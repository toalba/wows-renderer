# Brandon Theme — Design

**Status:** Approved — ready for implementation
**Date:** 2026-05-27

## Goal

Add a configurable color theme system to the renderer so users can pick a palette per render via a Discord slash-command option. Ship one alternative theme called **brandon** (cyan ally / magenta enemy) alongside the existing default (green ally / red enemy).

## Motivation

The current green/red palette was chosen for luminance-distinct colorblind safety. Some users prefer the cyan/pink scheme used in the in-game WoWs minimap. A second theme also lets us add more later (e.g. neon, high-contrast) without re-architecting.

## User-facing surface

Three slash commands gain a `theme` Choice option:

```
/render        ... theme: [Default | Brandon]   (default: Default)
/render_batch  ... theme: [Default | Brandon]
/render_dual   ... theme: [Default | Brandon]
```

When omitted, renders use the existing palette. Choosing **Brandon** recolors every team-tinted element in the output: ship icons, player names, HP bars, cap circles, score bar, killfeed lines, chat sender names, ribbons, achievements, projectile traces.

## Components

### `renderer/themes.py` (new)

Single source of truth for palettes.

```python
from dataclasses import dataclass

RGBA = tuple[float, float, float, float]
RGB = tuple[float, float, float]


@dataclass(frozen=True)
class Theme:
    name: str
    team_colors: dict[int, RGBA]               # 0=ally, 1=enemy
    self_color: RGBA = (1.0, 1.0, 1.0, 1.0)
    division_color: RGBA = (1.0, 0.84, 0.0, 1.0)
    contested_color: RGB = (1.0, 0.85, 0.0)    # cap contested ring


THEMES: dict[str, Theme] = {
    "default": Theme(
        name="default",
        team_colors={
            0: (0.36, 0.90, 0.51, 1.0),   # #5DE682
            1: (1.00, 0.42, 0.42, 1.0),   # #FF6B6B
        },
    ),
    "brandon": Theme(
        name="brandon",
        team_colors={
            0: (0x5B / 255, 0xCA / 255, 0xEC / 255, 1.0),  # #5BCAEC cyan
            1: (0xEB / 255, 0x47 / 255, 0xAB / 255, 1.0),  # #EB47AB magenta
        },
    ),
}
```

### `renderer/config.py` changes

Add two fields:

```python
theme: str = "default"
contested_color: RGB = (1.0, 0.85, 0.0)  # new — was a layer-local constant
```

`__post_init__()` resolves the theme:

```python
if self.theme not in THEMES:
    raise ValueError(f"Unknown theme: {self.theme!r}")
t = THEMES[self.theme]
self.team_colors = dict(t.team_colors)
self.self_color = t.self_color
self.division_color = t.division_color
self.contested_color = t.contested_color
```

If a caller passes both explicit `team_colors=` AND a non-default `theme=`, the theme wins (overwrites at the end of `__post_init__`). Acceptable: nothing in the codebase passes both today, and the slash command path never sets explicit colors.

### `renderer/layers/capture_points.py` changes

Replace the class constant with a config read.

```python
# Was:
CONTESTED_COLOR = (1.0, 0.85, 0.0)
# Now: read self.ctx.config.contested_color at call sites.
```

Sites that reference `CONTESTED_COLOR` (a small number; grep before edit) become `self.ctx.config.contested_color`.

### `bot/worker.py` changes

Both `render_replay` and `render_dual_replay` gain:

```python
theme: str = "default",
```

forwarded into `RenderConfig(..., theme=theme, ...)`.

### `bot/cog_render.py` changes

Add a module-level constant:

```python
THEME_CHOICES = [
    app_commands.Choice(name="Default — green/red", value="default"),
    app_commands.Choice(name="Brandon — cyan/magenta", value="brandon"),
]
```

Each of the three slash commands:
- Adds `theme=THEME_CHOICES` to `@app_commands.choices(...)`.
- Adds `theme: app_commands.Choice[str] | None = None` to the signature.
- Resolves `theme_value = theme.value if theme else "default"` and passes through to the worker call.

## Data flow

```
Discord user picks theme=Brandon
      |
      v
cog_render.py    theme_value = "brandon"
      |
      v
worker.py        render_replay(..., theme="brandon")
      |
      v
RenderConfig(theme="brandon")
   __post_init__ rewrites team_colors / contested_color from THEMES["brandon"]
      |
      v
Layers read self.ctx.config.team_colors as usual — no per-layer theme awareness
```

The key win: layers stay theme-unaware. Only RenderConfig knows about themes.

## Testing

One new unit test in `tests/test_render_config.py`:

```python
def test_brandon_theme_overrides_team_colors():
    from renderer.config import RenderConfig
    from renderer.themes import THEMES

    cfg = RenderConfig(theme="brandon")
    assert cfg.team_colors[0] == THEMES["brandon"].team_colors[0]
    assert cfg.team_colors[1] == THEMES["brandon"].team_colors[1]
    # Contested cap color is unchanged for Brandon (still amber).
    assert cfg.contested_color == (1.0, 0.85, 0.0)

def test_default_theme_matches_baseline():
    from renderer.config import RenderConfig

    cfg = RenderConfig()  # implicit theme="default"
    assert cfg.team_colors[0] == (0.36, 0.90, 0.51, 1.0)

def test_unknown_theme_raises():
    import pytest
    from renderer.config import RenderConfig

    with pytest.raises(ValueError, match="Unknown theme"):
        RenderConfig(theme="neon-purple")
```

Visual regression is covered by the nightly canary's Docker dry-run.

## Out of scope

- HUD score-bar background tint (neutral dark, not team-derived).
- Achievement icon tints (derived from team colors automatically).
- CLI flag for `render_quick.py` / `render_dual.py` — internal-use scripts.
- Per-user theme persistence — every render is explicit.
- Custom theme uploads — registry is static.

## Extensibility ceiling

Adding a third theme:
1. One entry in `renderer/themes.py::THEMES`.
2. One `app_commands.Choice` line in `bot/cog_render.py::THEME_CHOICES`.
3. (Optional) Add fields to `Theme` if it overrides something new (e.g. `hud_background`); then refactor that hardcoded site to read from config — same pattern as `contested_color`.
