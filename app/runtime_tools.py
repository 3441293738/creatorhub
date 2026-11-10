"""Resolve shared executable runtimes without asking users to install Python/Node."""
from pathlib import Path
import shutil
import sys


def find_node():
    # Frozen builds already carry Patchright's matching Node runtime.
    if getattr(sys, "frozen", False):
        from patchright._impl._driver import compute_driver_executable
        node, _ = compute_driver_executable()
        return str(node) if Path(node).is_file() else ""
    return shutil.which("node") or ""


def find_ffmpeg():
    # Frozen builds are deterministic; a random PATH binary must not override
    # the tested media runtime. Source installations retain their system choice.
    if not getattr(sys, "frozen", False):
        executable = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if executable:
            return executable
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        executable = get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        return ""
    return str(executable) if executable and Path(executable).is_file() else ""
