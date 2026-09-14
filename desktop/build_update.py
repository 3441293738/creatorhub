"""Publish a complete inventory and, optionally, a file delta from one stable base.

The normal Inno installer remains mandatory. No installed files/accounts are read.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop.update_manifest import (changes, delta_name, hash_file, inventory,
    manifest_name, parse_manifest, validate_manifest, version_tuple)


def build_update(app_dir, output_dir, version, base=None):
    app_dir, output_dir = Path(app_dir).resolve(), Path(output_dir).resolve()
    version_tuple(version)
    if (app_dir / "_internal/desktop-version.txt").read_text(encoding="utf-8").strip() != version:
        raise ValueError("Bundle version does not match release")
    if output_dir.is_relative_to(app_dir):
        raise ValueError("Release output must be outside the application bundle")
    output_dir.mkdir(parents=True, exist_ok=True)
    files = inventory(app_dir)
    # An interpreter/bootloader change uses the full installer. Dependency DLLs
    # can otherwise be updated as ordinary files in a complete verified tree.
    runtime = [f for f in files if f["path"] == "_internal/build-runtime.json"
               or (f["path"].startswith("_internal/python") and f["path"].endswith(".dll"))]
    if not runtime:
        raise ValueError("Bundle has no runtime identity")
    data = {"schema": 1, "platform": "windows-x64", "version": version, "files": files,
            "runtime_id": hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest()}
    if base:
        base = parse_manifest(Path(base).read_bytes()) if isinstance(base, (str, Path)) else validate_manifest(base)
        if version_tuple(base["version"]) >= version_tuple(version):
            raise ValueError("Delta base must precede this release")
        if base["runtime_id"] == data["runtime_id"]:
            changed, _ = changes(base["files"], files)
            package = output_dir / delta_name(base["version"], version)
            with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for entry in changed:
                    # Fixed timestamps/permissions keep identical input reproducible.
                    item = zipfile.ZipInfo(entry["path"], (2020, 1, 1, 0, 0, 0))
                    item.compress_type = zipfile.ZIP_DEFLATED
                    item.external_attr = 0o100644 << 16
                    with (app_dir / entry["path"]).open("rb") as source, archive.open(item, "w") as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
            data["delta"] = {"from_version": base["version"], "base_files": base["files"],
                "asset_name": package.name, "size": package.stat().st_size, "sha256": hash_file(package)}
    validate_manifest(data)
    (output_dir / manifest_name(version)).write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    # Include only this version's release artifacts, never stale packages or logs.
    names = [f"CreatorHub-Setup-{version}-windows-x64.exe", manifest_name(version)]
    if data.get("delta"):
        names.append(data["delta"]["asset_name"])
    present = [output_dir / name for name in names if (output_dir / name).is_file()]
    (output_dir / "SHA256.txt").write_text("".join(f"{hash_file(p)}  {p.name}\n" for p in present), encoding="ascii")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", default="dist/windows/CreatorHub")
    parser.add_argument("--output-dir", default="dist/installer")
    parser.add_argument("--version", default="0.2.0")
    parser.add_argument("--base-manifest")
    args = parser.parse_args()
    base = args.base_manifest if args.base_manifest and Path(args.base_manifest).is_file() else None
    result = build_update(args.app_dir, args.output_dir, args.version, base)
    print("Update inventory:", manifest_name(args.version))
    print("Delta:", result.get("delta", {}).get("asset_name", "none (initial release or different runtime)"))


if __name__ == "__main__":
    main()
