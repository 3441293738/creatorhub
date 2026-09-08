"""Offline layout contracts and contrast for every platform/theme palette."""
import colorsys
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app/web/appearance.css").read_text(encoding="utf-8")
HTML = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
LEGACY = json.loads((ROOT / "tests/fixtures/legacy-brand.json").read_text(encoding="utf-8"))


def tokens(selector):
    body = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", CSS).group(1)
    return dict(re.findall(r"--([\w-]+):\s*(#[\da-fA-F]{6})", body))


def contrast(a, b):
    def luminance(value):
        values = [int(value[n:n+2], 16) / 255 for n in (1, 3, 5)]
        values = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
        return sum(v * weight for v, weight in zip(values, (.2126, .7152, .0722)))
    low, high = sorted([luminance(a), luminance(b)])
    return (high + .05) / (low + .05)


def platform_palette(theme, platform):
    palette = tokens(":root")
    if theme == "dark":
        palette.update(tokens(':root[data-theme="dark"]'))
    if platform != "douyin":
        palette.update(tokens(f"body.pf-{platform}"))
        if theme == "dark":
            palette.update(tokens(f':root[data-theme="dark"] body.pf-{platform}'))
    return palette


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("platform", ["douyin", "xhs", "kuaishou", "shipinhao"])
def test_palette_readability(theme, platform):
    palette = platform_palette(theme, platform)
    for foreground in ("fg", "fg-strong", "mut", "mut-2", "acc", "info", "success", "warn", "danger", "chart-secondary"):
        for background in ("bg", "bg-grad", "surface", "surface-2", "surface-3", "surface-raised", "group-surface", "editor-bg", "control-bg"):
            assert contrast(palette[foreground], palette[background]) >= 4.5, (theme, platform, foreground, background)
    assert contrast(palette["on-accent"], palette["acc-solid"]) >= 4.5
    assert contrast(palette["on-accent"], palette["acc-hover"]) >= 4.5
    assert contrast(palette["control-line"], palette["control-bg"]) >= 3


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_brand_tones_follow_the_reference_not_an_invented_color_family(theme):
    def hue(value):
        h, _, s = colorsys.rgb_to_hls(*(int(value[n:n+2], 16) / 255 for n in (1, 3, 5)))
        assert s >= .4, "A neutral color is not a recognizable platform accent"
        return h * 360

    def distance(a, b):
        return abs((hue(a) - hue(b) + 180) % 360 - 180)

    for platform, reference in LEGACY["platforms"].items():
        palette = platform_palette(theme, platform)
        assert palette["brand"] == reference["accent"]
        for token in ("acc", "acc-solid", "acc-hover"):
            assert distance(palette[token], reference["accent"]) <= 12, (theme, platform, token)
    douyin, xhs = (platform_palette(theme, platform) for platform in ("douyin", "xhs"))
    assert distance(douyin["acc"], douyin["chart-secondary"]) >= 120
    for token in ("info", "success", "warn", "danger"):
        assert douyin[token] == xhs[token], (theme, token)
    # Restore legacy canvas identity, without making the platform force a theme.
    assert platform_palette("dark", "douyin")["bg"] == LEGACY["platforms"]["douyin"]["background"]
    assert platform_palette("light", "xhs")["bg"] == LEGACY["platforms"]["xhs"]["background"]
    for platform in LEGACY["platforms"]:
        assert contrast(platform_palette("light", platform)["bg"], platform_palette("dark", platform)["bg"]) >= 14


def test_original_brand_geometry_is_not_replaced_by_the_ui_icon_build():
    symbol = ET.fromstring(re.search(r'<symbol id="i-brand"[\s\S]*?</symbol>', HTML)[0])
    reference = ET.fromstring(LEGACY["symbol"])
    assert [{"tag": node.tag, **node.attrib} for node in symbol] == [{"tag": node.tag, **node.attrib} for node in reference]
    for attribute in ("viewBox", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin"):
        assert symbol.attrib[attribute] == reference.attrib[attribute]
    assert '<use href="#i-brand"/>' in HTML
    assert '<use href="#i-logo"/></svg><span>搜索功能</span>' in HTML
    assert 'readFile("frontend/brand.svg"' in (ROOT / "frontend/icons.mjs").read_text(encoding="utf-8")


def test_chart_series_and_legend_share_a_non_status_color():
    source = (ROOT / "app/web/app.js").read_text(encoding="utf-8")
    assert 'fill="var(--chart-secondary)"><title>${md(days[i])} · 评论' in source
    assert '.lg-b { background:var(--chart-secondary); }' in HTML


def test_appearance_boots_before_styles_and_platforms_do_not_own_theme():
    assert HTML.index('src="/static/appearance.js"') < HTML.index("<style>")
    assert 'body.pf-xhs { color-scheme: light; }' not in HTML
    assert "--bg:" not in HTML
    for theme in ("light", "dark", "system"):
        assert f'data-theme-choice="{theme}"' in HTML
        assert f'name="appearance-theme" value="{theme}"' in HTML
    assert 'name="appearance-density" value="compact"' in HTML
    assert 'id="appearance-motion"' in HTML
    assert "prefers-reduced-motion" in CSS
    assert 'html[data-motion="reduced"]' in CSS
    assert 'aria-controls="main-sidebar"' in HTML
    assert '<dialog class="command-dialog"' in HTML


def test_preview_includes_all_appearance_assets(tmp_path, monkeypatch):
    from preview import build_preview
    monkeypatch.setattr(build_preview, "ROOT", tmp_path)
    output = tmp_path / "site"
    build_preview.build(output)
    html = (output / "index.html").read_text(encoding="utf-8")
    assert "/static/" not in html
    assert html.index('./appearance.js') < html.index('<style>')
    for asset in ("appearance.js", "appearance.css", "workspace-ui.js", "workbench.js", "workbench.css", "workbench-licenses.txt", "engine-settings.js", "app.js"):
        assert (output / asset).read_bytes() == (ROOT / "app/web" / asset).read_bytes()


def test_local_icon_sprite_is_complete_and_consistent():
    source = (ROOT / "app/web/app.js").read_text(encoding="utf-8")
    symbols = set(re.findall(r'<symbol id="(i-[\w-]+)"', HTML))
    references = set(re.findall(r'href="#(i-[\w-]+)"', HTML + source))
    references.update(re.findall(r'ic\("(i-[\w-]+)"\)', source))
    references.update(re.findall(r'icon:\s*"(i-[\w-]+)"', source))
    assert references <= symbols, references - symbols
    assert 'data-icon-set="lucide"' in HTML
    assert len(symbols) == len(re.findall(r'<symbol id="i-', HTML))
    for attributes in re.findall(r'<symbol (id="i-[^"]+"[^>]+)>', HTML):
        if 'id="i-brand"' not in attributes:
            assert 'stroke-width="1.8"' in attributes
            assert 'viewBox="0 0 24 24"' in attributes
            assert 'fill="none"' in attributes
    licenses = (ROOT / "app/web/workbench-licenses.txt").read_text(encoding="utf-8")
    assert "lucide-static@" in licenses and "ISC License" in licenses
