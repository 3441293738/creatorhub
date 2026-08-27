from __future__ import annotations

import html
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .github_api import StarData
from .themes import Palette


@dataclass(frozen=True)
class ChartOptions:
    width: int = 960
    height: int = 540
    style: str = "card"
    curve: str = "smooth"
    title: str = ""
    show_points: bool = False
    show_growth: bool = True
    animate: bool = True


def _compact(number: int) -> str:
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if number >= 1_000:
        return f"{number / 1_000:.1f}k".rstrip("0").rstrip(".")
    return str(number)


def _nice_ceiling(value: int) -> int:
    if value <= 5:
        return max(value, 5)
    power = 10 ** int(math.floor(math.log10(value)))
    fraction = value / power
    step = 2 if fraction <= 2 else 5 if fraction <= 5 else 10
    return step * power


def _history(data: StarData) -> list[tuple[datetime, int]]:
    if not data.stars:
        now = data.fetched_at
        return [(now - timedelta(days=1), 0), (now, data.total)]
    by_day = Counter(star.astimezone(timezone.utc).date() for star in data.stars)
    start = min(by_day)
    end = max(data.fetched_at.date(), max(by_day))
    result: list[tuple[datetime, int]] = []
    running = 0
    day = start
    while day <= end:
        running += by_day[day]
        result.append((datetime.combine(day, datetime.min.time(), timezone.utc), running))
        day += timedelta(days=1)
    if data.total > running:
        result[-1] = (result[-1][0], data.total)
    return result


def _sample(points: list[tuple[datetime, int]], maximum: int = 180) -> list[tuple[datetime, int]]:
    if len(points) <= maximum:
        return points
    stride = (len(points) - 1) / (maximum - 1)
    indexes = sorted({round(index * stride) for index in range(maximum)} | {0, len(points) - 1})
    return [points[index] for index in indexes]


def _line_path(coords: list[tuple[float, float]], curve: str) -> str:
    if not coords:
        return ""
    if curve == "step":
        segments = [f"M {coords[0][0]:.1f} {coords[0][1]:.1f}"]
        for x, y in coords[1:]:
            segments.append(f"H {x:.1f} V {y:.1f}")
        return " ".join(segments)
    if curve == "straight" or len(coords) < 3:
        return "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
    path = [f"M {coords[0][0]:.1f} {coords[0][1]:.1f}"]
    for index in range(1, len(coords)):
        x0, y0 = coords[index - 1]
        x1, y1 = coords[index]
        middle = (x0 + x1) / 2
        path.append(f"C {middle:.1f} {y0:.1f}, {middle:.1f} {y1:.1f}, {x1:.1f} {y1:.1f}")
    return " ".join(path)


def _date_label(moment: datetime, span_days: int) -> str:
    if span_days > 730:
        return moment.strftime("%Y")
    if span_days > 180:
        return moment.strftime("%Y/%m")
    return moment.strftime("%m/%d")


