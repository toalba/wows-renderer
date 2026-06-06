"""Color theme registry for the minimap renderer.

Each Theme overrides the team palette (and optionally accent colors) used
by RenderConfig. Layers stay theme-unaware — they read the resolved
config values, not the registry directly.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

RGBA = tuple[float, float, float, float]
RGB = tuple[float, float, float]


@dataclass(frozen=True)
class Theme:
    team_colors: Mapping[int, RGBA]  # 0 = ally, 1 = enemy
    self_color: RGBA = (1.0, 1.0, 1.0, 1.0)
    division_color: RGBA = (1.0, 0.84, 0.0, 1.0)
    contested_color: RGB = (1.0, 0.85, 0.0)  # amber

    def __post_init__(self) -> None:
        # Freeze team_colors so the registry can't be mutated through
        # THEMES[x].team_colors[k] = ... at runtime.
        if not isinstance(self.team_colors, MappingProxyType):
            object.__setattr__(self, "team_colors", MappingProxyType(dict(self.team_colors)))


THEMES: dict[str, Theme] = {
    "default": Theme(
        team_colors={
            0: (0.36, 0.90, 0.51, 1.0),  # #5DE682 (ally)
            1: (1.00, 0.42, 0.42, 1.0),  # #FF6B6B (enemy)
        },
    ),
    "brandon": Theme(
        team_colors={
            0: (0x5B / 255, 0xCA / 255, 0xEC / 255, 1.0),  # #5BCAEC cyan
            1: (0xEB / 255, 0x47 / 255, 0xAB / 255, 1.0),  # #EB47AB magenta
        },
    ),
}
