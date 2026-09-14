"""CI-only retrieval of the latest published inventory; never publish/replace assets."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop.updates import fetch_release, fetch_manifest, numeric_version


def fetch_base(version, output):
    output = Path(output)
    # Explicit output is a single build file, not a directory or user-data path.
    output.unlink(missing_ok=True)
    try:
        info = fetch_release(version)
        if not info.get("manifest_asset") or numeric_version(info["version"]) >= numeric_version(version):
            print("No older published inventory: building full installer + new baseline.")
            return False
        _, raw = fetch_manifest(info)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
        print("Verified delta baseline:", info["version"])
        return True
    except (OSError, ValueError, TypeError, KeyError):
        print("Published baseline unavailable or unverified: full installer remains available.")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", default="build/windows/update-base.json")
    args = parser.parse_args()
    if numeric_version(args.version) is None:
        raise SystemExit("Numeric version required")
    fetch_base(args.version, args.output)
