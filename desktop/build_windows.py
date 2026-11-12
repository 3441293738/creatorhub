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
from desktop.bundle_config import dependency_options, node_module_data


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
    output = (dist_root / "aispace").resolve()
    if not output.is_relative_to(ROOT.resolve()):
        raise SystemExit("Build output must remain inside the repository")
    from desktop.ui import brand_image
    brand_image(256).save(staging / "aispace.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    build_guide(staging / "desktop-guide")
    (staging / "desktop-version.txt").write_text(args.version, encoding="utf-8")
    (staging / "build-runtime.json").write_text(json.dumps({
        "python": platform.python_version(), "architecture": platform.machine(),
        "pyinstaller": package_version("pyinstaller"), "update_protocol": 1,
    }, sort_keys=True), encoding="utf-8")
    # Independent updater survives replacement of the main onedir bundle.
    helper_dir = staging / "updater"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
                    "--name", "aispaceUpdater", "--paths", str(ROOT), "--distpath", str(helper_dir),
                    "--workpath", str(staging / "updater-work"), "--specpath", str(staging),
                    str(ROOT / "desktop" / "update_helper.py")], cwd=ROOT, check=True)
    helper = helper_dir / "aispaceUpdater.exe"
    subprocess.run([str(helper), "--self-test"], check=True, timeout=30,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
               "--name", "aispace", "--icon", str(staging / "aispace.ico"),
               "--distpath", str(dist_root),
               "--workpath", str(staging / "work"), "--specpath", str(staging),
               "--paths", str(ROOT)]
    command += dependency_options("windows")
    if not (ROOT / "desktop" / "web" / "index.html").exists():
        raise SystemExit("Build the desktop renderer first: npm run build:desktop")
    for source, destination in ((ROOT / "config.example.yaml", "."),
                                (ROOT / "COMMERCIAL-NOTICE.txt", "."),
                                (ROOT / "app" / "brand.json", "app"),
                                (ROOT / "app" / "license_public.pem", "app"),
                                node_module_data(ROOT),
                                (helper, "desktop"),
                                (ROOT / "desktop" / "languages" / "ChineseSimplified-LICENSE.txt", "desktop/licenses"),
                                (ROOT / "desktop" / "web", "desktop/web"),
                                (staging / "desktop-version.txt", "."),
                                (staging / "build-runtime.json", "."),
                                (staging / "desktop-guide", "desktop-guide")):
        command += ["--add-data", f"{source};{destination}"]
    command.append(str(ROOT / "desktop" / "launcher.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    print("Built:", output / "aispace.exe")


if __name__ == "__main__":
    main()
