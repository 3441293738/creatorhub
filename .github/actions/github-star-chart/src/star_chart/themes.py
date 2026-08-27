from __future__ import annotations

import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Palette:
    background: str
    surface: str
    surface_alt: str
    border: str
    text: str
    muted: str
    grid: str
    accent: str
    accent_2: str
    glow: str


THEMES: dict[str, dict[str, Palette]] = {
    "creatorhub": {
        "light": Palette("#fff8f7", "#ffffff", "#fff1ef", "#f1d8d3", "#251b1e", "#756369", "#f1e4e1", "#fe2c55", "#25f4ee", "#fe2c55"),
        "dark": Palette("#090b10", "#12161e", "#181d27", "#2a3341", "#f6f8fb", "#8b94a3", "#232b37", "#fe2c55", "#25f4ee", "#fe2c55"),
    },
    "github": {
        "light": Palette("#f6f8fa", "#ffffff", "#f6f8fa", "#d0d7de", "#1f2328", "#656d76", "#d8dee4", "#0969da", "#54aeff", "#0969da"),
        "dark": Palette("#0d1117", "#161b22", "#21262d", "#30363d", "#f0f6fc", "#8b949e", "#30363d", "#2f81f7", "#58a6ff", "#2f81f7"),
    },
    "ocean": {
        "light": Palette("#f0f9ff", "#ffffff", "#e0f2fe", "#bae6fd", "#0c4a6e", "#52758a", "#d5edf8", "#0284c7", "#22d3ee", "#0284c7"),
        "dark": Palette("#071a2b", "#0b253a", "#10334c", "#164e63", "#e0f2fe", "#7dd3fc", "#16405a", "#38bdf8", "#22d3ee", "#38bdf8"),
    },
    "sunset": {
        "light": Palette("#fff7ed", "#ffffff", "#ffedd5", "#fed7aa", "#431407", "#9a6751", "#fee2c5", "#f97316", "#ec4899", "#f97316"),
        "dark": Palette("#1c1012", "#28151a", "#351b22", "#57303a", "#fff7ed", "#fda4af", "#4b2730", "#fb923c", "#f472b6", "#fb923c"),
    },
    "forest": {
        "light": Palette("#f0fdf4", "#ffffff", "#dcfce7", "#bbf7d0", "#14532d", "#5c7c67", "#d4f0dd", "#16a34a", "#84cc16", "#16a34a"),
        "dark": Palette("#08160e", "#0d2115", "#153221", "#285238", "#ecfdf5", "#86c99e", "#20452f", "#34d399", "#a3e635", "#34d399"),
    },
    "lavender": {
        "light": Palette("#faf5ff", "#ffffff", "#f3e8ff", "#e9d5ff", "#3b0764", "#7c6590", "#eee3f5", "#8b5cf6", "#d946ef", "#8b5cf6"),
        "dark": Palette("#151020", "#1e172d", "#2b2140", "#493662", "#faf5ff", "#c4b5fd", "#3a2a50", "#a78bfa", "#e879f9", "#a78bfa"),
    },
    "mono": {
        "light": Palette("#f7f7f7", "#ffffff", "#eeeeee", "#d4d4d4", "#171717", "#737373", "#e5e5e5", "#262626", "#737373", "#525252"),
        "dark": Palette("#090909", "#141414", "#202020", "#353535", "#fafafa", "#a3a3a3", "#303030", "#e5e5e5", "#a3a3a3", "#d4d4d4"),
    },
}

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def palette(name: str, mode: str, accent: str = "", accent_2: str = "") -> Palette:
    key = name.lower()
    if key not in THEMES:
        raise ValueError(f"unknown theme {name!r}; choose: {', '.join(sorted(THEMES))}")
    result = THEMES[key][mode]
    if accent:
        if not HEX_COLOR.fullmatch(accent):
            raise ValueError("accent must be a six-digit hex color such as #fe2c55")
        result = replace(result, accent=accent, glow=accent)
    if accent_2:
        if not HEX_COLOR.fullmatch(accent_2):
            raise ValueError("accent-2 must be a six-digit hex color such as #25f4ee")
        result = replace(result, accent_2=accent_2)
    return result
