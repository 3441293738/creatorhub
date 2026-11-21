"""Windows per-user WinINet system proxy management with backup and crash recovery."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

INTERNET_SETTINGS_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
DEFAULT_PROXY_BYPASS_ITEMS = ("127.0.0.1", "localhost", "[::1]", "<local>")
BACKUP_FILENAME = "channels_proxy_backup.json"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


@dataclass
class RegistrySnapshot:
    """Captured WinINet Internet Settings values plus existence flags."""

    proxy_enable: int | None
    proxy_server: str | None
    proxy_override: str | None

    def to_backup_dict(self, *, applied_server: str, proxy_port: int) -> dict[str, Any]:
        return {
            "version": 1,
            "applied_proxy_server": applied_server,
            "applied_proxy_port": int(proxy_port),
            "previous": {
                "proxy_enable": self.proxy_enable,
                "proxy_server": self.proxy_server,
                "proxy_override": self.proxy_override,
            },
        }


class SystemProxyBackend(Protocol):
    """Abstraction over registry + WinINet refresh for safe isolated unit tests."""

    @property
    def supported(self) -> bool: ...

    def read_snapshot(self) -> RegistrySnapshot: ...

    def apply_values(
        self,
        *,
        proxy_enable: int | None,
        proxy_server: str | None,
        proxy_override: str | None,
    ) -> None: ...


def current_process_integrity_rid() -> int | None:
    """Return Windows Mandatory Integrity RID (`0x1000` Low, `0x2000` Medium, `0x3000` High)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return None
        try:
            buf = ctypes.create_string_buffer(256)
            ret_len = wintypes.DWORD(0)
            if not advapi32.GetTokenInformation(token, 25, buf, 256, ctypes.byref(ret_len)):
                return None
            psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
            if not psid:
                return None
            sub_cnt = int(advapi32.GetSidSubAuthorityCount(psid).contents.value)
            if sub_cnt <= 0:
                return None
            return int(advapi32.GetSidSubAuthority(psid, sub_cnt - 1).contents.value)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return None


def _low_integrity_hint(port: int | None = None) -> str:
    port_text = str(port or 2023)
    return (
        "当前 CreatorHub 后端进程处于 Windows 低完整性级别（Low IL），"
        "通常是工作区 .venv\\Scripts\\python.exe 继承了沙箱 Low Mandatory Level 标签，"
        "系统内核会拒绝写入 HKCU\\...\\Internet Settings。"
        f"解决方式：① 改用非低完整性路径的 Python 启动 uvicorn；或 ② 取消勾选「自动托管 Windows 系统代理」，"
        f"启动代理后在 Windows 系统代理设置或 Clash 分流规则中将流量指向 127.0.0.1:{port_text}"
    )


class WindowsWinInetBackend:
    """Real HKCU WinINet registry backend (used only on Windows)."""

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    @staticmethod
    def _open_settings_key(winreg: Any, *, write: bool) -> Any:
        access = winreg.KEY_QUERY_VALUE | (winreg.KEY_SET_VALUE if write else 0)
        try:
            return winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_REG_PATH, 0, access)
        except FileNotFoundError:
            return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_REG_PATH, 0, access)

    def read_snapshot(self) -> RegistrySnapshot:
        if not self.supported:
            raise RuntimeError("当前操作系统不支持 Windows WinINet 系统代理设置")
        import winreg

        with self._open_settings_key(winreg, write=False) as key:
            return RegistrySnapshot(
                proxy_enable=self._read_reg_value(winreg, key, "ProxyEnable", expected_int=True),
                proxy_server=self._read_reg_value(winreg, key, "ProxyServer", expected_int=False),
                proxy_override=self._read_reg_value(winreg, key, "ProxyOverride", expected_int=False),
            )

    def apply_values(
        self,
        *,
        proxy_enable: int | None,
        proxy_server: str | None,
        proxy_override: str | None,
    ) -> None:
        if not self.supported:
            raise RuntimeError("当前操作系统不支持 Windows WinINet 系统代理设置")
        import ctypes
        import winreg

        try:
            key = self._open_settings_key(winreg, write=True)
        except OSError as exc:
            il = current_process_integrity_rid()
            if il is not None and il < 0x2000:
                raise PermissionError(f"{_low_integrity_hint()}（原始错误: {exc}）") from exc
            raise PermissionError(
                f"打开 Windows Internet Settings 注册表键被拒绝：{exc}；可在下方取消勾选「自动托管系统代理」改用 Clash/手动代理规则"
            ) from exc

        with key:
            for name, fn, val in (
                ("ProxyEnable", self._write_dword_or_delete, proxy_enable),
                ("ProxyServer", self._write_sz_or_delete, proxy_server),
                ("ProxyOverride", self._write_sz_or_delete, proxy_override),
            ):
                try:
                    fn(winreg, key, name, val)
                except OSError as exc:
                    raise PermissionError(
                        f"写入 Windows 系统代理注册表 {name} 失败：{exc}；如被系统策略或代理守卫锁定，可先取消勾选「自动托管系统代理」"
                    ) from exc

        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
        wininet.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)

    @staticmethod
    def _read_reg_value(winreg: Any, key: Any, name: str, *, expected_int: bool) -> Any:
        try:
            value, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        if expected_int:
            try:
                return int(value)
            except Exception:
                return 0
        return str(value) if value is not None else ""

    @staticmethod
    def _write_dword_or_delete(winreg: Any, key: Any, name: str, value: int | None) -> None:
        if value is None:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
            return
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))

    @staticmethod
    def _write_sz_or_delete(winreg: Any, key: Any, name: str, value: str | None) -> None:
        if value is None:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
            return
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


