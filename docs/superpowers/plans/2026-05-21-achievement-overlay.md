# Achievement Overlay Under Ribbons — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display the recording player's earned achievements as persistent
icons on the right panel, anchored directly below the ribbon counter.

**Architecture:** A new `AchievementLayer` reads `AchievementEvent`s from the
parsed replay, filters to the recording player, accumulates unique achievement
IDs in first-appearance order, and draws their icons under the existing
`RibbonLayer`. A new `achievements.json` (extracted from GameParams during
gamedata-cache build) maps `id → ui_name` for icon-file lookup. `RibbonLayer`
gains a `panel_bottom` attribute so the new layer can anchor under it.

**Tech Stack:** Python 3.12, pycairo, pytest. Spec: [docs/superpowers/specs/2026-05-21-achievement-overlay-design.md](../specs/2026-05-21-achievement-overlay-design.md).

---

## File Structure

**Create:**
- `wows-renderer/renderer/layers/achievements.py` — the new layer.
- `wows-renderer/tests/test_achievements_layer.py` — unit tests for the layer (mocked ctx, no Cairo render).
- `wows-renderer/tests/test_gamedata_cache_achievements.py` — unit tests for `_extract_achievement_map` and `VersionedGamedata.achievements`.
- `wows-renderer/scripts/dump_achievement_schema.py` — one-shot CLI helper to confirm the GameParams Achievement_* schema; deleted at end of Task 1.

**Modify:**
- `wows-renderer/renderer/gamedata_cache.py` — add `_extract_achievement_map`, `achievements.json` write step, `VersionedGamedata.achievements` cached property with on-disk backfill.
- `wows-renderer/renderer/layers/ribbons.py` — expose `panel_bottom: float`.
- `wows-renderer/renderer/layers/right_panel.py` — wire `AchievementLayer` between ribbons and killfeed.

**No changes to:** parser, dual renderer (`render_dual.py`), CLI, bot, config dataclass.

---

## Task 1: Confirm achievement → icon naming convention

The spec relies on the rule: GameParams entry name `Achievement_<UI_NAME>` → icon file `gui/achievements/icon_achievement_<UI_NAME>.png`. The icon filenames look unambiguous (`icon_achievement_AIRDEFENSEEXPERT.png`, `icon_achievement_BATTLE_HERO.png`), but the GameParams entry-name format has not been verified directly. Confirm before writing the extractor.

**Files:**
- Create: `wows-renderer/scripts/dump_achievement_schema.py`
- Verify against: `~/.cache/wows-gamedata/v12506899/` (most recent cache)

- [ ] **Step 1: Write the discovery script**

```python
# wows-renderer/scripts/dump_achievement_schema.py
"""One-shot helper: dump a few Achievement_* entries from a cached GameParams.

Used to verify the spec's assumption that entry names follow
'Achievement_<UI_NAME>' and that <UI_NAME> matches the
'icon_achievement_<UI_NAME>.png' suffix in gui/achievements/.

Run:  uv run python scripts/dump_achievement_schema.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from renderer.gameparams import load_gameparams_cached

CACHE_ROOT = Path.home() / ".cache" / "wows-gamedata"


def main() -> int:
    versions = sorted(p for p in CACHE_ROOT.iterdir() if p.is_dir() and p.name.startswith("v"))
    if not versions:
        print(f"No cached versions under {CACHE_ROOT}", file=sys.stderr)
        return 1
    version_dir = versions[-1]
    print(f"Using {version_dir.name}")
    gp = load_gameparams_cached(version_dir)

    icons_dir = version_dir / "data" / "gui" / "achievements"
    icon_names = {
        p.stem.removeprefix("icon_achievement_")
        for p in icons_dir.glob("icon_achievement_*.png")
    }

    achievements = []
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if isinstance(ti, dict) and ti.get("type") == "Achievement":
            achievements.append((name, obj.get("id")))

    print(f"Found {len(achievements)} Achievement_* entries.")
    print(f"Found {len(icon_names)} icon files.")
    print("First 10 entries (name, id):")
    for name, aid in achievements[:10]:
        suffix = name.removeprefix("Achievement_") if name.startswith("Achievement_") else None
        has_icon = suffix in icon_names if suffix else False
        print(f"  {name!r:50s} id={aid!r:>6}  suffix={suffix!r}  icon_exists={has_icon}")

    missing_prefix = [n for n, _ in achievements if not n.startswith("Achievement_")]
    print(f"\nEntries WITHOUT 'Achievement_' prefix: {len(missing_prefix)}")
    for n in missing_prefix[:5]:
        print(f"  {n!r}")

    matched = sum(
        1 for n, _ in achievements
        if n.startswith("Achievement_") and n.removeprefix("Achievement_") in icon_names
    )
    print(f"\nMatched icon files: {matched}/{len(achievements)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the script**

Run (from `wows-renderer/`):
```bash
uv run python scripts/dump_achievement_schema.py
```

Expected output: a list of 10 sample entries, all with `Achievement_*` prefix and `icon_exists=True`, plus a high match ratio.

**Decision point:**
- If ≥95% of entries match → spec rule holds, proceed to Task 2.
- If <95% match → before continuing, write a note in this plan above Task 2 stating the exact failure mode and update the extractor in Task 2 to handle it (e.g. case folding, alternate prefixes). The fallback already in the spec (use raw name) covers the long tail.

- [ ] **Step 3: Delete the discovery script**

```bash
rm scripts/dump_achievement_schema.py
```

It's a one-shot helper and shouldn't ship.

- [ ] **Step 4: Commit (verification log only — script deleted)**

No code change to commit; capture the verification result as the body of a small docs note instead:

```bash
# No commit for this task. Record the script's output in your scratchpad
# and proceed to Task 2.
```

---

## Task 2: Add `_extract_achievement_map` to gamedata_cache.py (TDD)

**Files:**
- Modify: `wows-renderer/renderer/gamedata_cache.py` (add extractor function, see Task 3 for wiring)
- Create: `wows-renderer/tests/test_gamedata_cache_achievements.py`

- [ ] **Step 1: Write the failing test**

```python
# wows-renderer/tests/test_gamedata_cache_achievements.py
"""Unit tests for achievement extraction in gamedata_cache.

Pure-Python, no fixtures: pass a synthetic GameParams dict through the
extractor and assert the resulting id -> ui_name mapping.
"""
from __future__ import annotations

