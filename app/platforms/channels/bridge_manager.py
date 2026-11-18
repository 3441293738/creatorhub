"""桌面微信 Finder 本地回环 CDP 端口探测与受管 LoopbackBridge 生命周期控制。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .client import is_finder_page_target, validate_loopback_url
from .wmpf_cdp import (
    LoopbackBridge,
    discover_wechat_processes,
    inspect_endpoint_file,
)

COMMON_CDP_PORTS: tuple[int, ...] = (9222, 9223, 9229, 9333)


def _validate_port(port: int, label: str) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError(f"{label}必须是整数端口")
    if port != 0 and not (1024 <= port <= 65535):
        raise ValueError(f"{label}需在 1024–65535 之间（或 0 表示随机端口）")
    return port


class ChannelsBridgeManager:
    """在 CreatorHub 进程内按需托管单个 WMPF LoopbackBridge 会话。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = (data_dir or Path("data")).expanduser()
        self._bridge: LoopbackBridge | None = None
        self._lock = asyncio.Lock()

    def configure_data_dir(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir).expanduser()

    @property
    def endpoint_file(self) -> Path:
        return (self._data_dir / "channels_wmpf_endpoint.json").resolve()

    @property
    def status_file(self) -> Path:
        return (self._data_dir / "channels_wmpf_status.json").resolve()

    @property
    def running(self) -> bool:
        return bool(self._bridge is not None and self._bridge.servers)

    def status(self, *, include_processes: bool = True) -> dict[str, Any]:
        bridge = self._bridge
        base: dict[str, Any] = (
            bridge.status()
            if bridge is not None
            else {
                "transport": "wmpf-loopback-single-session",
                "listeners_active": False,
                "debug_listening_port": None,
                "cdp_listening_port": None,
                "bind_host": "127.0.0.1",
                "allowed_native_pids": [],
                "native_connected": False,
                "cdp_connected": False,
                "commands_forwarded": 0,
                "results_forwarded": 0,
                "rejected_connections_or_packets": 0,
                "error_categories": [],
                "raw_payload_logging": False,
                "updated_at": None,
            }
        )
        ep_file = self.endpoint_file
        base["running"] = bool(bridge is not None and bridge.servers)
        base["endpoint_file"] = str(ep_file)
        base["endpoint_file_exists"] = ep_file.is_file()
        if include_processes:
            base["wechat_processes"] = discover_wechat_processes()
        return base

    async def start(
        self,
        *,
        debug_port: int = 9421,
        cdp_port: int = 9422,
        allowed_pids: list[int] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._bridge is not None and self._bridge.servers:
                raise ValueError("本地 WMPF 桥接已在运行中，如需修改端口或 PID 请先停止后再启动")

            d_port = _validate_port(debug_port, "WMPF 调试监听端口")
            c_port = _validate_port(cdp_port, "CDP 转发端口")
            if d_port and c_port and d_port == c_port:
                raise ValueError("WMPF 调试监听端口与 CDP 转发端口不能相同")

            pids: set[int] = set()
            if allowed_pids:
                for raw_pid in allowed_pids:
                    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
                        raise ValueError("进程 PID 必须是正整数")
                    pids.add(raw_pid)
            else:
                pids = {item["pid"] for item in discover_wechat_processes()}

            if not pids:
                raise ValueError(
                    "未检测到运行中的桌面微信或 WMPF 进程，请先打开桌面微信，或手动填写允许的进程 PID"
                )

            ep_file = self.endpoint_file
            if ep_file.exists():
                info = inspect_endpoint_file(ep_file)
                owner = info.get("server_pid")
                if info.get("server_running") and owner not in (None, os.getpid()):
                    raise RuntimeError(f"受管口令文件正被其他桥接进程(PID {owner})占用")
                ep_file.unlink(missing_ok=True)

            bridge = LoopbackBridge(
                pids,
                debug_port=d_port,
                cdp_port=c_port,
                endpoint_file=ep_file,
                status_file=self.status_file,
            )
            await bridge.start()
            self._bridge = bridge
            return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            bridge = self._bridge
            self._bridge = None
            if bridge is not None:
                try:
                    await bridge.close()
                except RuntimeError:
                    # Event loop from an earlier lifespan may already be closed during test teardown.
                    for srv in getattr(bridge, "servers", ()):
                        try:
                            srv.close()
                        except Exception:
                            pass
            ep_file = self.endpoint_file
            if ep_file.exists():
                info = inspect_endpoint_file(ep_file)
                if info.get("server_pid") in (None, os.getpid()):
                    ep_file.unlink(missing_ok=True)
            return self.status()


async def _probe_http_cdp_port(client: httpx.AsyncClient, base_url: str) -> dict[str, Any] | None:
    list_url = base_url.rstrip("/") + "/json/list"
    try:
        resp = await client.get(list_url)
        if resp.status_code != 200:
            return None
        targets = resp.json()
    except Exception:
        return None
    if not isinstance(targets, list):
        return None

    page_targets = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    finder_targets: list[dict[str, str]] = []
    for target in page_targets:
        if not is_finder_page_target(target):
            continue
        parsed = urlsplit(str(target.get("url") or ""))
        finder_targets.append({
            "id": str(target.get("id") or target.get("targetId") or ""),
            "title": str(target.get("title") or "微信视频号")[:80],
            "path": parsed.path or "/",
        })
    return {
        "endpoint": base_url.rstrip("/"),
        "reachable": True,
        "total_pages": len(page_targets),
        "finder_targets": finder_targets,
        "has_finder_page": bool(finder_targets),
    }


async def discover_local_cdp_endpoints(
    extra_endpoints: list[str] | None = None,
    *,
    ports: tuple[int, ...] = COMMON_CDP_PORTS,
    timeout: float = 0.45,
) -> list[dict[str, Any]]:
    """安全探测本机回环常见 CDP HTTP 端口，识别已打开微信视频号页面。"""
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        raw = (url or "").strip().rstrip("/")
        if not raw:
            return
        try:
            validated = validate_loopback_url(raw, schemes=("http", "https"))
        except Exception:
            return
        parsed = urlsplit(validated)
        host = (parsed.hostname or "").lower()
        bracketed = f"[{host}]" if ":" in host else host
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        canonical = f"{parsed.scheme.lower()}://{bracketed}:{port}"
        if canonical not in seen:
            seen.add(canonical)
            candidates.append(canonical)

    for item in extra_endpoints or ():
        _add(item)
    for port in ports:
        _add(f"http://127.0.0.1:{int(port)}")

    if not candidates:
        return []

    async with httpx.AsyncClient(timeout=max(0.2, float(timeout)), trust_env=False) as client:
        results = await asyncio.gather(
            *(_probe_http_cdp_port(client, url) for url in candidates),
            return_exceptions=True,
        )

    found = [item for item in results if isinstance(item, dict)]
    found.sort(key=lambda item: (not item["has_finder_page"], item["endpoint"]))
    return found
