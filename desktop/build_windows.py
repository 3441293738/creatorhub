"""Build a self-contained Windows directory; Inno Setup wraps it afterwards."""
from pathlib import Path
import argparse
import subprocess
import sys
import json
import platform
from importlib.metadata import version as package_version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from preview.build_preview import build_guide


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="0.2.0")
    parser.add_argument("--dist-dir", default="dist/windows", help="Repository-local output directory")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Windows executable builds must run on Windows.")
    import re
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", args.version):
        raise SystemExit("Version must be numeric: 1.2.3 or 1.2.3.4")
    staging = ROOT / "build" / "windows"
    staging.mkdir(parents=True, exist_ok=True)
    dist_root = (ROOT / args.dist_dir).resolve()
    output = (dist_root / "CreatorHub").resolve()
    if not output.is_relative_to(ROOT.resolve()):
        raise SystemExit("Build output must remain inside the repository")
    from desktop.ui import brand_image
    brand_image(256).save(staging / "CreatorHub.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    build_guide(staging / "desktop-guide")
    (staging / "desktop-version.txt").write_text(args.version, encoding="utf-8")
    (staging / "build-runtime.json").write_text(json.dumps({
        "python": platform.python_version(), "architecture": platform.machine(),
        "pyinstaller": package_version("pyinstaller"), "update_protocol": 1,
    }, sort_keys=True), encoding="utf-8")
    # Independent updater survives replacement of the main onedir bundle.
    helper_dir = staging / "updater"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
                    "--name", "CreatorHubUpdater", "--paths", str(ROOT), "--distpath", str(helper_dir),
                    "--workpath", str(staging / "updater-work"), "--specpath", str(staging),
                    str(ROOT / "desktop" / "update_helper.py")], cwd=ROOT, check=True)
    helper = helper_dir / "CreatorHubUpdater.exe"
    subprocess.run([str(helper), "--self-test"], check=True, timeout=30,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
               "--name", "CreatorHub", "--icon", str(staging / "CreatorHub.ico"),
               "--distpath", str(dist_root),
               "--workpath", str(staging / "work"), "--specpath", str(staging),
               "--paths", str(ROOT), "--hidden-import", "pystray._win32",
               "--collect-submodules", "uvicorn"]
    for package in ("app", "patchright", "xhshow", "curl_cffi", "imageio_ffmpeg", "tzdata", "webview", "clr_loader", "pythonnet"):
        command += ["--collect-all", package]
    if not (ROOT / "desktop" / "web" / "index.html").exists():
        raise SystemExit("Build the desktop renderer first: npm run build:desktop")
    for source, destination in ((ROOT / "config.example.yaml", "."),
                                (helper, "desktop"),
                                (ROOT / "desktop" / "languages" / "ChineseSimplified-LICENSE.txt", "desktop/licenses"),
                                (ROOT / "desktop" / "web", "desktop/web"),
                                (staging / "desktop-version.txt", "."),
                                (staging / "build-runtime.json", "."),
                                (staging / "desktop-guide", "desktop-guide")):
        command += ["--add-data", f"{source};{destination}"]
    command.append(str(ROOT / "desktop" / "launcher.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    print("Built:", output / "CreatorHub.exe")


if __name__ == "__main__":
    main()
