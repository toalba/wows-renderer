# Brandon Theme Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-render `theme` option to the Discord slash commands, shipping a second palette ("brandon" — cyan/magenta) alongside the existing default (green/red).

**Architecture:** Themes live in a static registry (`renderer/themes.py`). `RenderConfig` gains a `theme: str` field; its `__post_init__` resolves the name against the registry and overwrites `team_colors`, `self_color`, `division_color`, and a new `contested_color` field. Layers stay theme-unaware — they continue reading `config.team_colors` etc. The slash commands gain a `Choice` parameter that flows through the worker into `RenderConfig`.

**Tech Stack:** Python 3.12, dataclasses, discord.py (app_commands.Choice), pytest. Reference spec: `docs/superpowers/specs/2026-05-27-brandon-theme-design.md`.

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `renderer/themes.py` | Create | `Theme` dataclass + `THEMES` registry. Single source of truth for palettes. |
| `renderer/config.py` | Modify | Add `theme: str = "default"` and `contested_color` fields. Resolve theme in `__post_init__`. |
| `renderer/layers/capture_points.py` | Modify | Replace `CONTESTED_COLOR` class constant with a read from `self.ctx.config.contested_color`. |
| `bot/worker.py` | Modify | Accept `theme: str = "default"` kwarg on both `render_replay` and `render_dual_replay`; forward to `RenderConfig`. |
| `bot/cog_render.py` | Modify | Add `THEME_CHOICES`; wire `theme` Choice option into `/render`, `/render_batch`, `/render_dual`; pass value to worker. |
| `tests/test_render_config.py` | Create | Unit tests for theme resolution. |

Each task below is one self-contained commit.

---

### Task 1: Theme registry + RenderConfig integration (TDD)

**Files:**
- Create: `renderer/themes.py`
- Create: `tests/test_render_config.py`
- Modify: `renderer/config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_render_config.py` with the following content:

```python
"""Tests for RenderConfig theme resolution."""
from __future__ import annotations

import pytest

from renderer.config import RenderConfig
from renderer.themes import THEMES


def test_default_theme_preserves_baseline_team_colors():
    cfg = RenderConfig()  # implicit theme="default"
    assert cfg.theme == "default"
    assert cfg.team_colors[0] == (0.36, 0.90, 0.51, 1.0)
    assert cfg.team_colors[1] == (1.00, 0.42, 0.42, 1.0)


def test_default_theme_contested_color_is_amber():
    cfg = RenderConfig()
    assert cfg.contested_color == (1.0, 0.85, 0.0)


def test_brandon_theme_overrides_team_colors():
    cfg = RenderConfig(theme="brandon")
    assert cfg.theme == "brandon"
    assert cfg.team_colors[0] == THEMES["brandon"].team_colors[0]
    assert cfg.team_colors[1] == THEMES["brandon"].team_colors[1]
    # Brandon-specific RGB sanity check (cyan ally / magenta enemy).
    r, g, b, _ = cfg.team_colors[0]
    assert (r, g, b) == pytest.approx((0x5B / 255, 0xCA / 255, 0xEC / 255))
    r, g, b, _ = cfg.team_colors[1]
    assert (r, g, b) == pytest.approx((0xEB / 255, 0x47 / 255, 0xAB / 255))


def test_brandon_keeps_default_self_division_contested():
    cfg = RenderConfig(theme="brandon")
    assert cfg.self_color == (1.0, 1.0, 1.0, 1.0)
    assert cfg.division_color == (1.0, 0.84, 0.0, 1.0)
    assert cfg.contested_color == (1.0, 0.85, 0.0)


def test_unknown_theme_raises_value_error():
    with pytest.raises(ValueError, match="Unknown theme"):
        RenderConfig(theme="neon-purple")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_render_config.py -v`

Expected: All tests fail with `ModuleNotFoundError: No module named 'renderer.themes'`.

- [ ] **Step 3: Create the themes registry**

Create `renderer/themes.py`:

