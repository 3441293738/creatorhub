"""CreatorHub-owned stable Chrome processes for Xiaohongshu CDP sessions."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import signal
import shutil
import socket
import subprocess
import time
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from .identity import Identity
from .proxy import ProxyPlan, Socks5AuthRelay


_OWNER_FILE = ".creatorhub-cdp-owner.json"
_WEBRTC_PROXY_ARGS = (
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
)


class CdpLaunchError(RuntimeError):
    """Stable Chrome or its CDP endpoint could not be initialized."""


class CdpProfileConflictError(CdpLaunchError):
    """The managed profile appears to be owned by another Chrome process."""


class CdpProxyError(CdpLaunchError):
    """The configured proxy could not be initialized."""


class _CdpPortRetryError(CdpLaunchError):
    """Chrome exited before binding the selected debugging port."""


class ChromeLocator:
    """Locate only a stable Google Chrome installation."""

    def __init__(
            self, *, system: str | None = None,
            environ: dict[str, str] | None = None,
            exists: Callable[[os.PathLike[str] | str], bool] | None = None,
            which: Callable[[str], str | None] | None = None):
        self.system = system or platform.system()
        # ``os.environ`` is case-insensitive on Windows, but converting it to a
        # normal dict commonly produces uppercase keys.  Normalize explicitly.
        source_environ = os.environ if environ is None else environ
        self.environ = {
            str(key).casefold(): str(value)
            for key, value in source_environ.items()
        }
        self.exists = exists or os.path.isfile
        self.which = which or shutil.which

    def find(self) -> Path | None:
        candidates: list[Path] = []
        if self.system == "Windows":
            for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                root = self.environ.get(env_name.casefold())
                if root:
                    candidates.append(
                        Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
        elif self.system == "Darwin":
            candidates.extend((
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ))
        else:
            for binary in ("google-chrome-stable", "google-chrome"):
                found = self.which(binary)
                if found:
                    return Path(found)
            candidates.extend((
                Path("/usr/bin/google-chrome-stable"),
                Path("/usr/bin/google-chrome"),
                Path("/opt/google/chrome/google-chrome"),
            ))
        for candidate in candidates:
            if self.exists(candidate):
                return candidate
        return None


def chrome_launch_args(
        profile_dir: Path, port: int,
        proxy_server: str = "") -> list[str]:
    """Build the minimal native Chrome argument set used by the CDP backend."""
    port = int(port)
    if not (1 <= port <= 65535):
        raise ValueError("CDP 调试端口必须是非零有效端口")
    args = [
        f"--user-data-dir={Path(profile_dir).resolve()}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        # Routine account tasks reuse one headed page in the background.
        # Login/explicit-open flows restore the same window when required.
        "--start-minimized",
    ]
    if proxy_server:
        args.append(f"--proxy-server={proxy_server}")
        args.extend(_WEBRTC_PROXY_ARGS)
    args.append("about:blank")
    return args


def select_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port == 0:  # defensive: the OS contract should already prevent this
        raise CdpLaunchError("操作系统未分配有效的 CDP 端口")
    return port


@dataclass
class OwnedChromeProcess:
    pid: int
    executable: Path
    profile_dir: Path
    started_at: str
    marker_path: Path
    port: int
    process: Any = None
    recovered: bool = False


@dataclass
class XhsCdpSession:
    browser: Any
    context: Any
    owned: OwnedChromeProcess
    proxy_signature: str = "direct"
    relay: Socks5AuthRelay | None = None
    last_used: float = 0.0
    auth_controller: CdpProxyAuthController | None = None


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    executable: Path
    command_line: tuple[str, ...]


class ProcessInspector:
    """Best-effort process metadata used only for strict owner validation."""

    def inspect(self, pid: int) -> ProcessInfo | None:
        try:
            if os.name == "nt":
                script = (
                    "$p=Get-CimInstance Win32_Process -Filter \"ProcessId = "
                    f"{int(pid)}\" -ErrorAction Stop;"
                    "if($null -eq $p){exit 3};"
                    "[pscustomobject]@{ExecutablePath=$p.ExecutablePath;"
                    "CommandLine=$p.CommandLine}|ConvertTo-Json -Compress"
                )
                completed = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive",
                     "-Command", script],
                    capture_output=True, text=True, timeout=3,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode != 0 or not completed.stdout.strip():
                    return None
                payload = json.loads(completed.stdout)
                executable = payload.get("ExecutablePath") or ""
                command = payload.get("CommandLine") or ""
                if not executable:
                    return None
                return ProcessInfo(
                    pid=int(pid), executable=Path(executable),
                    command_line=(str(command),))
            if platform.system() == "Linux":
                proc = Path("/proc") / str(int(pid))
                executable = Path(os.readlink(proc / "exe"))
                raw = (proc / "cmdline").read_bytes()
                command = tuple(
                    part.decode("utf-8", "replace")
                    for part in raw.split(b"\0") if part)
                return ProcessInfo(int(pid), executable, command)
            completed = subprocess.run(
                ["ps", "-p", str(int(pid)), "-o", "command="],
                capture_output=True, text=True, timeout=3)
            if completed.returncode != 0 or not completed.stdout.strip():
                return None
            command = completed.stdout.strip()
            executable = self._executable_from_command(command)
            return ProcessInfo(int(pid), executable, (command,))
        except Exception:
            return None

    @staticmethod
    def _executable_from_command(command: str) -> Path:
        # macOS ``ps command`` leaves the Chrome app path unquoted even though
        # it contains spaces. Our first managed argument is always user-data-dir.
        marker = " --user-data-dir="
        if marker in command:
            return Path(command.split(marker, 1)[0].strip())
        return Path(command.split(None, 1)[0])

    @staticmethod
    def terminate(pid: int) -> None:
        if os.name == "nt":
            with suppress(Exception):
                subprocess.run(
                    ["taskkill.exe", "/PID", str(int(pid)), "/T", "/F"],
                    capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            return
        with suppress(OSError, ProcessLookupError):
            os.kill(int(pid), signal.SIGTERM)


class CdpProxyAuthController:
    """Install per-page Fetch handlers for an authenticated HTTP proxy."""

    def __init__(self, context: Any, plan: ProxyPlan, max_attempts: int = 3):
        if plan.scheme not in {"http", "https"} or not plan.authenticated:
            raise CdpProxyError(
                "CDP 代理认证控制器只接受认证 HTTP 代理")
        self.context = context
        self.plan = plan
        self.max_attempts = max(1, int(max_attempts))
        self._sessions: list[Any] = []
        self._tasks: set[asyncio.Task] = set()
        self._attempts: dict[tuple[int, str], int] = {}
        self._closed = False
        self.last_error = ""

    async def install(self, page: Any) -> None:
        if self._closed:
            raise CdpProxyError("CDP 代理认证控制器已关闭")
        session = await self.context.new_cdp_session(page)
        session_key = id(session)

        def request_paused(event: dict[str, Any]) -> None:
            self._schedule(self._continue_request(session, event))

        def auth_required(event: dict[str, Any]) -> None:
            self._schedule(self._continue_auth(session_key, session, event))

        # Registration deliberately precedes Fetch.enable so no paused request
        # can arrive without a continuation handler.
        session.on("Fetch.requestPaused", request_paused)
        session.on("Fetch.authRequired", auth_required)
        await session.send("Fetch.enable", {"handleAuthRequests": True})
        self._sessions.append(session)

    def _schedule(self, awaitable: Awaitable[None]) -> None:
        if self._closed:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            return
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.last_error = (
                    f"代理认证事件处理失败：{type(exc).__name__}")

        task.add_done_callback(done)

    async def _continue_request(
            self, session: Any, event: dict[str, Any]) -> None:
        request_id = str(event.get("requestId") or "")
        if request_id:
            await session.send(
                "Fetch.continueRequest", {"requestId": request_id})

    async def _continue_auth(
            self, session_key: int, session: Any,
            event: dict[str, Any]) -> None:
        request_id = str(event.get("requestId") or "")
        challenge = event.get("authChallenge") or {}
        response: dict[str, str]
        if not (
                challenge.get("source") == "Proxy"
                and self._challenge_matches(str(challenge.get("origin") or ""))):
            response = {"response": "Default"}
        else:
            key = (session_key, request_id)
            attempts = self._attempts.get(key, 0) + 1
            self._attempts[key] = attempts
            if attempts >= self.max_attempts:
                response = {"response": "CancelAuth"}
                self.last_error = "代理认证连续失败"
            else:
                response = {
                    "response": "ProvideCredentials",
                    "username": self.plan.username,
                    "password": self.plan.password,
                }
        await session.send("Fetch.continueWithAuth", {
            "requestId": request_id,
            "authChallengeResponse": response,
        })

    def _challenge_matches(self, origin: str) -> bool:
        try:
            parsed = urlparse(
                origin if "://" in origin else f"//{origin}")
            host = (parsed.hostname or "").casefold()
            if parsed.port is not None:
                port = parsed.port
            elif parsed.scheme == "https":
                port = 443
            else:
                port = 80
            return host == self.plan.host.casefold() and port == self.plan.port
        except ValueError:
            return False

    async def close(self) -> None:
        self._closed = True
        while self._tasks:
            tasks = list(self._tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
        for session in reversed(self._sessions):
            with suppress(Exception):
                await session.send("Fetch.disable")
            with suppress(Exception):
                await session.detach()
        self._sessions.clear()
        self._attempts.clear()


class XhsCdpBackend:
    """Launch and connect to one CreatorHub-owned Chrome per account."""

    def __init__(
            self, playwright: Any, profiles_root: str,
            *, locator: ChromeLocator | Any | None = None,
            process_factory: Callable[[list[str]], Any] | None = None,
            connector: Callable[..., Awaitable[Any]] | None = None,
            endpoint_probe: Callable[[int], Awaitable[bool]] | None = None,
            proxy_probe: Callable[[ProxyPlan], Awaitable[bool]] | None = None,
            port_selector: Callable[[], int] | None = None,
            process_inspector: ProcessInspector | Any | None = None,
            sleep: Callable[[float], Awaitable[None]] | None = None,
            monotonic: Callable[[], float] | None = None,
            startup_timeout: float = 12.0):
        self.playwright = playwright
        self.profiles_root = Path(profiles_root)
        self.locator = locator or ChromeLocator()
        self.process_factory = process_factory or self._spawn_process
        self.connector = connector or self._connect
        self.endpoint_probe = endpoint_probe or self._probe_endpoint
        self.proxy_probe = proxy_probe or self._probe_proxy
        self.port_selector = port_selector or select_loopback_port
        self.process_inspector = process_inspector or ProcessInspector()
        self.sleep = sleep or asyncio.sleep
        self.monotonic = monotonic or time.monotonic
        self.startup_timeout = max(0.1, float(startup_timeout))

    async def open(
            self, identity: Identity,
            proxy_plan: ProxyPlan | None) -> XhsCdpSession:
        executable = self.locator.find()
        if executable is None:
            raise CdpLaunchError("未检测到系统 Chrome")
        profile_dir = Path(identity.profile_dir).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        if proxy_plan is not None and not await self.proxy_probe(proxy_plan):
            raise CdpProxyError(
                f"代理连接失败：{proxy_plan.redacted}")
        recovered = await self._recover(
            profile_dir, Path(executable).resolve(), proxy_plan)
        if recovered is not None:
            return recovered
        relay: Socks5AuthRelay | None = None
        process = None
        browser = None
        marker_path = profile_dir / _OWNER_FILE
        try:
            proxy_server = ""
            if proxy_plan is not None:
                if proxy_plan.scheme == "socks5" and proxy_plan.authenticated:
                    relay = Socks5AuthRelay(proxy_plan)
                    relay_port = await relay.start()
                    proxy_server = proxy_plan.chrome_server(relay_port)
                else:
                    proxy_server = proxy_plan.chrome_server()

            owned = None
            for attempt in range(3):
                port = int(self.port_selector())
                args = [str(Path(executable)), *chrome_launch_args(
                    profile_dir, port, proxy_server)]
                process = self.process_factory(args)
                started_at = datetime.now(timezone.utc).isoformat()
                owned = OwnedChromeProcess(
                    pid=int(process.pid),
                    executable=Path(executable).resolve(),
                    profile_dir=profile_dir,
                    started_at=started_at,
                    marker_path=marker_path,
                    port=port,
                    process=process,
                )
                self._write_marker(owned)
                try:
                    await self._wait_until_ready(process, port)
                    break
                except _CdpPortRetryError:
                    await self._stop_process(process)
                    self._remove_owned_marker(marker_path, owned.pid)
                    process = None
                    if attempt == 2:
                        raise CdpLaunchError(
                            "系统 Chrome 连续未能绑定随机 CDP 端口")
            assert owned is not None
            browser = await self.connector(
                f"http://127.0.0.1:{port}",
                is_local=True,
                no_defaults=True,
            )
            contexts = list(getattr(browser, "contexts", ()) or ())
            if not contexts:
                raise CdpLaunchError("系统 Chrome 未提供默认 Context")
            return XhsCdpSession(
                browser=browser,
                context=contexts[0],
                owned=owned,
                proxy_signature=(proxy_plan.signature if proxy_plan else "direct"),
                relay=relay,
                last_used=self.monotonic(),
            )
        except asyncio.CancelledError:
            await self._cleanup_partial(browser, process, marker_path, relay)
            raise
        except CdpLaunchError:
            await self._cleanup_partial(browser, process, marker_path, relay)
            raise
        except Exception as exc:
            await self._cleanup_partial(browser, process, marker_path, relay)
            raise CdpLaunchError(
                f"系统 Chrome CDP 启动失败：{type(exc).__name__}") from exc

    async def close(self, session: XhsCdpSession) -> None:
        if session.auth_controller is not None:
            with suppress(Exception):
                await session.auth_controller.close()
        with suppress(Exception):
            await session.browser.close()
        if session.owned.process is not None:
            await self._stop_process(session.owned.process)
        elif session.owned.recovered:
            info = await asyncio.to_thread(
                self.process_inspector.inspect, session.owned.pid)
            if info is not None and self._process_matches_owned(
                    info, session.owned):
                with suppress(Exception):
                    await asyncio.to_thread(
                        self.process_inspector.terminate, session.owned.pid)
            # On Windows the CDP Browser.close acknowledgement can arrive a
            # fraction before Chrome's Crashpad child releases profile files.
            # Give that owned helper a brief exit window so immediate profile
            # reuse/cleanup does not race a still-open file handle.
            if os.name == "nt":
                await self.sleep(0.25)
        self._remove_owned_marker(session.owned.marker_path, session.owned.pid)
        if session.relay is not None:
            with suppress(Exception):
                await session.relay.close()

    async def _wait_until_ready(self, process: Any, port: int) -> None:
        deadline = self.monotonic() + self.startup_timeout
        while self.monotonic() < deadline:
            if process.poll() is not None:
                raise _CdpPortRetryError("系统 Chrome 在 CDP 就绪前退出")
            if await self.endpoint_probe(port):
                return
            await self.sleep(0.1)
        raise CdpLaunchError("系统 Chrome CDP 启动超时")

    async def _recover(
            self, profile_dir: Path, executable: Path,
            proxy_plan: ProxyPlan | None) -> XhsCdpSession | None:
        marker_path = profile_dir / _OWNER_FILE
        active_path = profile_dir / "DevToolsActivePort"
        if not marker_path.exists() and not active_path.exists():
            return None

        marker = self._read_marker(marker_path)
        port = self._read_active_port(active_path)
        info = None
        if marker is not None:
            info = await asyncio.to_thread(
                self.process_inspector.inspect, int(marker["pid"]))
        owner_valid = bool(
            marker is not None and info is not None
            and self._marker_owner_matches(
                marker, info, executable, profile_dir)
        )
        # Stable Chrome does not create DevToolsActivePort when an explicit
        # nonzero port is supplied.  Once PID/executable/profile ownership is
        # proven, the credential-free loopback port in that process command is
        # the authoritative recovery source.
        if owner_valid and info is not None:
            command_port = self._read_process_port(info)
            if command_port is not None:
                port = command_port
        endpoint_live = bool(port and await self.endpoint_probe(port))
        valid = bool(
            owner_valid and marker is not None and info is not None
            and self._marker_matches(
                marker, info, executable, profile_dir, proxy_plan)
        )

        if endpoint_live and not valid:
            if owner_valid:
                assert marker is not None and port is not None
                await self._terminate_owned_for_restart(
                    marker_path, active_path, int(marker["pid"]), port)
                return None
            raise CdpProfileConflictError(
                "账号 Profile 已被无法验证所有权的 Chrome 占用")
        if not endpoint_live:
            if owner_valid:
                raise CdpProfileConflictError(
                    "账号 Profile 的专用 Chrome 仍在运行，但 CDP 端点不可用")
            if marker_path.exists():
                with suppress(OSError):
                    marker_path.unlink()
            return None

        # An authenticated SOCKS relay lives only in the old CreatorHub
        # process. Reusing that Chrome would retain a dead loopback proxy.
        if proxy_plan is not None and (
                proxy_plan.scheme == "socks5" and proxy_plan.authenticated):
            assert marker is not None and info is not None and port is not None
            await self._terminate_owned_for_restart(
                marker_path, active_path, int(marker["pid"]), port)
            return None

        assert marker is not None and info is not None and port is not None
        try:
            browser = await self.connector(
                f"http://127.0.0.1:{port}",
                is_local=True,
                no_defaults=True,
            )
        except Exception as exc:
            raise CdpLaunchError(
                f"恢复系统 Chrome CDP 失败：{type(exc).__name__}") from exc
        contexts = list(getattr(browser, "contexts", ()) or ())
        if not contexts:
            with suppress(Exception):
                await browser.close()
            raise CdpLaunchError("系统 Chrome 未提供默认 Context")
        owned = OwnedChromeProcess(
            pid=int(marker["pid"]),
            executable=Path(marker["executable"]).resolve(),
            profile_dir=profile_dir,
            started_at=str(marker["started_at"]),
            marker_path=marker_path,
            port=port,
            process=None,
            recovered=True,
        )
        return XhsCdpSession(
            browser=browser,
            context=contexts[0],
            owned=owned,
            proxy_signature=(proxy_plan.signature if proxy_plan else "direct"),
            last_used=self.monotonic(),
        )

    @staticmethod
    def _read_marker(marker_path: Path) -> dict[str, Any] | None:
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if set(marker) != {
                    "pid", "executable", "profile_dir", "started_at"}:
                return None
            if int(marker["pid"]) <= 0 or not str(marker["started_at"]):
                return None
            return marker
        except Exception:
            return None

    @staticmethod
    def _read_active_port(active_path: Path) -> int | None:
        try:
            lines = active_path.read_text(encoding="utf-8").splitlines()
            port = int(lines[0])
            websocket_path = lines[1]
            if not (1 <= port <= 65535):
                return None
            if not websocket_path.startswith("/devtools/browser/"):
                return None
            return port
        except Exception:
            return None

    @staticmethod
    def _read_process_port(info: ProcessInfo) -> int | None:
        command = "\0".join(str(item) for item in info.command_line)
        if "--remote-debugging-address=127.0.0.1" not in command:
            return None
        match = re.search(r"--remote-debugging-port=(\d+)", command)
        if match is None:
            return None
        try:
            port = int(match.group(1))
        except (TypeError, ValueError):
            return None
        return port if 1 <= port <= 65535 else None

    @classmethod
    def _marker_owner_matches(
            cls, marker: dict[str, Any], info: ProcessInfo,
            executable: Path, profile_dir: Path) -> bool:
        try:
            marker_owned = OwnedChromeProcess(
                pid=int(marker["pid"]),
                executable=Path(marker["executable"]).resolve(),
                profile_dir=Path(marker["profile_dir"]).resolve(),
                started_at=str(marker["started_at"]),
                marker_path=profile_dir / _OWNER_FILE,
                port=1,
            )
        except Exception:
            return False
        if cls._path_key(marker_owned.executable) != cls._path_key(executable):
            return False
        if cls._path_key(marker_owned.profile_dir) != cls._path_key(profile_dir):
            return False
        return cls._process_matches_owned(info, marker_owned)

    @classmethod
    def _marker_matches(
            cls, marker: dict[str, Any], info: ProcessInfo,
            executable: Path, profile_dir: Path,
            proxy_plan: ProxyPlan | None) -> bool:
        if not cls._marker_owner_matches(
                marker, info, executable, profile_dir):
            return False
        command = "\0".join(info.command_line)
        proxy_args = [
            item for item in info.command_line
            if "--proxy-server=" in item
        ]
        if not proxy_args and "--proxy-server=" in command:
            proxy_args = [command]
        if proxy_plan is None:
            return not proxy_args
        if proxy_plan.scheme == "socks5" and proxy_plan.authenticated:
            return bool(proxy_args and "socks5://127.0.0.1:" in command)
        return f"--proxy-server={proxy_plan.chrome_server()}" in command

    async def _terminate_owned_for_restart(
            self, marker_path: Path, active_path: Path,
            pid: int, port: int) -> None:
        try:
            await asyncio.to_thread(self.process_inspector.terminate, pid)
        except Exception as exc:
            raise CdpProfileConflictError(
                f"专用 Chrome 未能安全退出：{type(exc).__name__}") from exc
        for _attempt in range(30):
            process_live = await asyncio.to_thread(
                self.process_inspector.inspect, pid)
            endpoint_live = await self.endpoint_probe(port)
            if process_live is None and not endpoint_live:
                break
            await self.sleep(0.1)
        else:
            raise CdpProfileConflictError("专用 Chrome 未能安全退出")
        self._remove_owned_marker(marker_path, pid)
        with suppress(OSError):
            active_path.unlink()

    @classmethod
    def _process_matches_owned(
            cls, info: ProcessInfo,
            owned: OwnedChromeProcess) -> bool:
        if int(info.pid) != int(owned.pid):
            return False
        if cls._path_key(info.executable) != cls._path_key(owned.executable):
            return False
        expected_profile = f"--user-data-dir={owned.profile_dir}"
        command = "\0".join(info.command_line)
        return bool(
            expected_profile in command
            and cls._read_process_port(info) is not None
            and "--no-first-run" in command
            and "--no-default-browser-check" in command
        )

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(str(Path(path).resolve()))

    async def _cleanup_partial(
            self, browser: Any, process: Any, marker_path: Path,
            relay: Socks5AuthRelay | None) -> None:
        if browser is not None:
            with suppress(Exception):
                await browser.close()
        pid = int(getattr(process, "pid", 0) or 0)
        await self._stop_process(process)
        self._remove_owned_marker(marker_path, pid)
        if relay is not None:
            with suppress(Exception):
                await relay.close()

    @staticmethod
    def _write_marker(owned: OwnedChromeProcess) -> None:
        payload = {
            "pid": owned.pid,
            "executable": str(owned.executable),
            "profile_dir": str(owned.profile_dir),
            "started_at": owned.started_at,
        }
        temp = owned.marker_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temp, owned.marker_path)

    @staticmethod
    def _remove_owned_marker(marker_path: Path, pid: int) -> None:
        try:
            current = json.loads(marker_path.read_text(encoding="utf-8"))
            if int(current.get("pid") or 0) != int(pid):
                return
        except Exception:
            return
        with suppress(OSError):
            marker_path.unlink()

    @staticmethod
    async def _stop_process(process: Any) -> None:
        if process is None or process.poll() is not None:
            return
        with suppress(Exception):
            process.terminate()
        try:
            await asyncio.to_thread(process.wait, 5)
            return
        except Exception:
            pass
        with suppress(Exception):
            process.kill()
        with suppress(Exception):
            await asyncio.to_thread(process.wait, 2)

    def _spawn_process(self, args: list[str]):
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=(os.name != "nt"),
        )

    async def _connect(self, endpoint: str, **kwargs):
        return await self.playwright.chromium.connect_over_cdp(
            endpoint, **kwargs)

    @staticmethod
    async def _probe_endpoint(port: int) -> bool:
        def request() -> bool:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(
                        f"http://127.0.0.1:{port}/json/version",
                        timeout=0.35) as response:
                    if response.status != 200:
                        return False
                    payload = json.loads(response.read().decode("utf-8"))
                    return bool(payload.get("webSocketDebuggerUrl"))
            except Exception:
                return False

        return await asyncio.to_thread(request)

    @staticmethod
    async def _probe_proxy(plan: ProxyPlan) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(plan.host, plan.port), timeout=3.0)
        except Exception:
            return False
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        return True
