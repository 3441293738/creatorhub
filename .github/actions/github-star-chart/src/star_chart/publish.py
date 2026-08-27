from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--branch", default="star-history")
    parser.add_argument("--destination", default="assets")
    parser.add_argument("--message", default="chore: update star history chart")
    args = parser.parse_args()
    source = Path(args.source).resolve()
    if not source.is_dir() or not (source / "star-history.svg").is_file():
        raise SystemExit(f"invalid chart source: {source}")
    destination = Path(args.destination)
    if destination.is_absolute() or ".." in destination.parts or str(destination) in {"", "."}:
        raise SystemExit("destination must be a non-empty relative path without '..'")
    with tempfile.TemporaryDirectory(prefix="star-chart-publish-") as temp:
        saved = Path(temp) / "charts"
        shutil.copytree(source, saved)
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        run("git", "checkout", "--orphan", "__star_chart_publish")
        run("git", "rm", "-rf", "--ignore-unmatch", ".")
        destination.mkdir(parents=True, exist_ok=True)
        for file in saved.iterdir():
            shutil.copy2(file, destination / file.name)
        run("git", "add", "-f", str(destination))
        run("git", "commit", "-m", args.message)
        run("git", "push", "--force", "origin", f"HEAD:refs/heads/{args.branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