```python
"""Color theme registry for the minimap renderer.

Each Theme overrides the team palette (and optionally accent colors) used
by RenderConfig. Layers stay theme-unaware — they read the resolved
config values, not the registry directly.
"""
from __future__ import annotations

from dataclasses import dataclass

RGBA = tuple[float, float, float, float]
RGB = tuple[float, float, float]


@dataclass(frozen=True)
class Theme:
    name: str
    team_colors: dict[int, RGBA]  # 0 = ally, 1 = enemy
    self_color: RGBA = (1.0, 1.0, 1.0, 1.0)
    division_color: RGBA = (1.0, 0.84, 0.0, 1.0)
    contested_color: RGB = (1.0, 0.85, 0.0)  # amber


THEMES: dict[str, Theme] = {
    "default": Theme(
        name="default",
        team_colors={
            0: (0.36, 0.90, 0.51, 1.0),  # #5DE682 (ally)
            1: (1.00, 0.42, 0.42, 1.0),  # #FF6B6B (enemy)
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

- [ ] **Step 4: Wire theme resolution into RenderConfig**

In `renderer/config.py`:

**Edit 1** — add the new fields between `division_color` (line 40) and `hud_height` (line 42):

Replace:
```python
    division_color: tuple[float, float, float, float] = (1.0, 0.84, 0.0, 1.0)  # Gold yellow (division mates)

    hud_height: int = 24  # score bar above minimap
```

With:
```python
    division_color: tuple[float, float, float, float] = (1.0, 0.84, 0.0, 1.0)  # Gold yellow (division mates)
    contested_color: tuple[float, float, float] = (1.0, 0.85, 0.0)  # Amber by default; theme-overridable

    theme: str = "default"  # Looked up in renderer.themes.THEMES — overwrites team/self/division/contested at __post_init__ time.

    hud_height: int = 24  # score bar above minimap
```

**Edit 2** — append theme resolution at the end of `__post_init__` (after the existing `if not isinstance(self.gamedata_path, Path):` block, line 65-66). Append:

```python
        # Theme resolution — overwrites palette fields with the theme's
        # values. A non-default theme always wins over explicit colors
        # passed to RenderConfig() (nothing in the codebase does both).
        from renderer.themes import THEMES
        if self.theme not in THEMES:
            raise ValueError(f"Unknown theme: {self.theme!r} (known: {sorted(THEMES)})")
        t = THEMES[self.theme]
        self.team_colors = dict(t.team_colors)
        self.self_color = t.self_color
        self.division_color = t.division_color
        self.contested_color = t.contested_color
```

The import is done inside `__post_init__` to avoid a circular import at module-load time (themes.py is pure data so the cycle wouldn't actually trigger, but the local import keeps module-init order obvious).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_render_config.py -v`

Expected: All 5 tests pass.

- [ ] **Step 6: Run the full suite to confirm nothing else broke**

Run: `uv run pytest -x -q`

Expected: All tests pass (the existing suite is 26 passed / 5 skipped).

- [ ] **Step 7: Commit**

```bash
git add renderer/themes.py renderer/config.py tests/test_render_config.py
git commit -m "feat(themes): add Theme registry + RenderConfig.theme field

New renderer/themes.py holds a static THEMES registry with 'default'
(existing green/red) and 'brandon' (cyan/magenta). RenderConfig gains
a 'theme' string and a 'contested_color' field; __post_init__ resolves
the theme name and overwrites the palette fields so layers stay
theme-unaware."
```

---

### Task 2: Move contested-cap color from layer constant to config (TDD)

**Files:**
- Modify: `renderer/layers/capture_points.py:48` and `:305`

- [ ] **Step 1: Locate every CONTESTED_COLOR reference**

Run: `grep -n "CONTESTED_COLOR" renderer/layers/capture_points.py`

Expected output:
```
48:    CONTESTED_COLOR = (1.0, 0.85, 0.0)
305:                yr, yg, yb = self.CONTESTED_COLOR
```

(Two hits. If you see more, update them all in step 3 below.)

- [ ] **Step 2: Add a regression test that the color flows through**

Append to `tests/test_render_config.py`:

