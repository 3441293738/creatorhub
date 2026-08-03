from __future__ import annotations

import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "web"
PREVIEW = ROOT / "preview"


def build(destination: Path) -> None:
    destination = destination.resolve()
    if destination == ROOT or ROOT not in destination.parents:
        raise ValueError(f"preview destination must stay inside the repository: {destination}")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    html = (SOURCE / "index.html").read_text(encoding="utf-8")
    marker = '<script src="/static/app.js"></script>'
    replacement = '<script src="./demo-api.js"></script>\n<script src="./app.js"></script>'
    if marker not in html:
        raise RuntimeError("app script marker not found in app/web/index.html")
    (destination / "index.html").write_text(html.replace(marker, replacement), encoding="utf-8")
    shutil.copy2(SOURCE / "app.js", destination / "app.js")
    shutil.copy2(PREVIEW / "demo-api.js", destination / "demo-api.js")
    (destination / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site"
    build(target if target.is_absolute() else ROOT / target)
    print(f"Built static preview: {target}")