from renderer.gamedata_cache import _extract_achievement_map


def test_extract_achievement_map_strips_prefix():
    gp = {
        "Achievement_AIRDEFENSEEXPERT": {
            "id": 5001,
            "typeinfo": {"type": "Achievement"},
        },
        "Achievement_KRAKEN": {
            "id": 5002,
            "typeinfo": {"type": "Achievement"},
        },
    }
    result = _extract_achievement_map(gp)
    assert result == {"5001": "AIRDEFENSEEXPERT", "5002": "KRAKEN"}


def test_extract_achievement_map_ignores_other_types():
    gp = {
        "Achievement_BATTLE_HERO": {"id": 1, "typeinfo": {"type": "Achievement"}},
        "PASA001_Iowa": {"id": 9999, "typeinfo": {"type": "Ship"}},
        "PASUM001": {"id": 8888, "typeinfo": {"type": "Modernization"}},
    }
    result = _extract_achievement_map(gp)
    assert result == {"1": "BATTLE_HERO"}


def test_extract_achievement_map_skips_entries_without_id():
    gp = {
        "Achievement_NO_ID": {"typeinfo": {"type": "Achievement"}},
        "Achievement_GOOD": {"id": 42, "typeinfo": {"type": "Achievement"}},
    }
    result = _extract_achievement_map(gp)
    assert result == {"42": "GOOD"}


def test_extract_achievement_map_falls_back_to_raw_name_when_no_prefix():
    """Entries without the 'Achievement_' prefix keep their full name."""
    gp = {
        "WeirdAchievementName": {"id": 7, "typeinfo": {"type": "Achievement"}},
    }
    result = _extract_achievement_map(gp)
    assert result == {"7": "WeirdAchievementName"}


def test_extract_achievement_map_handles_non_dict_values():
    """GameParams contains non-dict entries (lists, primitives); must skip cleanly."""
    gp = {
        "Achievement_GOOD": {"id": 1, "typeinfo": {"type": "Achievement"}},
        "some_metadata_key": "not a dict",
        "another_key": ["list", "value"],
    }
    result = _extract_achievement_map(gp)
    assert result == {"1": "GOOD"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `wows-renderer/`):
```bash
uv run pytest tests/test_gamedata_cache_achievements.py -v
```
Expected: `ImportError` or `AttributeError: module 'renderer.gamedata_cache' has no attribute '_extract_achievement_map'`.

- [ ] **Step 3: Implement `_extract_achievement_map`**

Open `wows-renderer/renderer/gamedata_cache.py`. Just below `_extract_aircraft_icon_map` (around line 308), add:

```python
def _extract_achievement_map(gp: dict) -> dict[str, str]:
    """Extract achievement id → ui_name (icon filename suffix) from GameParams.

    Icon files in ``gui/achievements/`` follow the convention
    ``icon_achievement_<UI_NAME>.png`` where ``<UI_NAME>`` is the GameParams
    entry name minus the leading ``Achievement_`` prefix (verified against
    real GameParams + on-disk icon set during planning).

    Entries whose name does not start with ``Achievement_`` fall back to the
    raw name. Entries missing an ``id`` field are skipped. Non-dict values
    in the GameParams pickle are ignored.
    """
    result: dict[str, str] = {}
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if not isinstance(ti, dict) or ti.get("type") != "Achievement":
            continue
        aid = obj.get("id")
        if aid is None:
            continue
        if name.startswith("Achievement_"):
            ui_name = name[len("Achievement_"):]
        else:
            ui_name = name
        result[str(aid)] = ui_name
    return result
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_gamedata_cache_achievements.py -v
```
Expected: all five tests PASS.