```python
def test_brandon_can_override_contested_color_in_future():
    """Smoke test: contested_color is mutable on the dataclass, so a
    future theme can override it without touching capture_points.py."""
    cfg = RenderConfig()
    cfg.contested_color = (0.0, 1.0, 0.0)  # green, for the test only
    assert cfg.contested_color == (0.0, 1.0, 0.0)
```

Run: `uv run pytest tests/test_render_config.py::test_brandon_can_override_contested_color_in_future -v`

Expected: PASS (we're just confirming the field is settable — no implementation change needed for this test alone, but it documents the contract that the next step relies on).

- [ ] **Step 3: Replace the class constant with a config read**

In `renderer/layers/capture_points.py`:

**Edit 1** — delete the class constant (line 48). Replace:

```python
    CONTESTED_COLOR = (1.0, 0.85, 0.0)
```

With (nothing — delete the line and any surrounding blank-line collapse Ruff complains about):

```python
```

**Edit 2** — at line 305, replace:

```python
                yr, yg, yb = self.CONTESTED_COLOR
```

With:

```python
                yr, yg, yb = self.ctx.config.contested_color
```

- [ ] **Step 4: Confirm no stale references remain**

Run: `grep -n "CONTESTED_COLOR" renderer/layers/capture_points.py`

Expected: no output.

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -x -q`

Expected: All tests pass. The existing golden-image / capture-point coverage exercises this code path with the unchanged amber color, so a regression would fire here.

- [ ] **Step 6: Lint check**

Run: `uv run ruff check renderer/layers/capture_points.py`

Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add renderer/layers/capture_points.py tests/test_render_config.py
git commit -m "refactor(capture_points): read contested color from config

Moves the amber CONTESTED_COLOR class constant out of CapturePointLayer
and into RenderConfig.contested_color so themes can override it. The
default value is unchanged (1.0, 0.85, 0.0)."
```

---

### Task 3: Worker accepts and forwards `theme` (TDD)

**Files:**
- Modify: `bot/worker.py` — both `render_replay` and `render_dual_replay`

- [ ] **Step 1: Add a worker contract test**

The worker imports cairo/pyav and is heavy to run, so test the *signature contract* without invoking it. Create `tests/test_worker_signature.py`:

```python
"""Light contract tests for bot.worker — verify the public function
signatures accept the new `theme` kwarg without actually rendering."""
from __future__ import annotations

import inspect

from bot.worker import render_dual_replay, render_replay


def test_render_replay_accepts_theme_kwarg():
    sig = inspect.signature(render_replay)
    assert "theme" in sig.parameters
    assert sig.parameters["theme"].default == "default"


def test_render_dual_replay_accepts_theme_kwarg():
    sig = inspect.signature(render_dual_replay)
    assert "theme" in sig.parameters
    assert sig.parameters["theme"].default == "default"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_worker_signature.py -v`

Expected: Both tests fail with `assert 'theme' in sig.parameters`.

- [ ] **Step 3: Add `theme` to `render_replay`**

In `bot/worker.py`, modify the `render_replay` signature. Find:

```python
def render_replay(
    replay_path: str,
    output_path: str,
    gamedata_path: str,
    progress_queue: Queue | None = None,
    *,
    preset: str = "full",
    speed: float = 20.0,
    fps: int = 20,
    minimap_size: int = 1080,
    panel_width: int = 420,
    flags: frozenset[str] = frozenset(),
) -> tuple[str, float, dict[str, float], str, int, str, list, str]:
```

Add `theme: str = "default",` after `flags`:

```python
def render_replay(
    replay_path: str,
    output_path: str,
    gamedata_path: str,
    progress_queue: Queue | None = None,
    *,
    preset: str = "full",
    speed: float = 20.0,
    fps: int = 20,
    minimap_size: int = 1080,
    panel_width: int = 420,
    flags: frozenset[str] = frozenset(),
    theme: str = "default",
) -> tuple[str, float, dict[str, float], str, int, str, list, str]:
```

Then find the `RenderConfig(` construction inside `render_replay` (the one with `gamedata_path=vgd.version_dir / "data"`) and append `theme=theme,` as the last argument before the closing `)`. The block becomes:

```python
    config = RenderConfig(
        gamedata_path=vgd.version_dir / "data",
        versioned_gamedata=vgd,
        speed=speed,
        fps=fps,
        minimap_size=minimap_size,
        panel_width=panel_width,
        left_panel_width=left_pw,
        right_panel_width=right_pw,
        flags=flags,
        theme=theme,
    )
```

- [ ] **Step 4: Add `theme` to `render_dual_replay`**

Same shape. Find the `render_dual_replay` signature and append `theme: str = "default",` after `flags`. Find its `RenderConfig(` construction and append `theme=theme,` before the closing `)`. The block becomes:

```python
    config = RenderConfig(
        gamedata_path=vgd.version_dir / "data",
        versioned_gamedata=vgd,
        speed=speed,
        fps=fps,
        minimap_size=minimap_size,
        panel_width=panel_width,
        flags=flags,
        theme=theme,
    )
```

- [ ] **Step 5: Verify tests pass**

Run: `uv run pytest tests/test_worker_signature.py -v`

Expected: Both tests pass.

- [ ] **Step 6: Full suite + lint**

Run: `uv run pytest -x -q && uv run ruff check bot/worker.py`

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add bot/worker.py tests/test_worker_signature.py
git commit -m "feat(worker): accept and forward 'theme' kwarg

Both render_replay and render_dual_replay now take theme=\"default\"
and pass it to RenderConfig(theme=...). Adds a light signature test."
```

---

### Task 4: Slash commands expose `theme` Choice

**Files:**
- Modify: `bot/cog_render.py` (three commands: `render`, `render_batch`, `render_dual`)

This is the only task without an automated test — discord.py interactions can't be unit-tested without a heavy fixture harness. The verification step is manual: restart the bot, see the option appear in Discord. Lint catches structural mistakes.

- [ ] **Step 1: Define the choices constant**

In `bot/cog_render.py`, add a module-level constant near `KNOWN_FLAGS` (around line 33). After:

```python
KNOWN_FLAGS = frozenset({"anonymize"})
```

Add:

```python
THEME_CHOICES = [
    app_commands.Choice(name="Default — green/red", value="default"),
    app_commands.Choice(name="Brandon — cyan/magenta", value="brandon"),
]
```

- [ ] **Step 2: Wire `theme` into `/render`**

Find the `render` command. There are three edits to make.

**Edit 2a** — `@app_commands.describe(...)`: add a `theme=` entry. The current block is:

```python
    @app_commands.describe(
        replay="Upload a .wowsreplay file",
        preset="Render preset (default: full)",
        flags="Comma-separated flags. Available: anonymize",
    )
```

Make it:

```python
    @app_commands.describe(
        replay="Upload a .wowsreplay file",
        preset="Render preset (default: full)",
        theme="Color theme (default: Default)",
        flags="Comma-separated flags. Available: anonymize",
    )
```

**Edit 2b** — the `@app_commands.choices(...)` decorator currently only sets `preset=[...]`. Extend it to also set `theme=THEME_CHOICES`. Replace:

```python
    @app_commands.choices(preset=[
        app_commands.Choice(name="Full — all layers + both panels", value="full"),
        app_commands.Choice(name="Map — minimap only, no panels", value="map"),
        app_commands.Choice(name="Player data — minimap + killfeed/ribbons", value="playerdata"),
    ])
```

With:

```python
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="Full — all layers + both panels", value="full"),
            app_commands.Choice(name="Map — minimap only, no panels", value="map"),
            app_commands.Choice(name="Player data — minimap + killfeed/ribbons", value="playerdata"),
        ],
        theme=THEME_CHOICES,
    )