def merge_proxy_override(existing: str | None) -> str:
    """Append loopback + `<local>` bypass entries without duplicating existing items."""
    items: list[str] = []
    seen: set[str] = set()
    for part in str(existing or "").split(";"):
        cleaned = part.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            items.append(cleaned)
    for default_item in DEFAULT_PROXY_BYPASS_ITEMS:
        if default_item.lower() not in seen:
            seen.add(default_item.lower())
            items.append(default_item)
    return ";".join(items)


def _matches_creatorhub_server(current_server: str | None, applied_server: str | None) -> bool:
    current = str(current_server or "").strip().lower()
    applied = str(applied_server or "").strip().lower()
    if not current or not applied:
        return False
    if current == applied:
        return True
    # Also accept scheme-mapped form such as http=127.0.0.1:2023;https=127.0.0.1:2023
    return f"={applied}" in current


def parse_loopback_proxy_server(proxy_server: str | None) -> tuple[str, int] | None:
    """Extract `(host, port)` from a WinINet `ProxyServer` value when pointing to 127.0.0.1/localhost."""
    raw = str(proxy_server or "").strip()
    if not raw:
        return None

    candidate = ""
    if ";" in raw or "=" in raw:
        mapped: dict[str, str] = {}
        bare: list[str] = []
        for item in raw.split(";"):
            part = item.strip()
            if not part:
                continue
            if "=" in part:
                scheme, _, val = part.partition("=")
                mapped[scheme.strip().lower()] = val.strip()
            else:
                bare.append(part)
        candidate = mapped.get("https") or mapped.get("http") or (bare[0] if bare else "")
    else:
        candidate = raw

    for prefix in ("https://", "http://"):
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    candidate = candidate.strip().rstrip("/")
    if ":" not in candidate:
        return None
    host, _, port_str = candidate.rpartition(":")
    host_clean = host.strip().lower()
    if host_clean not in {"127.0.0.1", "localhost"} or not port_str.isdigit():
        return None
    port = int(port_str)
    if not (1 <= port <= 65535):
        return None
    return ("127.0.0.1" if host_clean == "127.0.0.1" else "localhost", port)


