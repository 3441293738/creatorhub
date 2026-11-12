"""Build a native-architecture .app/.dmg on macOS, optionally sign/notarize.

Unsigned builds are explicitly named UNTESTED and are never release assets.
Signed distribution requires an imported Developer ID certificate. Notarization
uses an existing keychain profile; secrets never enter command-line arguments.
"""
import argparse
import hashlib
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop.bundle_config import dependency_options, node_module_data
from desktop.release import version_key

MACHO_MAGIC = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}


def notarize(path, profile):
    command = ["xcrun", "notarytool", "submit", str(path), "--keychain-profile", profile,
               "--wait", "--timeout", "30m", "--output-format", "json"]
    if os.environ.get("MACOS_NOTARY_KEYCHAIN"):
        command += ["--keychain", os.environ["MACOS_NOTARY_KEYCHAIN"]]
    result = subprocess.run(command,
                            check=True, capture_output=True, text=True)
    if json.loads(result.stdout).get("status") != "Accepted":
        raise RuntimeError("Apple notarization did not accept this artifact; inspect the notarytool log")


def sign_bundle(bundle, identity):
    flags = ["codesign", "--force", "--sign", identity]
    if identity != "-":
        flags += ["--timestamp", "--options", "runtime"]
    for path in sorted(bundle.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        if path.is_file():
            with path.open("rb") as stream:
                binary = stream.read(4) in MACHO_MAGIC
            if binary:
                subprocess.run(flags + ["--entitlements", str(ROOT / "desktop/entitlements.macos.plist"), str(path)], check=True)
        elif path.suffix in {".framework", ".app", ".bundle"}:
            subprocess.run(flags + [str(path)], check=True)
    subprocess.run(flags + ["--entitlements", str(ROOT / "desktop/entitlements.macos.plist"), str(bundle)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", required=True, choices=("arm64", "x86_64"))
    parser.add_argument("--identity", default=os.environ.get("MACOS_SIGNING_IDENTITY", ""))
    parser.add_argument("--notary-profile", default="")
    args = parser.parse_args()
    version_key(args.version)
    if sys.platform != "darwin" or platform.machine().lower() != args.arch:
        raise SystemExit("Run on macOS with a Python interpreter matching --arch")
    if args.notary_profile and not args.identity:
        raise SystemExit("Notarization requires a Developer ID identity")
    from desktop.ui import brand_image
    from preview.build_preview import build_guide
    staging = ROOT / "build" / f"macos-{args.arch}"
    output = ROOT / "dist" / f"macos-{args.arch}"
    staging.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    brand_image(1024).save(staging / "aispace.icns")
    build_guide(staging / "desktop-guide")
    (staging / "desktop-version.txt").write_text(args.version, encoding="utf-8")
    (staging / "build-runtime.json").write_text(json.dumps({
        "python": platform.python_version(), "architecture": args.arch,
        "pyinstaller": package_version("pyinstaller"), "update_protocol": 1,
    }, sort_keys=True), encoding="utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
               "--name", "aispace", "--icon", str(staging / "aispace.icns"),
               "--distpath", str(output), "--workpath", str(staging / "work"), "--specpath", str(staging),
               "--paths", str(ROOT), "--target-arch", args.arch,
               "--osx-bundle-identifier", "io.github.creatorhub.desktop"]
    command += dependency_options("macos")
    for source, destination in ((ROOT / "config.example.yaml", "."), node_module_data(ROOT),
            (ROOT / "desktop/web", "desktop/web"), (staging / "desktop-version.txt", "."),
            (staging / "build-runtime.json", "."), (staging / "desktop-guide", "desktop-guide")):
        command += ["--add-data", f"{source}:{destination}"]
    command += [str(ROOT / "desktop/launcher.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    bundle = output / "aispace.app"
    plist_path = bundle / "Contents/Info.plist"
    with plist_path.open("rb") as stream:
        info = plistlib.load(stream)
    info.update(CFBundleShortVersionString=args.version, CFBundleVersion=args.version,
                NSHighResolutionCapable=True, LSMinimumSystemVersion="13.0")
    with plist_path.open("wb") as stream:
        plistlib.dump(info, stream)
    sign_bundle(bundle, args.identity or "-")
    subprocess.run([sys.executable, str(ROOT / "desktop/smoke_macos.py"), str(bundle)], check=True)
    if args.notary_profile:
        archive = staging / "notarization.zip"
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(bundle), str(archive)], check=True)
        notarize(archive, args.notary_profile)
        subprocess.run(["xcrun", "stapler", "staple", str(bundle)], check=True)
        subprocess.run(["xcrun", "stapler", "validate", str(bundle)], check=True)
        subprocess.run(["spctl", "--assess", "--type", "execute", "--verbose", str(bundle)], check=True)
    # Build the disk-image source without mutating a previously published bundle.
    import tempfile
    verified = bool(args.identity and args.notary_profile)
    prefix = "aispace-Setup" if verified else "aispace-UNTESTED"
    dmg = output / f"{prefix}-{args.version}-macos-{args.arch}.dmg"
    with tempfile.TemporaryDirectory(prefix="dmg-", dir=staging) as temp:
        shutil.copytree(bundle, Path(temp) / "aispace.app", symlinks=True)
        (Path(temp) / "Applications").symlink_to("/Applications", target_is_directory=True)
        subprocess.run(["hdiutil", "create", "-volname", "aispace", "-srcfolder", temp,
                        "-format", "UDZO", "-ov", str(dmg)], check=True)
    if args.identity:
        subprocess.run(["codesign", "--sign", args.identity, "--timestamp", str(dmg)], check=True)
    if args.notary_profile:
        notarize(dmg, args.notary_profile)
        subprocess.run(["xcrun", "stapler", "staple", str(dmg)], check=True)
        subprocess.run(["xcrun", "stapler", "validate", str(dmg)], check=True)
    with dmg.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / f"SHA256-macos-{args.arch}.txt").write_text(f"{digest}  {dmg.name}\n", encoding="ascii")
    print(f"Built: {dmg}")


if __name__ == "__main__":
    main()