```

**Edit 2c** — the `render` function signature. Replace:

```python
    async def render(
        self,
        interaction: discord.Interaction,
        replay: discord.Attachment,
        preset: app_commands.Choice[str] | None = None,
        flags: str | None = None,
    ) -> None:
```

With:

```python
    async def render(
        self,
        interaction: discord.Interaction,
        replay: discord.Attachment,
        preset: app_commands.Choice[str] | None = None,
        theme: app_commands.Choice[str] | None = None,
        flags: str | None = None,
    ) -> None:
```

**Edit 2d** — resolve the value at the top of the body (alongside `preset_value` and `flag_set`). Find:

```python
        preset_value = preset.value if preset else "full"
        flag_set = _parse_flags(flags)
```

Replace with:

```python
        preset_value = preset.value if preset else "full"
        theme_value = theme.value if theme else "default"
        flag_set = _parse_flags(flags)
```

**Edit 2e** — pass `theme=theme_value` into `render_replay`. Find:

```python
            render_call = functools.partial(
                render_replay,
                str(replay_path),
                str(output_path),
                str(cfg.gamedata_path),
                progress_queue,
                preset=preset_value,
                speed=cfg.render_speed,
                fps=cfg.render_fps,
                minimap_size=cfg.minimap_size,
                panel_width=cfg.panel_width,
                flags=flag_set,
            )
