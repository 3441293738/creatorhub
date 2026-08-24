"""Discovery and metadata helpers for local Chromium runtime binaries."""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
from pathlib import Path


def executable_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _windows_file_version(path: Path) -> str:
    if os.name != "nt":
        return ""

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [
            ("dwSignature", ctypes.c_uint32),
            ("dwStrucVersion", ctypes.c_uint32),
            ("dwFileVersionMS", ctypes.c_uint32),
            ("dwFileVersionLS", ctypes.c_uint32),
            ("dwProductVersionMS", ctypes.c_uint32),
            ("dwProductVersionLS", ctypes.c_uint32),
            ("dwFileFlagsMask", ctypes.c_uint32),
            ("dwFileFlags", ctypes.c_uint32),
            ("dwFileOS", ctypes.c_uint32),
            ("dwFileType", ctypes.c_uint32),
            ("dwFileSubtype", ctypes.c_uint32),
            ("dwFileDateMS", ctypes.c_uint32),
            ("dwFileDateLS", ctypes.c_uint32),
        ]

    try:
        version = ctypes.windll.version
        unused = ctypes.c_uint32(0)
        size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint32(0)
        if not version.VerQueryValueW(
                buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return ""
        info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        parts = (
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        )
        return ".".join(str(value) for value in parts)
    except Exception:
        return ""


def detect_chromium_version(path: str | Path) -> str:
    executable = Path(path).expanduser().resolve()
    version = _windows_file_version(executable)
    if version and version != "0.0.0.0":
        return version
    # Portable archives normally keep the full build number in a parent or
    # sibling directory (for example Application/148.0.7778.215/chrome.dll).
    for parent in (executable.parent, *executable.parents[:3]):
        match = re.fullmatch(r"(\d{2,3}(?:\.\d+){1,3})", parent.name)
        if match:
            return match.group(1)
        try:
            versions = [
                item.name for item in parent.iterdir() if item.is_dir()
                and re.fullmatch(r"\d{2,3}(?:\.\d+){1,3}", item.name)
            ]
        except OSError:
            versions = []
        if versions:
            return sorted(versions, key=_version_tuple, reverse=True)[0]
    return ""


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except ValueError:
        return (0,)


def runtime_id_for(path: str | Path, version: str = "") -> str:
    executable = Path(path).expanduser().resolve()
    detected = version or detect_chromium_version(executable)
    major = detected.split(".", 1)[0] if detected else "custom"
    digest = hashlib.sha256(
        os.path.normcase(str(executable)).encode("utf-8")).hexdigest()[:8]
    return f"fp-{major}-{digest}"


def runtime_metadata(path: str | Path) -> dict:
    executable = Path(path).expanduser().resolve()
    if not executable.is_file():
        raise ValueError("内核可执行文件不存在")
    if executable.name.lower() not in {"chrome.exe", "chrome", "chromium", "chromium.exe"}:
        raise ValueError("请选择 Chromium 的 chrome/chromium 主程序")
    version = detect_chromium_version(executable)
    runtime_id = runtime_id_for(executable, version)
    return {
        "runtime_id": runtime_id,
        "name": f"Fingerprint Chromium {version or runtime_id}",
        "version": version,
        "executable_path": str(executable),
        "file_sha256": executable_sha256(executable),
    }


def discover_chromium_runtimes(root: str | Path) -> list[dict]:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise ValueError("内核扫描目录不存在")
    candidates: set[Path] = set()
    for name in ("chrome.exe", "chrome", "chromium.exe", "chromium"):
        candidates.update(path for path in base.rglob(name) if path.is_file())
    results = []
    for path in sorted(candidates, key=lambda item: os.path.normcase(str(item))):
        try:
            results.append(runtime_metadata(path))
        except (OSError, ValueError):
            continue
    return results