def render_svg(data: StarData, colors: Palette, mode: str, options: ChartOptions) -> str:
    if options.style not in {"card", "minimal", "glass", "neon"}:
        raise ValueError("style must be card, minimal, glass, or neon")
    if options.curve not in {"smooth", "straight", "step"}:
        raise ValueError("curve must be smooth, straight, or step")
    width, height = options.width, options.height
    pad = 42 if options.style != "minimal" else 24
    header = 116 if options.style != "minimal" else 76
    plot_left, plot_right = pad + 50, width - pad
    plot_top, plot_bottom = header + 30, height - pad - 36
    points = _sample(_history(data))
    start, end = points[0][0], points[-1][0]
    span = max((end - start).total_seconds(), 86400)
    maximum = _nice_ceiling(max(data.total, max(value for _, value in points), 1))
    coords = [
        (
            plot_left + ((moment - start).total_seconds() / span) * (plot_right - plot_left),
            plot_bottom - (value / maximum) * (plot_bottom - plot_top),
        )
        for moment, value in points
    ]
    line = _line_path(coords, options.curve)
    area = f"{line} L {coords[-1][0]:.1f} {plot_bottom:.1f} L {coords[0][0]:.1f} {plot_bottom:.1f} Z"
    title = html.escape(options.title or data.repo)
    description = html.escape(data.description[:90])
    surface_opacity = ".88" if options.style == "glass" else "1"
    radius = 24 if options.style in {"glass", "neon"} else 16
    outer = "" if options.style == "minimal" else f'<rect x="1" y="1" width="{width-2}" height="{height-2}" rx="{radius}" fill="{colors.surface}" fill-opacity="{surface_opacity}" stroke="{colors.border}"/>'
    grid: list[str] = []
    for index in range(5):
        ratio = index / 4
        y = plot_bottom - ratio * (plot_bottom - plot_top)
        value = round(maximum * ratio)
        grid.append(f'<line x1="{plot_left}" y1="{y:.1f}" x2="{plot_right}" y2="{y:.1f}" stroke="{colors.grid}"/>')
        grid.append(f'<text x="{plot_left-14}" y="{y+4:.1f}" class="axis" text-anchor="end">{_compact(value)}</text>')
    date_ticks: list[str] = []
    span_days = max((end - start).days, 1)
    for index in range(5):
        ratio = index / 4
        x = plot_left + ratio * (plot_right - plot_left)
        moment = start + (end - start) * ratio
        anchor = "start" if index == 0 else "end" if index == 4 else "middle"
        date_ticks.append(f'<text x="{x:.1f}" y="{plot_bottom+30}" class="axis" text-anchor="{anchor}">{_date_label(moment, span_days)}</text>')
    recent_cutoff = data.fetched_at - timedelta(days=7)
    recent = sum(1 for star in data.stars if star >= recent_cutoff)
    growth = f'+{recent} <tspan class="small">last 7 days</tspan>' if recent else 'steady <tspan class="small">last 7 days</tspan>'
    animation = ""
    if options.animate:
        animation = """
        .trace { stroke-dasharray: 2200; stroke-dashoffset: 2200; animation: draw 1.7s cubic-bezier(.16,1,.3,1) forwards; }
        .area { opacity: 0; animation: fade .8s .55s ease forwards; }
        @keyframes draw { to { stroke-dashoffset: 0; } }
        @keyframes fade { to { opacity: 1; } }
        @media (prefers-reduced-motion: reduce) { .trace { stroke-dashoffset:0; animation:none } .area { opacity:1; animation:none } }
        """
    glow_filter = ""
    line_filter = ""
    if options.style == "neon":
        glow_filter = f'<filter id="glow"><feGaussianBlur stdDeviation="5" result="blur"/><feFlood flood-color="{colors.glow}" flood-opacity=".7"/><feComposite in2="blur" operator="in"/><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
        line_filter = ' filter="url(#glow)"'
    points_svg = ""
    if options.show_points:
        points_svg = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{colors.surface}" stroke="{colors.accent}" stroke-width="2"/>' for x, y in coords[::max(1, len(coords)//20)])
    icon_x = pad
    header_text = f"""
      <g transform="translate({icon_x} {pad})">
        <rect width="42" height="42" rx="12" fill="url(#brand)"/>
        <path d="M21 9.5l3.5 7.1 7.8 1.1-5.7 5.5 1.4 7.8-7-3.7-7 3.7 1.4-7.8-5.7-5.5 7.8-1.1z" fill="#fff"/>
      </g>
      <text x="{pad+56}" y="{pad+22}" class="title">{title}</text>
      <text x="{pad+56}" y="{pad+45}" class="subtitle">{description or 'GitHub star growth'}</text>
      <text x="{width-pad}" y="{pad+22}" class="total" text-anchor="end">{data.total:,}</text>
      <text x="{width-pad}" y="{pad+45}" class="subtitle" text-anchor="end">total stars</text>
    """
    if options.style == "minimal":
        header_text = f'<text x="{pad}" y="{pad+20}" class="title">{title}</text><text x="{width-pad}" y="{pad+20}" class="total" text-anchor="end">★ {data.total:,}</text>'
    growth_svg = f'<text x="{plot_left}" y="{plot_top-14}" class="growth">{growth}</text>' if options.show_growth else ""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
  <title id="title">{title} Star History</title>
  <desc id="desc">{data.total} stars from {_date_label(start, span_days)} to {_date_label(end, span_days)}</desc>
  <defs>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="1"><stop stop-color="{colors.accent_2}"/><stop offset=".48" stop-color="{colors.accent_2}"/><stop offset=".52" stop-color="{colors.accent}"/><stop offset="1" stop-color="{colors.accent}"/></linearGradient>
    <linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop stop-color="{colors.accent}" stop-opacity=".30"/><stop offset="1" stop-color="{colors.accent}" stop-opacity=".025"/></linearGradient>
    {glow_filter}
    <style>
      text {{ font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; fill:{colors.text}; }}
      .title {{ font-size:22px; font-weight:700; }} .subtitle,.axis {{ fill:{colors.muted}; font-size:12px; }}
      .total {{ font-size:26px; font-weight:800; }} .growth {{ fill:{colors.accent}; font-size:13px; font-weight:700; }} .small {{ fill:{colors.muted}; font-weight:500; }}
      {animation}
    </style>
  </defs>
  <rect width="{width}" height="{height}" rx="{radius}" fill="{colors.background}"/>
  {outer}
  {header_text}
  {growth_svg}
  <g>{''.join(grid)}{''.join(date_ticks)}</g>
  <path class="area" d="{area}" fill="url(#area)"/>
  <path class="trace" d="{line}" fill="none" stroke="{colors.accent}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"{line_filter}/>
  {points_svg}
  <circle cx="{coords[-1][0]:.1f}" cy="{coords[-1][1]:.1f}" r="5" fill="{colors.surface}" stroke="{colors.accent}" stroke-width="3"/>
  <text x="{width-pad}" y="{height-15}" text-anchor="end" class="axis">generated by github-star-chart · {mode}</text>
</svg>'''