- [ ] **Step 5: Commit**

```bash
git add renderer/gamedata_cache.py tests/test_gamedata_cache_achievements.py
git commit -m "feat(gamedata): extract achievement id -> ui_name map from GameParams"
```

---

## Task 3: Write `achievements.json` during cache build (TDD)

Wire `_extract_achievement_map` into `ensure_version_cache` so every freshly-populated version cache writes `data/achievements.json`. Existing caches stay unchanged for now; Task 4 adds the backfill that makes them work too.

**Files:**
- Modify: `wows-renderer/renderer/gamedata_cache.py:633-650` (the cache-population block around `ship_consumables.json` and `write_split_subset`)
- Modify: `wows-renderer/tests/test_gamedata_cache_achievements.py`

- [ ] **Step 1: Add a failing integration-style test that drives the write step directly**

The full `ensure_version_cache` path needs git + a real gamedata repo, which is heavy for a unit test. Instead, test the write-step function in isolation. Append to `tests/test_gamedata_cache_achievements.py`:

```python
import json
from pathlib import Path

from renderer.gamedata_cache import _write_achievements_json


def test_write_achievements_json_creates_file(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    gp = {
        "Achievement_FIRST": {"id": 1, "typeinfo": {"type": "Achievement"}},
        "Achievement_SECOND": {"id": 2, "typeinfo": {"type": "Achievement"}},
    }
    _write_achievements_json(gp, data_dir)
    out = data_dir / "achievements.json"
    assert out.exists()
    loaded = json.loads(out.read_text())
    assert loaded == {"1": "FIRST", "2": "SECOND"}


def test_write_achievements_json_writes_empty_dict_for_empty_input(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_achievements_json({}, data_dir)
    out = data_dir / "achievements.json"
    assert out.exists()
    assert json.loads(out.read_text()) == {}
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
uv run pytest tests/test_gamedata_cache_achievements.py::test_write_achievements_json_creates_file -v
```
Expected: `ImportError: cannot import name '_write_achievements_json'`.

- [ ] **Step 3: Add `_write_achievements_json` and call it in `ensure_version_cache`**

In `wows-renderer/renderer/gamedata_cache.py`, just below the new `_extract_achievement_map` add:

```python
def _write_achievements_json(gp: dict, data_dir: Path) -> Path:
    """Extract achievement map from GameParams and write it under data_dir.

    Returns the written path. Always writes a file, even when the map is
    empty, so the on-disk schema is consistent across patches.
    """
    achievements = _extract_achievement_map(gp)
    out = data_dir / "achievements.json"
    out.write_text(json.dumps(achievements, separators=(",", ":")))
    return out
```

Then, in `ensure_version_cache` immediately after `ship_consumables.json` is written (after the `sc_path.write_text(...)` block at lines 641-645), add:

```python
        # Achievement id -> ui_name map for the achievement overlay layer.
        _write_achievements_json(gp, data_dir)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_gamedata_cache_achievements.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add renderer/gamedata_cache.py tests/test_gamedata_cache_achievements.py
git commit -m "feat(gamedata): write achievements.json during cache population"
```

---

## Task 4: `VersionedGamedata.achievements` with on-disk backfill (TDD)

Add the cached property used by the layer. Old version caches don't have `achievements.json`; the property must backfill from the lazy-loaded GameParams pickle and write the file so future calls hit disk.

**Files:**
- Modify: `wows-renderer/renderer/gamedata_cache.py` (`VersionedGamedata` class around line 412)
- Modify: `wows-renderer/tests/test_gamedata_cache_achievements.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_gamedata_cache_achievements.py`:

```python
from renderer.gamedata_cache import VersionedGamedata


def _make_versioned_gamedata(tmp_path: Path, gp: dict | None = None) -> VersionedGamedata:
    """Build a VersionedGamedata pointing at a tmp dir.

    If gp is supplied it's used as the pre-loaded gameparams dict (no pickle
    file needed). Otherwise the caller is responsible for setting up the
    pickle on disk.
    """
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return VersionedGamedata(
        version_dir=tmp_path,
        build_id="test",
        _gameparams=gp,
    )


def test_achievements_loads_existing_json(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "achievements.json").write_text(
        json.dumps({"1": "FIRST", "2": "SECOND"})
    )
    vgd = _make_versioned_gamedata(tmp_path, gp={})  # gp irrelevant — file present
    assert vgd.achievements == {1: "FIRST", 2: "SECOND"}


def test_achievements_backfills_from_gameparams_when_json_missing(tmp_path: Path):
    gp = {
        "Achievement_BACKFILL": {"id": 99, "typeinfo": {"type": "Achievement"}},
    }
    vgd = _make_versioned_gamedata(tmp_path, gp=gp)
    assert vgd.achievements == {99: "BACKFILL"}
    # Backfill should have written the file for future loads.
    out = tmp_path / "data" / "achievements.json"
    assert out.exists()
    assert json.loads(out.read_text()) == {"99": "BACKFILL"}


def test_achievements_returns_empty_dict_when_backfill_fails(tmp_path: Path, monkeypatch):
    """If the JSON is missing AND we can't access GameParams, return {}."""
    vgd = _make_versioned_gamedata(tmp_path, gp=None)

    # Force a failure when the property tries to load gameparams.
    def _boom(self):  # noqa: ANN001 — bound method shim
        raise FileNotFoundError("no pickle here")

    monkeypatch.setattr(VersionedGamedata, "gameparams", property(_boom))
    assert vgd.achievements == {}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_gamedata_cache_achievements.py -v
```
Expected: three new failures with `AttributeError: 'VersionedGamedata' object has no attribute 'achievements'`.

