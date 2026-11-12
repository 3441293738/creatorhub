"""Report actual artifact sizes and gate accidental heavyweight dependencies."""
import argparse
from collections import defaultdict
import json
from pathlib import Path


def package_report(bundle, installer=None):
    root = Path(bundle).resolve(strict=True)
    groups = defaultdict(int)
    largest = []
    forbidden = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        parts = relative.parts
        if any(part in {"cv2", "numpy", "numpy.libs", "PyQt5", "PyQt6", "PySide6"} for part in parts):
            forbidden.append(relative.as_posix())
        size = path.stat().st_size
        group = parts[1] if parts[0] == "_internal" and len(parts) > 1 else parts[0]
        groups[group] += size
        largest.append({"path": relative.as_posix(), "bytes": size})
    if not largest:
        raise ValueError("Bundle contains no files")
    result = {"schema": 1, "bundle_bytes": sum(groups.values()), "file_count": len(largest),
              "components": dict(sorted(groups.items(), key=lambda pair: -pair[1])),
              "largest_files": sorted(largest, key=lambda row: -row["bytes"])[:30],
              "forbidden_files": forbidden}
    if installer:
        result["installer_bytes"] = Path(installer).stat().st_size
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-installer-mib", type=float)
    parser.add_argument("--max-bundle-mib", type=float)
    args = parser.parse_args()
    report = package_report(args.bundle, args.installer)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Bundle: {report['bundle_bytes'] / 1024**2:.2f} MiB")
    if args.installer:
        print(f"Installer: {report['installer_bytes'] / 1024**2:.2f} MiB")
    if report["forbidden_files"]:
        raise SystemExit("Unused heavy dependencies were bundled; inspect the size report")
    for key, maximum in (("bundle_bytes", args.max_bundle_mib), ("installer_bytes", args.max_installer_mib)):
        if maximum is not None and (key not in report or report[key] > maximum * 1024**2):
            raise SystemExit(f"Size budget exceeded or missing artifact: {key}")


if __name__ == "__main__":
    main()