def _probe_tcp_endpoint(host: str, port: int, *, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_upstream_proxy(
    *,
    data_dir: Path | None = None,
    proxy_port: int,
    backend: SystemProxyBackend | None = None,
    probe_tcp: bool = True,
    timeout: float = 0.8,
) -> dict[str, Any]:
    """Inspect pre-existing WinINet loopback HTTP proxy and verify reachability before chaining."""
    ops = backend or WindowsWinInetBackend()
    proxy_enable: int | None = None
    proxy_server: str | None = None

    if data_dir is not None:
        backup_path = _active_backup_path(data_dir)
        backup = _read_backup(backup_path) if backup_path else None
        if isinstance(backup, dict) and isinstance(backup.get("previous"), dict):
            prev = backup["previous"]
            proxy_enable = prev.get("proxy_enable")
            proxy_server = prev.get("proxy_server")

    if proxy_enable is None and proxy_server is None and ops.supported:
        try:
            snap = ops.read_snapshot()
            proxy_enable = snap.proxy_enable
            proxy_server = snap.proxy_server
        except Exception:
            pass

    if int(proxy_enable or 0) != 1:
        return {"enabled": False, "host": "", "port": 0, "server": "", "note": "系统代理未启用，直连"}

    parsed = parse_loopback_proxy_server(proxy_server)
    if parsed is None:
        return {
            "enabled": False,
            "host": "",
            "port": 0,
            "server": "",
            "note": f"原代理非本机 HTTP 端口（{proxy_server or '-'}），直连",
        }

    up_host, up_port = parsed
    if up_port == int(proxy_port):
        return {
            "enabled": False,
            "host": "",
            "port": 0,
            "server": "",
            "note": f"原代理端口与当前监听端口相同（{up_port}），跳过同端口转发并改为直连",
        }

    server_text = f"{up_host}:{up_port}"
    if probe_tcp and not _probe_tcp_endpoint(up_host, up_port, timeout=timeout):
        return {
            "enabled": False,
            "host": "",
            "port": 0,
            "server": "",
            "note": f"原代理 {server_text} 当前未监听，自动回退直连",
        }

    return {
        "enabled": True,
        "host": up_host,
        "port": up_port,
        "server": server_text,
        "note": f"链式上游 {server_text}",
    }


def _fallback_backup_file() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()).expanduser()
    return (base / "CreatorHub" / BACKUP_FILENAME).resolve()


def _backup_candidates(data_dir: Path) -> tuple[Path, Path]:
    primary = backup_file_for(data_dir)
    fallback = _fallback_backup_file()
    return primary, fallback


def _active_backup_path(data_dir: Path) -> Path | None:
    for candidate in _backup_candidates(data_dir):
        if candidate.is_file():
            return candidate
    return None