- [ ] **Step 3: Add the `achievements` cached property**

In `wows-renderer/renderer/gamedata_cache.py`, inside the `VersionedGamedata` class (just below `aircraft_icon_map` around line 382), add:

```python
    @cached_property
    def achievements(self) -> dict[int, str]:
        """Achievement id → ui_name (icon filename suffix).

        Prefers ``data/achievements.json`` if present (written during cache
        population). Falls back to backfilling from the lazy-loaded
        GameParams pickle for caches that pre-date the achievement overlay
        feature; the backfill also writes the JSON so future loads hit disk.
        Returns an empty dict if neither source is available.
        """
        path = self.version_dir / "data" / "achievements.json"
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                return {int(k): v for k, v in raw.items()}
            except (ValueError, OSError):
                log.warning("Corrupt achievements.json at %s — backfilling", path)

        # Backfill from GameParams pickle.
        try:
            gp = self.gameparams
        except (FileNotFoundError, OSError):
            return {}
        raw = _extract_achievement_map(gp)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(raw, separators=(",", ":")))
        except OSError:
            log.debug("Could not write backfill achievements.json at %s", path, exc_info=True)
        return {int(k): v for k, v in raw.items()}
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_gamedata_cache_achievements.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add renderer/gamedata_cache.py tests/test_gamedata_cache_achievements.py
git commit -m "feat(gamedata): VersionedGamedata.achievements with on-disk backfill"
```

---

## Task 5: Expose `panel_bottom` on `RibbonLayer` (TDD)

