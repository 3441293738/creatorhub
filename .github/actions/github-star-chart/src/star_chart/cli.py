from __future__ import annotations

import argparse
import os
from pathlib import Path

from .github_api import fetch_star_history, load_fixture
from .render import ChartOptions, render_svg
from .themes import THEMES, palette


def _bool(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Generate theme-aware GitHub star history SVG charts")
    value.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    value.add_argument("--token", default=os.getenv("GITHUB_TOKEN", ""))
    value.add_argument("--fixture", help="Read deterministic star data from JSON instead of GitHub")
    value.add_argument("--output-dir", default="assets")
    value.add_argument("--theme", choices=sorted(THEMES), default="creatorhub")
    value.add_argument("--style", choices=["card", "minimal", "glass", "neon"], default="card")
    value.add_argument("--curve", choices=["smooth", "straight", "step"], default="smooth")
    value.add_argument("--accent", default="")
    value.add_argument("--accent-2", default="")
    value.add_argument("--title", default="")
    value.add_argument("--width", type=int, default=960)
    value.add_argument("--height", type=int, default=540)
    value.add_argument("--show-points", type=_bool, default=False)
    value.add_argument("--show-growth", type=_bool, default=True)
    value.add_argument("--animate", type=_bool, default=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.width < 560 or args.height < 340:
        raise SystemExit("width must be >= 560 and height >= 340")
    if args.fixture:
        data = load_fixture(args.fixture)
    else:
        if not args.repo or not args.token:
            raise SystemExit("--repo and --token (or GITHUB_REPOSITORY/GITHUB_TOKEN) are required")
        data = fetch_star_history(args.repo, args.token)
    options = ChartOptions(args.width, args.height, args.style, args.curve, args.title, args.show_points, args.show_growth, args.animate)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for mode, filename in (("light", "star-history.svg"), ("dark", "star-history-dark.svg")):
        colors = palette(args.theme, mode, args.accent, args.accent_2)
        target = output / filename
        target.write_text(render_svg(data, colors, mode, options), encoding="utf-8")
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