def _read_backup(backup_path: Path) -> dict[str, Any] | None:
    if not backup_path.is_file():
        return None
    try:
        data = json.loads(backup_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_backup_at(target_path: Path, text: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        try:
            tmp_path.replace(target_path)
        except PermissionError:
            # Directories with restricted child-delete/rename ACLs allow direct overwrite when replace fails.
            target_path.write_text(text, encoding="utf-8")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_backup(backup_path: Path, payload: dict[str, Any]) -> Path:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        _write_backup_at(backup_path, text)
        return backup_path
    except PermissionError as exc:
        fallback = _fallback_backup_file()
        if fallback != backup_path:
            try:
                _write_backup_at(fallback, text)
                return fallback
            except PermissionError:
                pass
        raise PermissionError(
            f"无法写入代理备份文件 {backup_path}: {exc}"
        ) from exc


def _remove_backup_files(data_dir: Path) -> None:
    for candidate in _backup_candidates(data_dir):
        if not candidate.exists():
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def backup_file_for(data_dir: Path) -> Path:
    return (Path(data_dir).expanduser() / BACKUP_FILENAME).resolve()


def get_system_proxy_state(
    *,
    data_dir: Path | None = None,
    backend: SystemProxyBackend | None = None,
) -> dict[str, Any]:
    """Return current Windows WinINet proxy state and whether CreatorHub backup exists."""
    ops = backend or WindowsWinInetBackend()
    backup_exists = bool(data_dir and _active_backup_path(data_dir) is not None)
    if not ops.supported:
        return {
            "supported": False,
            "proxy_enable": False,
            "proxy_server": "",
            "proxy_override": "",
            "backup_present": backup_exists,
        }
    try:
        snap = ops.read_snapshot()
        il = current_process_integrity_rid() if isinstance(ops, WindowsWinInetBackend) else None
        return {
            "supported": True,
            "proxy_enable": bool(snap.proxy_enable),
            "proxy_server": str(snap.proxy_server or ""),
            "proxy_override": str(snap.proxy_override or ""),
            "backup_present": backup_exists,
            "low_integrity": bool(il is not None and il < 0x2000),
            "integrity_rid": il,
        }
    except Exception as exc:
        return {
            "supported": False,
            "proxy_enable": False,
            "proxy_server": "",
            "proxy_override": "",
            "backup_present": backup_exists,
            "error": str(exc),
        }


def enable_system_proxy(
    *,
    host: str = "127.0.0.1",
    port: int = 2023,
    data_dir: Path,
    backend: SystemProxyBackend | None = None,
) -> dict[str, Any]:
    """Back up existing Windows WinINet proxy settings and point WinINet to `host:port`."""
    ops = backend or WindowsWinInetBackend()
    if not ops.supported:
        return {
            "ok": False,
            "supported": False,
            "error": "自动配置系统代理仅支持 Windows；请手动将 HTTPS 代理设为 127.0.0.1:" + str(port),
        }

    proxy_host = str(host or "127.0.0.1").strip() or "127.0.0.1"
    proxy_port = int(port)
    target_server = f"{proxy_host}:{proxy_port}"
    backup_path = _active_backup_path(data_dir) or backup_file_for(data_dir)

    snap = ops.read_snapshot()
    existing_backup = _read_backup(backup_path)
    # If a backup from CreatorHub is already present and the live proxy still matches it,
    # preserve the pre-CreatorHub snapshot so repeated starts never overwrite the real baseline.
    if existing_backup and _matches_creatorhub_server(
        snap.proxy_server, existing_backup.get("applied_proxy_server")
    ):
        previous = existing_backup.get("previous") or {}
        base_override = previous.get("proxy_override")
        backup_payload = {
            **existing_backup,
            "applied_proxy_server": target_server,
            "applied_proxy_port": proxy_port,
        }
    else:
        base_override = snap.proxy_override
        backup_payload = snap.to_backup_dict(applied_server=target_server, proxy_port=proxy_port)

    written_backup = _write_backup(backup_path, backup_payload)
    new_override = merge_proxy_override(base_override)
    ops.apply_values(
        proxy_enable=1,
        proxy_server=target_server,
        proxy_override=new_override,
    )
    after = get_system_proxy_state(data_dir=data_dir, backend=ops)
    return {
        "ok": True,
        **after,
        "applied_server": target_server,
        "backup_path": str(written_backup),
        "previous": backup_payload.get("previous") or {},
    }


def restore_system_proxy(
    *,
    data_dir: Path,
    expected_port: int | None = None,
    backend: SystemProxyBackend | None = None,
) -> dict[str, Any]:
    """Restore WinINet settings from backup, guarding against clobbering external user changes."""
    ops = backend or WindowsWinInetBackend()
    backup_path = _active_backup_path(data_dir)
    if not ops.supported:
        return {"ok": True, "supported": False, "restored": False}

    backup = _read_backup(backup_path) if backup_path else None
    snap = ops.read_snapshot()

    if backup is not None:
        applied_server = str(backup.get("applied_proxy_server") or "")
        # Only revert registry if system proxy still points to CreatorHub (or ProxyEnable was left on by us).
        if snap.proxy_server and not _matches_creatorhub_server(snap.proxy_server, applied_server):
            _remove_backup_files(data_dir)
            after = get_system_proxy_state(data_dir=data_dir, backend=ops)
            return {
                "ok": True,
                "restored": False,
                "reason": "modified_externally",
                **after,
            }
        prev = backup.get("previous") or {}
        ops.apply_values(
            proxy_enable=prev.get("proxy_enable"),
            proxy_server=prev.get("proxy_server"),
            proxy_override=prev.get("proxy_override"),
        )
        _remove_backup_files(data_dir)
        after = get_system_proxy_state(data_dir=data_dir, backend=ops)
        return {"ok": True, "restored": True, **after}

    if expected_port is not None and _matches_creatorhub_server(
        snap.proxy_server, f"127.0.0.1:{int(expected_port)}"
    ):
        ops.apply_values(
            proxy_enable=0,
            proxy_server=snap.proxy_server,
            proxy_override=snap.proxy_override,
        )
        after = get_system_proxy_state(data_dir=data_dir, backend=ops)
        return {"ok": True, "restored": True, "from_fallback": True, **after}

    after = get_system_proxy_state(data_dir=data_dir, backend=ops)
    return {"ok": True, "restored": False, **after}


def recover_stale_system_proxy(
    data_dir: Path,
    *,
    backend: SystemProxyBackend | None = None,
) -> dict[str, Any]:
    """Clean up a leftover Windows system proxy left active by an unclean shutdown."""
    if _active_backup_path(data_dir) is None:
        return {"ok": True, "recovered": False}
    result = restore_system_proxy(data_dir=data_dir, backend=backend)
    return {"recovered": bool(result.get("restored")), **result}