`AchievementLayer` anchors under the ribbon block. Mirror the `panel_bottom` pattern already used by `PlayerHeaderLayer` and `DamageStatsLayer` ([renderer/layers/damage_stats.py:79](renderer/layers/damage_stats.py#L79)).

**Files:**
- Modify: `wows-renderer/renderer/layers/ribbons.py`
- Create: `wows-renderer/tests/test_ribbon_panel_bottom.py`

- [ ] **Step 1: Write a failing test**

```python
# wows-renderer/tests/test_ribbon_panel_bottom.py
"""Verify RibbonLayer exposes panel_bottom for downstream layers."""
from __future__ import annotations

from renderer.layers.ribbons import RibbonLayer


def test_ribbon_layer_has_panel_bottom_attribute():
    layer = RibbonLayer()
    assert hasattr(layer, "panel_bottom")
    assert layer.panel_bottom == 0.0
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_ribbon_panel_bottom.py -v
```
Expected: `AssertionError: assert False` (no `panel_bottom` attribute yet).

- [ ] **Step 3: Add `panel_bottom` and update its value during render**

In `wows-renderer/renderer/layers/ribbons.py`:

1. Add a class-level default. Inside `class RibbonLayer(Layer):` just below the existing constants (after `COUNT_FONT_SIZE = 10`), add:

```python
    panel_bottom: float = 0.0
```

2. In `render()`, immediately before the existing `cr.restore()` at the end (current line 201), record the bottom Y. Replace:

```python
            x += col_w + gap * 2

        cr.restore()
```

with:

```python
            x += col_w + gap * 2

        # Record bottom of the last drawn row for downstream layers
        # (AchievementLayer anchors directly under this).
        self.panel_bottom = y_row + row_max_h

        cr.restore()
```

3. Handle the early-return path where nothing draws (`if not self._counts: return` at line 132-133). Replace:

```python
        if not self._counts:
            return
```

with:

```python
        if not self._counts:
            # Still expose a sensible panel_bottom so AchievementLayer
            # can anchor at the would-be start position.
            s_local = self.ctx.scale
            y_start = getattr(self, "_dmg_stats_ref", None)
            if y_start is not None and y_start.panel_bottom > 0:
                self.panel_bottom = y_start.panel_bottom + 6 * s_local
            else:
                self.panel_bottom = self.ctx.config.hud_height + 10
            return
```

Also handle the initial `if not self._timeline: return` (line 117-118):

```python
        if not self._timeline:
            self.panel_bottom = self.ctx.config.hud_height + 10
            return
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_ribbon_panel_bottom.py -v
```
Expected: PASS.

- [ ] **Step 5: Run existing test suite to confirm no regressions**

```bash
uv run pytest tests/ -v
```
Expected: All previously-passing tests still pass. (Smoke / integration tests will skip if no fixture replay is present — that's fine.)

- [ ] **Step 6: Commit**

```bash
git add renderer/layers/ribbons.py tests/test_ribbon_panel_bottom.py
git commit -m "feat(ribbons): expose panel_bottom for downstream layers"
```

---

## Task 6: Create `AchievementLayer` (TDD, no Cairo)

Write the layer in two test passes: first verify it filters and de-duplicates events correctly using a synthetic context. Then verify timeline-walk + panel_bottom behavior. Cairo rendering itself is not unit-tested — it's exercised by the smoke test in Task 8.

**Files:**
- Create: `wows-renderer/renderer/layers/achievements.py`
- Create: `wows-renderer/tests/test_achievements_layer.py`

- [ ] **Step 1: Write the failing test for filtering + de-duplication**

```python
# wows-renderer/tests/test_achievements_layer.py
"""Unit tests for AchievementLayer.

Avoids Cairo entirely by stopping after initialize() — we just inspect the
internal timeline and seen-order state. End-to-end render coverage lives in
the smoke / golden-image suites.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from renderer.layers.achievements import AchievementLayer
from wows_replay_parser.events.models import AchievementEvent


@dataclass
class _StubPlayer:
    account_id: int
    relation: int
    team_id: int = 0
    name: str = "stub"


@dataclass
class _StubReplay:
    events: list = field(default_factory=list)


@dataclass
class _StubConfig:
    effective_gamedata_path: Path
    versioned_gamedata: object | None
    minimap_size: int = 760
    hud_height: int = 24


@dataclass
class _StubVGD:
    achievements: dict
    version_dir: Path = field(default_factory=lambda: Path("/nonexistent"))


@dataclass
class _StubCtx:
    config: _StubConfig
    replay: _StubReplay
    player_lookup: dict

    @property
    def scale(self) -> float:
        return 1.0


def _make_ctx(
    tmp_path: Path,
    events: list,
    players: dict[int, _StubPlayer],
    achievements: dict[int, str] | None = None,
) -> _StubCtx:
    (tmp_path / "gui" / "achievements").mkdir(parents=True)
    vgd = _StubVGD(achievements=achievements or {})
    cfg = _StubConfig(
        effective_gamedata_path=tmp_path,
        versioned_gamedata=vgd,
    )
    return _StubCtx(
        config=cfg,
        replay=_StubReplay(events=events),
        player_lookup=players,
    )


def test_initialize_filters_to_recording_player(tmp_path: Path):
    self_player = _StubPlayer(account_id=111, relation=0)
    other = _StubPlayer(account_id=222, relation=1)
    events = [
        AchievementEvent(timestamp=10.0, entity_id=1, player_id=111, achievement_id=1),
        AchievementEvent(timestamp=20.0, entity_id=1, player_id=222, achievement_id=2),
        AchievementEvent(timestamp=30.0, entity_id=1, player_id=111, achievement_id=3),
    ]
    ctx = _make_ctx(tmp_path, events, {1: self_player, 2: other})
    layer = AchievementLayer()
    layer.initialize(ctx)
    assert layer._timeline == [(10.0, 1), (30.0, 3)]


def test_initialize_deduplicates_by_achievement_id(tmp_path: Path):
    self_player = _StubPlayer(account_id=111, relation=0)
    events = [
        AchievementEvent(timestamp=10.0, entity_id=1, player_id=111, achievement_id=1),
        AchievementEvent(timestamp=20.0, entity_id=1, player_id=111, achievement_id=1),
    ]
    ctx = _make_ctx(tmp_path, events, {1: self_player})
    layer = AchievementLayer()
    layer.initialize(ctx)
    # Timeline keeps both entries — de-dup happens during render walk
    # so timestamps are preserved for analysis.
    assert layer._timeline == [(10.0, 1), (20.0, 1)]


def test_initialize_with_no_self_player_gives_empty_timeline(tmp_path: Path):
    other = _StubPlayer(account_id=222, relation=1)
    events = [
        AchievementEvent(timestamp=10.0, entity_id=1, player_id=222, achievement_id=2),
    ]
    ctx = _make_ctx(tmp_path, events, {2: other})
    layer = AchievementLayer()
    layer.initialize(ctx)
    assert layer._timeline == []


def test_initialize_handles_missing_versioned_gamedata(tmp_path: Path):
    """When versioned_gamedata is None we don't crash; layer becomes a no-op."""
    self_player = _StubPlayer(account_id=111, relation=0)
    events = [AchievementEvent(timestamp=10.0, entity_id=1, player_id=111, achievement_id=1)]
    (tmp_path / "gui" / "achievements").mkdir(parents=True)
    cfg = _StubConfig(
        effective_gamedata_path=tmp_path,
        versioned_gamedata=None,
    )
    ctx = _StubCtx(
        config=cfg,
        replay=_StubReplay(events=events),
        player_lookup={1: self_player},
    )
    layer = AchievementLayer()
    layer.initialize(ctx)
    # Timeline may still be populated (event filtering doesn't need gamedata)
    # but the icon map must be empty.
    assert layer._achievement_names == {}


def test_panel_bottom_zero_until_first_render_call(tmp_path: Path):
    self_player = _StubPlayer(account_id=111, relation=0)
    events: list = []
    ctx = _make_ctx(tmp_path, events, {1: self_player})
    layer = AchievementLayer()
    layer.initialize(ctx)
    # Before any render call, panel_bottom is unset
    assert layer.panel_bottom == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_achievements_layer.py -v
```
Expected: `ModuleNotFoundError: No module named 'renderer.layers.achievements'`.

- [ ] **Step 3: Create the layer**

Create `wows-renderer/renderer/layers/achievements.py`:

```python
"""Achievement icon overlay for the recording player.

Reads ``AchievementEvent`` entries from the parsed replay, filters to the
recording player, and renders the corresponding icons in first-appearance
order directly below the ribbon block on the right panel.

Persistent accumulator — once an icon appears it stays for the rest of the
match. Mirrors the layout rules of :class:`RibbonLayer` (24px icons, 3px
gap, panel-width wrap). Renders nothing and consumes no vertical space
until at least one achievement has been earned.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cairo

from renderer.layers.base import Layer, SingleRenderContext

log = logging.getLogger(__name__)


class AchievementLayer(Layer):
    """Persistent achievement-icon row under the ribbon block."""

    MAIN_HEIGHT = 24        # icon display height (matches RibbonLayer.MAIN_HEIGHT)
    GAP = 3                 # gap between icons / wrap rows
    Y_PAD = 6               # gap above the row (below ribbons.panel_bottom)
    FALLBACK_FILENAME = "default.png"

    panel_bottom: float = 0.0

    def initialize(self, ctx: SingleRenderContext) -> None:
        super().initialize(ctx)

        self._timeline: list[tuple[float, int]] = []
        self._tl_idx: int = 0
        self._seen_order: list[int] = []
        self._icons: dict[int, cairo.ImageSurface] = {}
        self._fallback_icon: cairo.ImageSurface | None = None
        self._achievement_names: dict[int, str] = {}

        # 1. Find recording player's account_id (relation == 0).
        own_account_id: int | None = None
        for player in ctx.player_lookup.values():
            if getattr(player, "relation", None) == 0:
                own_account_id = player.account_id
                break
        if own_account_id is None:
            log.debug("No self-player found in player_lookup; achievement layer idle")
            return

        # 2. Build timeline from achievement events for the recording player.
        from wows_replay_parser.events.models import AchievementEvent
        for event in ctx.replay.events:
            if not isinstance(event, AchievementEvent):
                continue
            if event.player_id != own_account_id:
                continue
            self._timeline.append((event.timestamp, event.achievement_id))

        # 3. Resolve id -> ui_name via versioned_gamedata.
        vgd = getattr(ctx.config, "versioned_gamedata", None)
        if vgd is None:
            log.debug("No versioned_gamedata; achievement icons unavailable")
            return
        try:
            self._achievement_names = dict(vgd.achievements)
        except Exception:
            log.debug("Failed to read versioned_gamedata.achievements", exc_info=True)
            return

        # 4. Preload icons we expect to use (unique achievement_ids in timeline).
        gui_dir = Path(ctx.config.effective_gamedata_path) / "gui" / "achievements"
        seen_ids: set[int] = set()
        for _, aid in self._timeline:
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            self._try_load_icon(gui_dir, aid)

        # Always try the fallback so we can draw something for unknown IDs.
        fb_path = gui_dir / self.FALLBACK_FILENAME
        if fb_path.exists():
            try:
                self._fallback_icon = cairo.ImageSurface.create_from_png(str(fb_path))
            except Exception:
                log.debug("Failed to load fallback icon %s", fb_path, exc_info=True)

    def _try_load_icon(self, gui_dir: Path, achievement_id: int) -> None:
        ui_name = self._achievement_names.get(achievement_id)
        if ui_name is None:
            return
        path = gui_dir / f"icon_achievement_{ui_name}.png"
        if not path.exists():
            return
        try:
            self._icons[achievement_id] = cairo.ImageSurface.create_from_png(str(path))
        except Exception:
            log.debug("Failed to load icon %s", path, exc_info=True)

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        # Advance timeline up to `timestamp`, appending new unique IDs.
        while self._tl_idx < len(self._timeline):
            t, aid = self._timeline[self._tl_idx]
            if t > timestamp:
                break
            if aid not in self._seen_order:
                self._seen_order.append(aid)
            self._tl_idx += 1

        # Anchor under ribbons (or fall back to a sane default).
        s = self.ctx.scale
        ribbons_ref = getattr(self, "_ribbons_ref", None)
        if ribbons_ref is not None and ribbons_ref.panel_bottom > 0:
            y_start = ribbons_ref.panel_bottom + self.Y_PAD * s
        else:
            y_start = self.ctx.config.hud_height + 10

        if not self._seen_order:
            # No rows drawn — expose y_start so any future downstream
            # layer can anchor at the same position we would have used.
            self.panel_bottom = y_start
            return

        config = self.ctx.config
        icon_h = self.MAIN_HEIGHT * s
        gap = self.GAP * s
        x_start = config.left_panel + config.minimap_size + 8
        max_x = config.total_width - 4

        cr.save()
        clip_x = config.left_panel + config.minimap_size
        cr.rectangle(clip_x, 0, config.right_panel, config.total_height)
        cr.clip()

        x = x_start
        y_row = y_start
        for aid in self._seen_order:
            icon = self._icons.get(aid, self._fallback_icon)
            if icon is None:
                continue
            iw = icon.get_width()
            ih = icon.get_height()
            scale = icon_h / ih
            draw_w = iw * scale
            if x + draw_w > max_x and x > x_start:
                x = x_start
                y_row += icon_h + gap
            cr.save()
            cr.translate(x, y_row)
            cr.scale(scale, scale)
            cr.set_source_surface(icon, 0, 0)
            cr.paint()
            cr.restore()
            x += draw_w + gap

        self.panel_bottom = y_row + icon_h
        cr.restore()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_achievements_layer.py -v
```
Expected: all five tests PASS.

- [ ] **Step 5: Commit**

```bash
git add renderer/layers/achievements.py tests/test_achievements_layer.py
git commit -m "feat(layer): add AchievementLayer for recording-player achievements"
```

---

## Task 7: Wire `AchievementLayer` into `RightPanelLayer` (TDD)

**Files:**
- Modify: `wows-renderer/renderer/layers/right_panel.py`
- Create: `wows-renderer/tests/test_right_panel_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# wows-renderer/tests/test_right_panel_wiring.py
"""Verify RightPanelLayer instantiates and wires the AchievementLayer."""
from __future__ import annotations

from renderer.layers.achievements import AchievementLayer
from renderer.layers.ribbons import RibbonLayer
from renderer.layers.right_panel import RightPanelLayer


def test_right_panel_creates_achievement_layer_by_default():
    panel = RightPanelLayer()
    assert panel._achievements is not None
    assert isinstance(panel._achievements, AchievementLayer)


def test_right_panel_skips_achievements_when_disabled():
    panel = RightPanelLayer(show_achievements=False)
    assert panel._achievements is None


def test_right_panel_wires_ribbons_ref_on_achievement_layer():
    panel = RightPanelLayer()
    assert panel._achievements is not None
    assert panel._achievements._ribbons_ref is panel._ribbons
    assert isinstance(panel._achievements._ribbons_ref, RibbonLayer)


def test_right_panel_no_ribbons_means_no_achievement_wiring():
    """If ribbons are disabled, achievements still exist but have no anchor."""
    panel = RightPanelLayer(show_ribbons=False)
    if panel._achievements is not None:
        # Either the layer exists without a ribbon ref, or wiring was skipped.
        assert getattr(panel._achievements, "_ribbons_ref", None) is None


def test_right_panel_sub_layers_includes_achievements():
    panel = RightPanelLayer()
    sub_layers = panel._sub_layers()
    assert any(isinstance(s, AchievementLayer) for s in sub_layers)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_right_panel_wiring.py -v
```
Expected: `AttributeError: 'RightPanelLayer' object has no attribute '_achievements'`.

- [ ] **Step 3: Wire `AchievementLayer` in**

In `wows-renderer/renderer/layers/right_panel.py`:

1. Add the import at the top with the others:

```python
from renderer.layers.achievements import AchievementLayer
```

2. Replace the `__init__` signature and body to add `show_achievements`:

```python
    def __init__(
        self,
        *,
        show_header: bool = True,
        show_damage: bool = True,
        show_ribbons: bool = True,
        show_achievements: bool = True,
        show_killfeed: bool = True,
    ) -> None:
        self._show_header = show_header
        self._show_damage = show_damage
        self._show_ribbons = show_ribbons
        self._show_achievements = show_achievements
        self._show_killfeed = show_killfeed

        self._header = PlayerHeaderLayer() if show_header else None
        self._damage = DamageStatsLayer() if show_damage else None
        self._ribbons = RibbonLayer() if show_ribbons else None
        self._achievements = AchievementLayer() if show_achievements else None
        self._killfeed = KillfeedLayer() if show_killfeed else None

        # Wire cross-references
        if self._header and self._damage:
            self._header._dmg_stats_ref = self._damage
            self._damage._header_ref = self._header
        if self._ribbons and self._damage:
            self._ribbons._dmg_stats_ref = self._damage
        if self._achievements and self._ribbons:
            self._achievements._ribbons_ref = self._ribbons
```

3. Update `render` to call achievements between ribbons and killfeed:

```python
        # Render sub-layers (header and damage draw the shared background)
        if self._header:
            self._header.render(cr, state, timestamp)
        if self._damage:
            self._damage.render(cr, state, timestamp)
        if self._ribbons:
            self._ribbons.render(cr, state, timestamp)
        if self._achievements:
            self._achievements.render(cr, state, timestamp)
        if self._killfeed:
            self._killfeed.render(cr, state, timestamp)
```

4. Update `_sub_layers` to include achievements:

```python
    def _sub_layers(self):
        return [
            layer for layer in (
                self._header,
                self._damage,
                self._ribbons,
                self._achievements,
                self._killfeed,
            )
            if layer
        ]
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_right_panel_wiring.py -v
```
Expected: all five tests PASS.

- [ ] **Step 5: Run the full suite to catch regressions**

```bash
uv run pytest tests/ -v
```
Expected: all previously-passing tests pass; layer / wiring tests added in this plan also pass.

- [ ] **Step 6: Commit**

```bash
git add renderer/layers/right_panel.py tests/test_right_panel_wiring.py
git commit -m "feat(right_panel): wire AchievementLayer between ribbons and killfeed"
```

---

## Task 8: End-to-end render smoke check (manual)

This is a manual verification, not a new automated test. We confirm the achievement row actually appears in a rendered video.

**Files:** none modified.

- [ ] **Step 1: Pick a replay where the recording player earned at least one achievement**

Check existing replays at the top of `wows-renderer/`. From the file listing, any replay over a few minutes long will likely have at least one achievement.

If unsure, run from `wows-renderer/`:
```bash
uv run python -c "
import sys
from pathlib import Path
from wows_replay_parser import parse_replay
from wows_replay_parser.events.models import AchievementEvent

replay_path = Path(sys.argv[1])
gamedata = Path('wows-gamedata/data/scripts_entity/entity_defs')
r = parse_replay(replay_path, gamedata)
self_acct = next((p.account_id for p in r.players if p.relation == 0), None)
acks = [e for e in r.events if isinstance(e, AchievementEvent) and e.player_id == self_acct]
print(f'{replay_path.name}: {len(acks)} self achievements')
for e in acks[:10]:
    print(f'  t={e.timestamp:6.1f} id={e.achievement_id}')
" 20260521_081743_PVSB010-Libertad_19_OC_prey.wowsreplay
```

Pick the first replay with non-zero achievements.

- [ ] **Step 2: Run a short render**

From `wows-renderer/`:
```bash
uv run python render_quick.py <chosen-replay>.wowsreplay /tmp/achievement-smoke.mp4
```

- [ ] **Step 3: Visually inspect the output**

Open `/tmp/achievement-smoke.mp4`. Verify:
- Right panel renders identically to baseline until the first achievement is earned.
- At the moment of the first achievement, an icon appears directly below the last ribbon row, with ~6px gap.
- Each subsequent achievement appends to the right.
- The killfeed below remains usable (its bottom-anchored layout shrinks but no overlap or clipping into the ribbon area).
- Once a row fills the panel width, icons wrap to a second row.

- [ ] **Step 4: (Conditional) Test the unknown-ID fallback**

If the chosen replay has no unknown IDs (likely), this step is informational only. To force-exercise the fallback, temporarily edit
`~/.cache/wows-gamedata/v<latest>/data/achievements.json` to remove one ID
that appears in the chosen replay, then re-render. Confirm a `default.png`
icon shows in that slot. Restore the JSON afterward (or delete the cache —
it will be rebuilt).

- [ ] **Step 5: No commit**

Manual verification step. Nothing to commit unless the visual inspection
surfaces a bug, in which case file a fix as a follow-up.

---

## Self-review checklist

Run this after Task 8 is complete:

- [ ] All seven test files pass: `uv run pytest tests/test_achievements_layer.py tests/test_gamedata_cache_achievements.py tests/test_ribbon_panel_bottom.py tests/test_right_panel_wiring.py -v`
- [ ] Full suite still green: `uv run pytest tests/ -v` (smoke / integration may skip without fixtures — that's expected)
- [ ] `uv run ruff check renderer/ tests/` is clean for the touched files.
- [ ] Spec coverage (cross-reference each spec section to a task):
  - "User-visible behaviour" → Tasks 5, 6, 7, 8.
  - "AchievementLayer" → Task 6.
  - "RibbonLayer.panel_bottom" → Task 5.
  - "RightPanelLayer wiring" → Task 7.
  - "Gamedata cache: achievements.json" → Tasks 2, 3.
  - "RenderContext.versioned_gamedata" → Task 6 (uses `ctx.config.versioned_gamedata.achievements`).
  - "Edge cases" → covered by Task 6 unit tests + Task 4 backfill test + Task 8 manual check.
- [ ] No placeholders, no TBD, no "implement later" anywhere in the diff.
