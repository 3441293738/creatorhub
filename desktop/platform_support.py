"""Small OS boundary; application logic and frontend stay shared."""
import os
from pathlib import Path
import platform
import subprocess
import sys


def default_home():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "CreatorHub" / "user-data"


def webview_options():
    return {"gui": "edgechromium"} if sys.platform == "win32" else {}


def platform_tag():
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "macos-x86_64"
    if sys.platform == "win32":
        return "windows-x64"
    return "unsupported"


def open_folder(path):
    path = str(Path(path).resolve())
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
