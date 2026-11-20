"""Deterministic brand graphics for desktop executables and Inno Setup wizard.

Renders CreatorHub's prism-play mark using fast Pillow primitives only (no SVG
or Cairo runtime), including multi-DPI wizard bitmaps for 100%-200% displays.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SMALL_SIZES = ((100, 58), (150, 97), (200, 124))
BANNER_SIZES = ((100, 164, 314), (150, 246, 471), (200, 328, 628))
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _lerp_color(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(round(c1[0] + (c2[0] - c1[0]) * t)),
        int(round(c1[1] + (c2[1] - c1[1]) * t)),
        int(round(c1[2] + (c2[2] - c1[2]) * t)),
    )


def _diagonal_gradient(width: int, height: int, top_c: tuple[int, int, int], bot_c: tuple[int, int, int]):
    """Create a smooth diagonal RGBA gradient using C-accelerated Pillow resize."""
    from PIL import Image

    grid = 64
    small = Image.new("RGBA", (grid, grid))
    px = small.load()
    for y in range(grid):
        ty = y / (grid - 1)
        for x in range(grid):
            tx = x / (grid - 1)
            t = ty * 0.8 + tx * 0.2
            px[x, y] = (*_lerp_color(top_c, bot_c, t), 255)
    return small.resize((width, height), Image.Resampling.BILINEAR)


def _ring_color(t: float, *, dark: bool = False) -> tuple[int, int, int]:
    cyan = (37, 244, 238) if dark else (6, 182, 212)
    blue = (94, 162, 255) if dark else (79, 124, 255)
    rose = (254, 44, 85)
    if t <= 0.5:
        return _lerp_color(cyan, blue, t / 0.5)
    return _lerp_color(blue, rose, (t - 0.5) / 0.5)


def _draw_rounded_triangle(draw, points: list[tuple[float, float]], radius: float, fill: tuple[int, int, int, int]):
    """Draw a triangle with rounded vertices matching the brand SVG play core."""
    inset = []
    count = len(points)
    for index in range(count):
        prev_pt = points[(index - 1) % count]
        curr_pt = points[index]
        next_pt = points[(index + 1) % count]
        v1 = (prev_pt[0] - curr_pt[0], prev_pt[1] - curr_pt[1])
        v2 = (next_pt[0] - curr_pt[0], next_pt[1] - curr_pt[1])
        len1 = math.hypot(*v1)
        len2 = math.hypot(*v2)
        u1 = (v1[0] / len1, v1[1] / len1)
        u2 = (v2[0] / len2, v2[1] / len2)
        bisector = (u1[0] + u2[0], u1[1] + u2[1])
        bis_len = math.hypot(*bisector)
        half_angle = math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))) / 2.0
        dist = radius / max(0.25, math.sin(half_angle))
        center = (curr_pt[0] + bisector[0] / bis_len * dist, curr_pt[1] + bisector[1] / bis_len * dist)
        tangent = radius / max(0.25, math.tan(half_angle))
        p1 = (curr_pt[0] + u1[0] * tangent, curr_pt[1] + u1[1] * tangent)
        p2 = (curr_pt[0] + u2[0] * tangent, curr_pt[1] + u2[1] * tangent)
        inset.append((center, p1, p2))

    polygon = []
    for center, p1, p2 in inset:
        polygon.extend([p1, p2])
        draw.ellipse((center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius), fill=fill)
    draw.polygon(polygon, fill=fill)


def _draw_prism_glyph(image, cx: float, cy: float, scale: float, *, dark: bool = False):
    """Draw the CreatorHub C-arc, play core and accent beacon centered at (cx, cy)."""
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(image)
    ring_r = 74.0 * scale
    stroke_r = 11.0 * scale
    start_deg = 42.0
    end_deg = 318.0
    steps = max(180, int((end_deg - start_deg) * 2.5))
    for step in range(steps + 1):
        t = step / steps
        angle = math.radians(start_deg + (end_deg - start_deg) * t)
        px = cx + ring_r * math.cos(angle)
        py = cy + ring_r * math.sin(angle)
        rgb = _ring_color(1.0 - t, dark=dark)
        draw.ellipse((px - stroke_r, py - stroke_r, px + stroke_r, py + stroke_r), fill=(*rgb, 255))

    tri_base = [
        (cx + (94.0 - 128.0) * scale, cy + (95.0 - 128.0) * scale),
        (cx + (155.0 - 128.0) * scale, cy + (128.0 - 128.0) * scale),
        (cx + (94.0 - 128.0) * scale, cy + (161.0 - 128.0) * scale),
    ]
    corner_r = 6.5 * scale
    if dark:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)
        _draw_rounded_triangle(
            ov_draw,
            [(x - 4.0 * scale, y) for x, y in tri_base],
            corner_r,
            (37, 244, 238, 92),
        )
        _draw_rounded_triangle(
            ov_draw,
            [(x + 4.0 * scale, y) for x, y in tri_base],
            corner_r,
            (254, 44, 85, 92),
        )
        image.alpha_composite(overlay)
        draw = ImageDraw.Draw(image)
        _draw_rounded_triangle(draw, tri_base, corner_r, (255, 255, 255, 255))
    else:
        _draw_rounded_triangle(draw, tri_base, corner_r, (20, 22, 28, 255))

    dot_x = cx + (198.0 - 128.0) * scale
    dot_y = cy
    outer_r = 15.0 * scale
    inner_r = 6.2 * scale
    beacon = (37, 244, 238, 255) if dark else (254, 44, 85, 255)
    draw.ellipse((dot_x - outer_r, dot_y - outer_r, dot_x + outer_r, dot_y + outer_r), fill=beacon)
    draw.ellipse((dot_x - inner_r, dot_y - inner_r, dot_x + inner_r, dot_y + inner_r), fill=(255, 255, 255, 255))


def render_brand_icon(size: int = 256, *, dark: bool = False):
    """Render an RGBA CreatorHub squircle icon at the requested pixel size."""
    from PIL import Image, ImageDraw

    canvas_size = max(512, min(1024, size * 4)) if size <= 256 else size * 2
    scale = canvas_size / 256.0
    bg = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))

    top_c = (23, 29, 43) if dark else (255, 255, 255)
    bot_c = (9, 12, 19) if dark else (238, 242, 248)
    grad = _diagonal_gradient(canvas_size, canvas_size, top_c, bot_c)

    mask = Image.new("L", (canvas_size, canvas_size), 0)
    pad = int(round(8 * scale))
    radius = int(round(56 * scale))
    ImageDraw.Draw(mask).rounded_rectangle(
        (pad, pad, canvas_size - 1 - pad, canvas_size - 1 - pad),
        radius=radius,
        fill=255,
    )
    bg.paste(grad, (0, 0), mask)

    border_color = (255, 255, 255, 42) if dark else (217, 223, 236, 255)
    border_layer = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    ImageDraw.Draw(border_layer).rounded_rectangle(
        (pad + int(scale), pad + int(scale), canvas_size - 1 - pad - int(scale), canvas_size - 1 - pad - int(scale)),
        radius=max(4, radius - int(scale)),
        outline=border_color,
        width=max(2, int(round(2.2 * scale))),
    )
    bg.alpha_composite(border_layer)

    _draw_prism_glyph(bg, canvas_size / 2.0, canvas_size / 2.0, scale, dark=dark)
    if size != canvas_size:
        return bg.resize((size, size), Image.Resampling.LANCZOS)
    return bg


def render_wizard_small_bmp(size: int):
    """Render a square RGB header mark on white (#ffffff) for Inno's modern header."""
    from PIL import Image

    base = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    icon_size = max(24, int(round(size * 0.86)))
    icon = render_brand_icon(icon_size, dark=False)
    offset = (size - icon_size) // 2
    base.alpha_composite(icon, (offset, offset))
    return base.convert("RGB")


def render_wizard_banner_bmp(width: int, height: int):
    """Render the vertical Welcome/Finished sidebar artwork for Inno Setup."""
    from PIL import Image, ImageDraw

    ss = 2
    w, h = width * ss, height * ss
    # Smooth ambient backdrop computed on a compact grid and bilinearly upsampled.
    gw, gh = 64, 128
    backdrop = Image.new("RGBA", (gw, gh))
    px = backdrop.load()
    top_bg = (19, 26, 42)
    bot_bg = (10, 13, 22)
    for y in range(gh):
        ny = y / (gh - 1)
        base_c = _lerp_color(top_bg, bot_bg, ny)
        for x in range(gw):
            nx = x / (gw - 1)
            d_cyan = math.hypot(nx - 0.22, (ny - 0.22) * 1.35)
            d_rose = math.hypot(nx - 0.82, (ny - 0.78) * 1.35)
            glow_c = max(0.0, 1.0 - d_cyan / 0.72) ** 2 * 0.16
            glow_r = max(0.0, 1.0 - d_rose / 0.72) ** 2 * 0.14
            px[x, y] = (
                min(255, int(base_c[0] + 37 * glow_c + 254 * glow_r)),
                min(255, int(base_c[1] + 244 * glow_c + 44 * glow_r)),
                min(255, int(base_c[2] + 238 * glow_c + 85 * glow_r)),
                255,
            )
    canvas = backdrop.resize((w, h), Image.Resampling.BILINEAR)

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx, cy = w * 0.5, h * 0.39
    for factor, alpha in ((0.44, 24), (0.60, 16), (0.76, 10)):
        orbit_r = w * factor
        draw.ellipse(
            (cx - orbit_r, cy - orbit_r, cx + orbit_r, cy + orbit_r),
            outline=(255, 255, 255, alpha),
            width=max(2, int(round(w * 0.006))),
        )

    for angle_deg, factor, color in (
        (-56, 0.60, (37, 244, 238, 190)),
        (132, 0.60, (94, 162, 255, 180)),
        (68, 0.76, (254, 44, 85, 185)),
    ):
        ang = math.radians(angle_deg)
        nx = cx + w * factor * math.cos(ang)
        ny = cy + w * factor * math.sin(ang)
        nr = max(4, w * 0.022)
        draw.ellipse((nx - nr * 2.1, ny - nr * 2.1, nx + nr * 2.1, ny + nr * 2.1), fill=(*color[:3], 38))
        draw.ellipse((nx - nr, ny - nr, nx + nr, ny + nr), fill=color)

    bar_y = int(h * 0.855)
    bar_h = max(4, int(round(w * 0.02)))
    total_w = int(w * 0.44)
    start_x = (w - total_w) // 2
    seg_gap = max(4, int(round(w * 0.025)))
    seg_w = (total_w - seg_gap * 2) // 3
    for idx, col in enumerate(((37, 244, 238, 175), (94, 162, 255, 165), (254, 44, 85, 175))):
        x0 = start_x + idx * (seg_w + seg_gap)
        draw.rounded_rectangle((x0, bar_y, x0 + seg_w, bar_y + bar_h), radius=bar_h // 2, fill=col)

    draw.line((w - 1, 0, w - 1, h), fill=(220, 226, 236, 55), width=max(2, ss))
    canvas.alpha_composite(overlay)

    emblem_size = int(round(w * 0.56))
    emblem = render_brand_icon(emblem_size, dark=True)
    canvas.alpha_composite(emblem, ((w - emblem_size) // 2, int(round(cy - emblem_size / 2))))

    return canvas.resize((width, height), Image.Resampling.LANCZOS).convert("RGB")


def write_installer_assets(output_dir: Path) -> dict[str, Path]:
    """Generate .ico and multi-DPI wizard .bmp assets in output_dir."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    ico_path = output_dir / "CreatorHub.ico"
    render_brand_icon(256, dark=False).save(ico_path, sizes=ICO_SIZES)
    written["icon"] = ico_path

    for dpi, dim in SMALL_SIZES:
        path = output_dir / f"installer-small-{dpi}.bmp"
        render_wizard_small_bmp(dim).save(path, format="BMP")
        written[f"small_{dpi}"] = path

    for dpi, width, height in BANNER_SIZES:
        path = output_dir / f"installer-banner-{dpi}.bmp"
        render_wizard_banner_bmp(width, height).save(path, format="BMP")
        written[f"banner_{dpi}"] = path

    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate CreatorHub Windows icon and Inno Setup wizard bitmaps.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build" / "windows")
    args = parser.parse_args(argv)
    paths = write_installer_assets(args.output_dir)
    print(f"Generated {len(paths)} installer brand assets in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