```

Add `theme=theme_value,` after `flags=flag_set,`:

```python
            render_call = functools.partial(
                render_replay,
                str(replay_path),
                str(output_path),
                str(cfg.gamedata_path),
                progress_queue,
                preset=preset_value,
                speed=cfg.render_speed,
                fps=cfg.render_fps,
                minimap_size=cfg.minimap_size,
                panel_width=cfg.panel_width,
                flags=flag_set,
                theme=theme_value,
            )
```

**Edit 2f** — the log line below the "/render start:" message includes `flags`. Append `theme=%s` and `theme_value`. Find:

```python
        log.info(
            "/render start: user=%s guild=%s replay=%s size=%.1fMB preset=%s flags=%s",
            interaction.user.id, interaction.guild_id,
            replay.filename, replay.size / 1024 / 1024, preset_value,
            sorted(flag_set) or "—",
        )
```

Replace with:

```python
        log.info(
            "/render start: user=%s guild=%s replay=%s size=%.1fMB preset=%s theme=%s flags=%s",
            interaction.user.id, interaction.guild_id,
            replay.filename, replay.size / 1024 / 1024, preset_value, theme_value,
            sorted(flag_set) or "—",
        )
```

- [ ] **Step 3: Wire `theme` into `/render_batch`**

Same shape. Apply edits 2a → 2f to `render_batch`. Specifically:

- Add `theme="Color theme (default: Default)",` to its `@app_commands.describe(...)`.
- Extend its `@app_commands.choices(...)` with `theme=THEME_CHOICES`.
- Add `theme: app_commands.Choice[str] | None = None,` to the signature (after `preset`, before `flags`).
- Resolve `theme_value = theme.value if theme else "default"` at the top of the body (after `preset_value = preset.value if preset else "full"`).
- The batch dispatches via `self._render_one_for_batch(...)`. Update that helper's signature too:

  Find:
  ```python
      async def _render_one_for_batch(
          self,
          item: _BatchItem,
          preset_value: str,
          timeout: float,
          semaphore: asyncio.Semaphore,
          flag_set: frozenset[str] = frozenset(),
      ) -> _BatchResult:
  ```

  Replace with:
  ```python
      async def _render_one_for_batch(
          self,
          item: _BatchItem,
          preset_value: str,
          timeout: float,
          semaphore: asyncio.Semaphore,
          flag_set: frozenset[str] = frozenset(),
          theme_value: str = "default",
      ) -> _BatchResult:
  ```

  Then in its body, find the `functools.partial(render_replay, ...)` block and add `theme=theme_value,` after `flags=flag_set,`.

- In `render_batch`, find the `asyncio.create_task(self._render_one_for_batch(item, preset_value, per_replay_timeout, semaphore, flag_set))` call and add `theme_value` as the last positional argument:

  ```python
          tasks = [
              asyncio.create_task(
                  self._render_one_for_batch(item, preset_value, per_replay_timeout, semaphore, flag_set, theme_value),
              )
              for item in items
          ]
  ```

- The `[BATCH]` log at the bottom includes `preset=%s`. Append `theme=%s` and pass `theme_value`.

- [ ] **Step 4: Wire `theme` into `/render_dual`**

Apply the same edits to `render_dual`:

- Add `theme="Color theme (default: Default)"` to its `@app_commands.describe(...)`.
- Add `@app_commands.choices(theme=THEME_CHOICES)` decorator (currently has no `@app_commands.choices` — add it between `@app_commands.describe(...)` and `@app_commands.checks.dynamic_cooldown(_batch_cooldown)`).
- Add `theme: app_commands.Choice[str] | None = None,` to the signature (after `flags`).

  Wait — `render_dual` currently has `flags` as the last param. Reorder: add `theme` *before* `flags` so the param order is consistent across commands. Replace:

  ```python
      async def render_dual(
          self,
          interaction: discord.Interaction,
          replay1: discord.Attachment,
          replay2: discord.Attachment,
          flags: str | None = None,
      ) -> None:
  ```

  With:

  ```python
      async def render_dual(
          self,
          interaction: discord.Interaction,
          replay1: discord.Attachment,
          replay2: discord.Attachment,
          theme: app_commands.Choice[str] | None = None,
          flags: str | None = None,
      ) -> None:
  ```

- Resolve `theme_value = theme.value if theme else "default"` at the top of the body (after `flag_set = _parse_flags(flags)`).
- Find the `functools.partial(render_dual_replay, ...)` block and add `theme=theme_value,` after `flags=flag_set,`.
- Update the "/render_dual start:" log line to include `theme=%s` and `theme_value` (same shape as edit 2f).

- [ ] **Step 5: Lint check**

Run: `uv run ruff check bot/cog_render.py`

Expected: `All checks passed!`

- [ ] **Step 6: Full test suite**

Run: `uv run pytest -x -q`

Expected: all green (the new theme tests still pass; nothing else regressed).

- [ ] **Step 7: Manual verification with the running test bot**

If the test bot from the previous session isn't running, start it:

```bash
DISCORD_TOKEN='<your-test-token>' ENABLE_BUILD_URLS=true \
  uv --directory /home/toalba/projects/wows/wows-renderer \
  run python -m bot.main > /tmp/wows-bot.log 2>&1 &
