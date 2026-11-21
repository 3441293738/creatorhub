"""Desktop WeChat process lookup, foreground wake, and clean restart helpers (zero injection)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .wmpf_cdp import discover_wechat_processes

_MAIN_WECHAT_EXES = ("Weixin.exe", "WeChat.exe")
_MAIN_WECHAT_EXES_LOWER = frozenset(name.lower() for name in _MAIN_WECHAT_EXES)


def _creation_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _query_process_exe_path(pid: int) -> Path | None:
    """Resolve full executable path of a local PID using PROCESS_QUERY_LIMITED_INFORMATION."""
    if sys.platform != "win32" or pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            buf_len = wintypes.DWORD(4096)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                return None
            raw = str(buf.value or "").strip()
            if not raw:
                return None
            candidate = Path(raw)
            return candidate if candidate.is_file() else None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _registry_install_candidates() -> list[Path]:
    if sys.platform != "win32":
        return []
    candidates: list[Path] = []
    try:
        import winreg

        roots = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
        subkeys = [
            (r"Software\Tencent\Weixin", "Weixin.exe"),
            (r"Software\Tencent\WeChat", "WeChat.exe"),
            (r"Software\WOW6432Node\Tencent\Weixin", "Weixin.exe"),
            (r"Software\WOW6432Node\Tencent\WeChat", "WeChat.exe"),
        ]
        for root in roots:
            for subkey, exe_name in subkeys:
                try:
                    with winreg.OpenKey(root, subkey, 0, winreg.KEY_QUERY_VALUE) as key:
                        for value_name in ("InstallPath", "ExePath"):
                            try:
                                val, _ = winreg.QueryValueEx(key, value_name)
                            except FileNotFoundError:
                                continue
                            raw = str(val or "").strip().strip('"')
                            if not raw:
                                continue
                            p = Path(raw)
                            if p.suffix.lower() == ".exe":
                                candidates.append(p)
                            else:
                                candidates.append(p / exe_name)
                except OSError:
                    continue
    except Exception:
        pass
    return candidates


def _standard_install_candidates() -> list[Path]:
    if sys.platform != "win32":
        return []
    bases: list[Path] = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        raw = os.environ.get(env_key)
        if raw:
            bases.append(Path(raw))
    out: list[Path] = []
    for base in bases:
        out.append(base / "Tencent" / "Weixin" / "Weixin.exe")
        out.append(base / "Tencent" / "WeChat" / "WeChat.exe")
    return out


def find_wechat_executable() -> Path | None:
    """Locate desktop WeChat (`Weixin.exe` or `WeChat.exe`) from active PIDs, registry, or standard paths."""
    if sys.platform != "win32":
        return None

    for proc in discover_wechat_processes():
        name = str(proc.get("name") or "")
        pid = int(proc.get("pid") or 0)
        if name.lower() in _MAIN_WECHAT_EXES_LOWER and pid > 0:
            exe = _query_process_exe_path(pid)
            if exe is not None and exe.name.lower() in _MAIN_WECHAT_EXES_LOWER:
                return exe

    for candidate in (*_registry_install_candidates(), *_standard_install_candidates()):
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _launch_executable_detached(exe_path: Path) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    subprocess.Popen(
        [str(exe_path)],
        cwd=str(exe_path.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def open_or_focus_wechat() -> dict[str, Any]:
    """Wake or launch desktop WeChat on Windows without triggering browser protocol dialogs."""
    if sys.platform != "win32":
        return {"ok": False, "error": "仅支持在 Windows 桌面环境自动唤起微信"}

    was_running = any(
        str(p.get("name") or "").lower() in _MAIN_WECHAT_EXES_LOWER
        for p in discover_wechat_processes()
    )
    exe_path = find_wechat_executable()
    if exe_path is not None:
        try:
            _launch_executable_detached(exe_path)
            return {
                "ok": True,
                "launched": not was_running,
                "was_running": was_running,
                "exe_path": str(exe_path),
                "message": "已唤起电脑微信窗口" if was_running else "已启动电脑微信客户端",
            }
        except Exception as exc:
            last_err = str(exc)
    else:
        last_err = ""

    startfile = getattr(os, "startfile", None)
    if callable(startfile):
        try:
            startfile("weixin://")
            return {
                "ok": True,
                "launched": not was_running,
                "was_running": was_running,
                "exe_path": "",
                "message": "已唤起电脑微信",
            }
        except OSError as exc:
            last_err = str(exc)

    return {
        "ok": False,
        "error": f"未能自动唤起电脑微信，请手动打开桌面微信（{last_err or '未检测到安装路径'}）",
    }


def restart_wechat(*, wait_timeout: float = 3.0) -> dict[str, Any]:
    """Close running desktop WeChat and relaunch it so WinINet system proxy settings take effect."""
    if sys.platform != "win32":
        return {"ok": False, "error": "仅支持在 Windows 桌面环境自动重开微信"}

    exe_path = find_wechat_executable()
    if exe_path is None:
        return {
            "ok": False,
            "error": "未定位到桌面微信安装路径，请手动在右下角托盘退出微信后重新打开",
        }

    running_main = [
        p
        for p in discover_wechat_processes()
        if str(p.get("name") or "").lower() in _MAIN_WECHAT_EXES_LOWER
    ]
    if running_main:
        seen_names = sorted({str(p["name"]) for p in running_main if p.get("name")})
        for exe_name in seen_names:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/IM", exe_name],
                    capture_output=True,
                    timeout=6.0,
                    creationflags=_creation_flags(),
                )
            except Exception:
                pass

        deadline = time.monotonic() + max(0.5, float(wait_timeout))
        while time.monotonic() < deadline:
            still_alive = any(
                str(p.get("name") or "").lower() in _MAIN_WECHAT_EXES_LOWER
                for p in discover_wechat_processes()
            )
            if not still_alive:
                break
            time.sleep(0.15)

    try:
        _launch_executable_detached(exe_path)
    except Exception as exc:
        return {"ok": False, "error": f"重新启动微信失败，请手动打开桌面微信: {exc}"}

    return {
        "ok": True,
        "restarted": bool(running_main),
        "exe_path": str(exe_path),
        "message": (
            "已重新启动电脑微信，请在弹出的微信窗口点击「进入微信」，再点开「文件传输助手」中的桥接链接"
            if running_main
            else "已启动电脑微信，请登录后在「文件传输助手」点开桥接链接"
        ),
    }