```

Wait for `Logged in as ...` in `/tmp/wows-bot.log`, then in your test Discord server:

1. Type `/render` and confirm the `theme` dropdown appears with **Default — green/red** and **Brandon — cyan/magenta**.
2. Upload a `.wowsreplay` with `theme: Brandon`. Verify the rendered video uses cyan for ally and magenta for enemy ships, names, caps, score bar, killfeed.
3. Repeat with `theme: Default` (or no `theme` arg) → confirm the original green/red palette.
4. Spot-check `/render_dual` and `/render_batch` show the dropdown.

If anything looks wrong, check `/tmp/wows-bot.log` for the new `theme=` value in the start log line.

- [ ] **Step 8: Commit**

```bash
git add bot/cog_render.py
git commit -m "feat(bot): expose 'theme' Choice on /render, /render_batch, /render_dual

Users can now pick 'Default — green/red' (existing palette) or
'Brandon — cyan/magenta' from a slash-command dropdown. The choice
flows through worker → RenderConfig → layers without per-layer
theme awareness."
```

---

## Self-Review

**Spec coverage:**
- User-facing surface (theme Choice on three commands) → Task 4.
- `renderer/themes.py` registry → Task 1 step 3.
- `RenderConfig` field additions + resolution → Task 1 step 4.
- `capture_points.py` refactor → Task 2.
- Worker plumbing → Task 3.
- Unit tests for theme resolution → Task 1 step 1.
- Out-of-scope items (HUD background, achievement tints, CLI flags, custom uploads) → correctly not in any task.

**Placeholder scan:** No TBD/TODO/"similar to". Every code block is complete and self-contained.

**Type consistency:** `Theme.contested_color` is `RGB` (3-tuple) matching the layer's `yr, yg, yb = ...` unpack at `capture_points.py:305`. `team_colors` is `dict[int, RGBA]` everywhere. `theme: str` everywhere.

**Spec gap?** The spec calls for `tests/test_render_config.py` with three tests; this plan ships five (added `test_brandon_keeps_default_self_division_contested` and `test_brandon_can_override_contested_color_in_future` for tighter coverage). No spec requirement is missing a task.
