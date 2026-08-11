# Statistics Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third button, **Statistics**, to the Discord render result view that posts a wide PNG stats board covering every player in the match.

**Architecture:** Two new modules with a hard boundary. `renderer/stats_export.py` runs in the render worker (which already holds the parsed replay and gamedata) and turns `BattleResults` into frozen, picklable dataclasses. `renderer/stats_board.py` runs in the bot process only when the button is clicked, and is pure Cairo presentation with no parser, gamedata, or Discord imports.

**Tech Stack:** Python 3.11+, pycairo, discord.py, pytest, `wows-replay-parser` (local path dep).

**Spec:** `docs/superpowers/specs/2026-08-11-statistics-board-design.md`

**Branch:** `feat/statistics-board` (already created; the spec commit is `2bfdd59`).

## Context

The bot renders a replay to mp4 and attaches two buttons — `Show Builds` and `Download Chat`. The video shows what happened but gives no way to compare players afterwards.

Every replay that reaches its end carries packet `0x22`, which the parser already decodes into `BattleResults`: **466 named fields for every player in the match**, not just the recorder, plus per-player ribbon tallies. The bot currently discards it entirely. Surfacing it costs one button and adds no new data source.

Two formulas in this area produce plausible-looking wrong numbers and are the reason this plan is TDD-first. Both were verified against a real 14-player sample:

- **Trap A — `damage` is authoritative.** Re-deriving it as `Σ damage_*` overshoots for any player who shot down aircraft, because `damage_airdefense` and `damage_planes_by_plane` share the prefix. Wrong for 5 of 14 sample players (Lucytus: 85,382 real vs 111,818 naive).
- **Trap B — `Recv Dmg` is `Σ received_damage_*`, not `max_health - remained_hp`.** The subtraction under-reports any ship that healed. BlueMidhir received 86,887 but lost only 75,050 HP.

**Scope correction carried by this plan.** The spec's "Open item — `Pts` units" is wrong and Task 8 fixes it. `Σ victory_points_*` is not a ×100 team score: the per-team sums are 261,720 / 34,660 in a match that scores to 1000, the value `34,800` appears identically on two different players, and `23,400` appears on one player from *each* team. These are reward/bonus points partly duplicated across teammates, not per-player score contribution. **The `Pts` column is dropped from v1**, leaving 29 columns. Nothing else in the spec changes.

## Global Constraints

- `renderer/` must gain **no new third-party dependency**. Cairo and stdlib only.
- `renderer/stats_board.py` must not import `wows_replay_parser`, `discord`, or anything under `renderer/gamedata*`. Presentation only.
- `renderer/stats_export.py` must not import `cairo`. Data only.
- All new dataclasses are `@dataclass(frozen=True)` and picklable across `ProcessPoolExecutor`.
- Extraction failure must **never** fail a render — wrap and log, mirroring the `build_urls` treatment at `bot/worker.py:219-230`.
- Ruff and mypy must pass: `ruff check .` and `mypy renderer bot`. Line limit is E501 (existing config).
- Player-facing text respects the `anonymize` flag in `RenderConfig.flags`.
- Team colors come from `renderer.themes.THEMES[theme]`, never hardcoded.

## File Structure

| File | Responsibility |
|---|---|
| `renderer/death_reasons.py` (new) | Shared `DEATH_REASON` id → (label, icon) table |
| `renderer/stats_export.py` (new) | `PlayerStats` / `MatchStats` dataclasses + extraction from `BattleResults` |
| `renderer/stats_board.py` (new) | Cairo → PNG bytes for a `MatchStats` |
| `renderer/layers/killfeed.py` (modify) | Import the shared table instead of defining it |
| `bot/worker.py` (modify) | `RenderResult` dataclass replaces the 8-tuple; call extraction |
| `bot/cog_render.py` (modify) | `Statistics` button; 3 unpack sites become attribute access |
| `tests/fixtures/battle_results_sample.json` (new) | Sanitised 14-player, 538-field fixture |
| `tests/test_stats_export.py` (new) | Trap A/B, accuracy, ribbons, anonymize, None path, sort |
| `tests/test_stats_board.py` (new) | Golden image + empty-input guard |
| `tests/test_worker_signature.py` (modify) | Real `RenderResult` field assertion |
| `tests/test_stats_button.py` (new) | Button present/absent by `stats` |

---

### Task 1: Shared DEATH_REASON table

`_DEATH_REASON` maps a weapon/death id to a label and a frag icon. It lives inside `killfeed.py` today; the stats board needs the same table for its "Killed by" column. One table, two consumers.

**Files:**
- Create: `renderer/death_reasons.py`
- Modify: `renderer/layers/killfeed.py:10-41` (delete the dict, import instead)
- Test: `tests/test_death_reasons.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `renderer.death_reasons.DEATH_REASON: dict[int, tuple[str, str]]` mapping id → `(label, icon_frag_filename)`, and `death_reason_label(reason: int) -> str` returning `""` for unknown ids.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_death_reasons.py
"""The death-reason table is shared by the killfeed layer and the stats
board. These tests pin the ids that both consumers depend on."""
from __future__ import annotations

from renderer.death_reasons import DEATH_REASON, death_reason_label


def test_known_ids_have_labels():
    # 18 = HE_SHELL. The sample replay's killer_weapon field uses it.
    assert death_reason_label(18) == "HE"
    assert death_reason_label(3) == "TORP"
    assert death_reason_label(6) == "FIRE"
    assert death_reason_label(7) == "RAM"


def test_unknown_id_returns_empty_string():
    assert death_reason_label(9999) == ""
    assert death_reason_label(0) == ""


def test_killfeed_uses_the_shared_table():
    """Guard against the table being re-forked into killfeed.py later."""
    from renderer.layers import killfeed

    assert killfeed._DEATH_REASON is DEATH_REASON
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_death_reasons.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renderer.death_reasons'`

- [ ] **Step 3: Create the shared module**

Create `renderer/death_reasons.py` by moving the dict verbatim out of `renderer/layers/killfeed.py:11-41`. Do not retype it — copy it, so no entry is lost.

```python
"""DEATH_REASON enum from battle.xml → (label, icon_frag filename).

Shared by the killfeed layer (which needs the icon) and the statistics
board (which needs only the label). Kept in one place so a patch that
shifts the enum is a single-file fix.
"""
from __future__ import annotations

DEATH_REASON: dict[int, tuple[str, str]] = {
    0: ("", ""),                                  # NONE
    1: ("ARTILLERY", "icon_frag_main_caliber"),   # ARTILLERY (generic)
    2: ("SEC", "icon_frag_atba"),                 # ATBA
    3: ("TORP", "icon_frag_torpedo"),             # TORPEDO
    4: ("BOMB", "icon_frag_bomb"),                # BOMB
    5: ("TORP", "icon_frag_torpedo"),             # TBOMB (torpedo bomber)
    6: ("FIRE", "icon_frag_burning"),             # BURNING
    7: ("RAM", "icon_frag_ram"),                  # RAM
    8: ("TERRAIN", ""),                           # TERRAIN
    9: ("FLOOD", "icon_frag_flood"),              # FLOOD
    10: ("MIRROR", ""),                           # MIRROR
    11: ("MINE", "icon_frag_naval_mine"),         # SEA_MINE
    12: ("", ""),                                 # SPECIAL
    13: ("DBOMB", "icon_frag_depthbomb"),         # DBOMB
    14: ("ROCKET", "icon_frag_rocket"),           # ROCKET
    15: ("DETONATE", "icon_frag_detonate"),       # DETONATE
    16: ("", ""),                                 # HEALTH
    17: ("AP", "icon_frag_main_caliber"),         # AP_SHELL
    18: ("HE", "icon_frag_main_caliber"),         # HE_SHELL
    19: ("SAP", "icon_frag_main_caliber"),        # CS_SHELL
    20: ("FEL", "icon_frag_fel"),                 # FEL
    21: ("PORTAL", "icon_frag_portal"),           # PORTAL
    22: ("SKIP", "icon_frag_skip"),               # SKIP_BOMB
    23: ("WAVE", "icon_frag_wave"),               # SECTOR_WAVE
    24: ("ACID", "icon_frag_acid"),               # ACID
    25: ("LASER", "icon_frag_laser"),             # LASER
    26: ("MATCH", "icon_frag_octagon"),           # MATCH
    28: ("DBOMB", "icon_frag_depthbomb"),         # ADBOMB
    35: ("MISSILE", "icon_frag_missile"),         # MISSILE
}


def death_reason_label(reason: int) -> str:
    """Short label for a death/weapon id. Empty string for unknown ids."""
    return DEATH_REASON.get(reason, ("", ""))[0]
```

**Note:** copy any entries present in `killfeed.py` that this snippet omits — the file is the source of truth for the exact set.

- [ ] **Step 4: Point killfeed at the shared table**

In `renderer/layers/killfeed.py`, delete lines 10-41 (the comment plus the dict literal) and add near the other imports:

```python
from renderer.death_reasons import DEATH_REASON as _DEATH_REASON
```

The alias keeps the existing call sites at `killfeed.py:83` and `killfeed.py:250` unchanged.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_death_reasons.py -v`
Expected: 3 passed

- [ ] **Step 6: Verify no killfeed regression**

Run: `python -m pytest tests/ -q -k "killfeed or golden or smoke"`
Expected: PASS or SKIP (golden/smoke skip without fixtures); no failures, no import errors.

- [ ] **Step 7: Commit**

```bash
git add renderer/death_reasons.py renderer/layers/killfeed.py tests/test_death_reasons.py
git commit -m "refactor: extract DEATH_REASON table into renderer/death_reasons.py"
```

---

### Task 2: Test fixture + damage columns

Builds the sanitised fixture every later test depends on, plus the four damage columns — the two traps live here.

**Files:**
- Create: `tests/fixtures/battle_results_sample.json`
- Create: `scripts/make_stats_fixture.py`
- Create: `renderer/stats_export.py`
- Test: `tests/test_stats_export.py`

**Interfaces:**
- Consumes: `wows_replay_parser.battle_results.BattleResults`, `PlayerBattleResult`.
- Produces:
  - `renderer.stats_export.total_received_damage(stats: dict) -> int`
  - `renderer.stats_export.potential_damage(stats: dict) -> int`
  - `renderer.stats_export.SPECIES_TAG: dict[str, str]`
  - `renderer.stats_export.BR_ROW_LEN: int` (538)

- [ ] **Step 1: Write the fixture generator**

Create `scripts/make_stats_fixture.py`. It reads a `*.postbattle.json` (the repo root has one) and writes a sanitised copy of its `raw` dict. Names, clan tags and account ids are replaced; every numeric stat is preserved untouched, because the tests assert on those numbers.

```python
"""Build the sanitised BattleResults test fixture.

Usage:
    python scripts/make_stats_fixture.py <input.postbattle.json>

Reads the `raw` dict (the exact payload `BattleResults._decode` consumes)
and rewrites identifying fields. Numeric stats pass through unchanged so
the fixture keeps its value as a regression baseline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "battle_results_sample.json"

# Indices into the 538-element playersPublicInfo row. These come from
# PLAYER_INFO_FIELDS in wows_replay_parser/battle_results.py.
IDX_ACCOUNT_DB_ID = 0
IDX_NAME = 1
IDX_CLAN_ID = 2
IDX_CLAN_TAG = 3


def main(src: Path) -> None:
    doc = json.loads(src.read_text())
    raw = doc["raw"]

    ppi = raw["playersPublicInfo"]
    id_map = {old: 100_000 + i for i, old in enumerate(sorted(ppi, key=str))}

    sanitised: dict[str, list] = {}
    for i, (old_key, row) in enumerate(sorted(ppi.items(), key=lambda kv: str(kv[0]))):
        row = list(row)
        row[IDX_ACCOUNT_DB_ID] = id_map[old_key]
        row[IDX_NAME] = f"Player{i + 1:02d}"
        row[IDX_CLAN_ID] = 0
        row[IDX_CLAN_TAG] = f"CL{i % 2}"
        sanitised[str(id_map[old_key])] = row

    raw["playersPublicInfo"] = sanitised
    raw["accountDBID"] = id_map[sorted(ppi, key=str)[0]]
    # privateDataList carries the recorder's economics — not needed and
    # the most identifying part of the payload.
    raw["privateDataList"] = []

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(raw, indent=1))
    print(f"wrote {OUT} ({len(sanitised)} players)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
```

- [ ] **Step 2: Generate the fixture**

Run:
```bash
python scripts/make_stats_fixture.py 20260723_221703_PVSC110-San-Martin_22_tierra_del_fuego.postbattle.json
```
Expected: `wrote .../tests/fixtures/battle_results_sample.json (14 players)`

Verify the row width survived:
```bash
python -c "import json;d=json.load(open('tests/fixtures/battle_results_sample.json'));r=d['playersPublicInfo'];print(len(r), {len(v) for v in r.values()})"
```
Expected: `14 {538}`

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_stats_export.py
"""Statistics extraction — the numeric formulas behind the stats board.

The fixture is a real 14-player BattleResults payload with identities
scrubbed and every stat preserved, so these assertions are regression
baselines against live game data rather than invented numbers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wows_replay_parser.battle_results import BattleResults, _decode

FIXTURE = Path(__file__).parent / "fixtures" / "battle_results_sample.json"


@pytest.fixture(scope="module")
def battle_results() -> BattleResults:
    return _decode(json.loads(FIXTURE.read_text()))


def test_fixture_has_full_width_rows(battle_results):
    """Ribbon columns read raw[481 + ribbon_id]; a short row means the
    schema moved and the ribbon assertions below are meaningless."""
    assert len(battle_results.players) == 14
    for player in battle_results.players.values():
        assert len(player.raw) == 538


def test_damage_field_is_authoritative_not_the_sum(battle_results):
    """Trap A: `Σ damage_*` also catches damage_airdefense and
    damage_planes_by_plane, which are not ship damage. Five of the
    fourteen sample players diverge; assert on the widest gap."""
    from renderer.stats_export import ship_damage

    worst = max(
        battle_results.players.values(),
        key=lambda p: sum(
            v for k, v in p.stats.items()
            if k.startswith("damage_") and isinstance(v, (int, float))
        ) - (p.stat("damage") or 0),
    )
    naive = sum(
        v for k, v in worst.stats.items()
        if k.startswith("damage_") and isinstance(v, (int, float))
    )
    assert ship_damage(worst.stats) == worst.stat("damage")
    assert ship_damage(worst.stats) < naive


def test_received_damage_is_the_sum_not_hp_lost(battle_results):
    """Trap B: max_health - remained_hp under-reports any ship that
    healed. At least one sample player received more than they lost."""
    from renderer.stats_export import total_received_damage

    healed = [
        p for p in battle_results.players.values()
        if total_received_damage(p.stats)
        > (p.stat("max_health") or 0) - (p.stat("remained_hp") or 0)
    ]
    assert healed, "fixture should contain at least one player who healed"

    p = healed[0]
    expected = sum(
        v for k, v in p.stats.items()
        if k.startswith("received_damage_") and isinstance(v, (int, float))
    )
    assert total_received_damage(p.stats) == int(expected)


def test_received_damage_excludes_hits_and_module_fields(battle_results):
    """The prefix must not widen to `received_*`, which would fold in
    received_hits_* and received_module_* counts as if they were damage."""
    from renderer.stats_export import total_received_damage

    p = next(iter(battle_results.players.values()))
    stats = dict(p.stats)
    stats["received_hits_main_ap"] = 10_000_000
    stats["received_module_crits_engine"] = 10_000_000
    assert total_received_damage(stats) == total_received_damage(p.stats)


def test_potential_damage_sums_the_four_agro_fields(battle_results):
    from renderer.stats_export import potential_damage

    p = next(iter(battle_results.players.values()))
    expected = sum(p.stat(f"agro_{x}") or 0 for x in ("art", "tpd", "air", "dbomb"))
    assert potential_damage(p.stats) == int(expected)
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_stats_export.py -v`
Expected: `test_fixture_has_full_width_rows` PASSES; the other four FAIL with `ModuleNotFoundError: No module named 'renderer.stats_export'`

- [ ] **Step 5: Write the module**

```python
# renderer/stats_export.py
"""Post-battle statistics extraction.

Turns the parser's BattleResults into plain frozen dataclasses for the
statistics board. Runs inside the render worker, which already holds the
parsed replay and the gamedata cache.

Deliberately imports no cairo and no discord: the output crosses a
ProcessPoolExecutor boundary and is consumed by a renderer that knows
nothing about replays.
"""
from __future__ import annotations

from typing import Any

# Length of a playersPublicInfo row on the schema this module targets.
# The four ribbon columns read raw[481 + ribbon_id]; a shorter row means
# the schema moved and those columns must degrade to zero rather than
# report whatever happens to sit at that offset.
BR_ROW_LEN = 538

RIBBON_CITADEL = 8
RIBBON_OVERPEN = 14
RIBBON_PENETRATION = 15
RIBBON_SHATTER = 16

_AGRO_FIELDS = ("agro_art", "agro_tpd", "agro_air", "agro_dbomb")
_MAIN_AMMO = ("ap", "cs", "he")

SPECIES_TAG: dict[str, str] = {
    "Destroyer": "DD",
    "Cruiser": "CA",
    "Battleship": "BB",
    "AirCarrier": "CV",
    "Submarine": "SS",
    "Auxiliary": "AUX",
}


def _num(stats: dict[str, Any], key: str) -> float:
    """Numeric field lookup. Missing or non-numeric values read as 0."""
    value = stats.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def ship_damage(stats: dict[str, Any]) -> int:
    """Total damage dealt to ships.

    Reads the `damage` field directly. Do NOT re-derive this as
    `Σ damage_*` — that prefix also matches `damage_airdefense` and
    `damage_planes_by_plane`, which are aircraft damage, and inflates the
    figure for anyone who ran AA. Verified against a 14-player sample
    where five players diverged, the worst by 31%.
    """
    return int(_num(stats, "damage"))


def total_received_damage(stats: dict[str, Any]) -> int:
    """Damage received from all sources.

    The sum, not `max_health - remained_hp` — the subtraction ignores
    healing, so any ship with a repair party under-reports. The prefix is
    exactly `received_damage_`, which covers `received_damage_from_buildings_*`
    while excluding `received_hits_*` and `received_module_*`.
    """
    return int(sum(
        v for k, v in stats.items()
        if k.startswith("received_damage_") and isinstance(v, (int, float))
    ))


def potential_damage(stats: dict[str, Any]) -> int:
    """Potential damage — shells, torpedoes, aircraft and depth charges
    that were aimed at this ship but did not connect."""
    return int(sum(_num(stats, f) for f in _AGRO_FIELDS))


def main_battery_hits(stats: dict[str, Any]) -> int:
    return int(sum(_num(stats, f"hits_main_{a}") for a in _MAIN_AMMO))


def main_battery_shots(stats: dict[str, Any]) -> int:
    return int(sum(_num(stats, f"shots_main_{a}") for a in _MAIN_AMMO))


def accuracy(stats: dict[str, Any]) -> float | None:
    """Main-battery accuracy as a percentage, or None when the ship never
    fired its main guns (submarines, pure torpedo boats). None renders as
    an em dash; returning 0.0 would read as "missed everything"."""
    shots = main_battery_shots(stats)
    if shots <= 0:
        return None
    return 100.0 * main_battery_hits(stats) / shots
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_stats_export.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add scripts/make_stats_fixture.py tests/fixtures/battle_results_sample.json \
        renderer/stats_export.py tests/test_stats_export.py
git commit -m "feat(stats): damage column formulas + sanitised BattleResults fixture"
```

---

### Task 3: Gunnery, ribbon and crit columns

**Files:**
- Modify: `renderer/stats_export.py`
- Test: `tests/test_stats_export.py` (append)

**Interfaces:**
- Consumes: `_num`, `main_battery_hits`, `main_battery_shots`, `BR_ROW_LEN`, ribbon id constants from Task 2.
- Produces: `renderer.stats_export.ribbon_columns(player: PlayerBattleResult) -> tuple[int, int, int, int]` returning `(citadels, penetrations, overpens, shatters)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_stats_export.py


def test_accuracy_is_none_when_main_guns_never_fired(battle_results):
    """Submarines and pure torpedo boats fire no main battery. Accuracy
    must be None (renders as a dash), never 0.0 or a ZeroDivisionError."""
    from renderer.stats_export import accuracy

    p = next(iter(battle_results.players.values()))
    stats = {k: 0 for k in p.stats}
    assert accuracy(stats) is None


def test_accuracy_matches_hits_over_shots(battle_results):
    from renderer.stats_export import accuracy, main_battery_hits, main_battery_shots

    shooters = [
        p for p in battle_results.players.values()
        if main_battery_shots(p.stats) > 0
    ]
    assert shooters, "fixture should contain at least one gunship"

    p = shooters[0]
    expected = 100.0 * main_battery_hits(p.stats) / main_battery_shots(p.stats)
    assert accuracy(p.stats) == pytest.approx(expected)


def test_ribbon_columns_read_the_tail_slots(battle_results):
    """Citadels/pens/overpens/shatters come from raw[481 + ribbon_id],
    not from summing wire events."""
    from renderer.stats_export import ribbon_columns

    totals = [ribbon_columns(p) for p in battle_results.players.values()]
    # The sample is a clan battle; at least one player landed citadels
    # and at least one landed penetrations.
    assert any(t[0] > 0 for t in totals), "expected some citadels"
    assert any(t[1] > 0 for t in totals), "expected some penetrations"


def test_ribbon_columns_zero_out_on_short_rows():
    """If a patch moves the tail offset the row shortens; report zeros
    rather than whatever integer happens to sit at that index."""
    from wows_replay_parser.battle_results import PlayerBattleResult

    from renderer.stats_export import ribbon_columns

    truncated = PlayerBattleResult(db_id=1, stats={}, extra={}, raw=[0] * 400)
    assert ribbon_columns(truncated) == (0, 0, 0, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stats_export.py -v -k "accuracy or ribbon"`
Expected: the two `ribbon` tests FAIL with `ImportError: cannot import name 'ribbon_columns'`; both `accuracy` tests PASS (implemented in Task 2).

- [ ] **Step 3: Implement `ribbon_columns`**

Append to `renderer/stats_export.py`:

```python
def ribbon_columns(player: Any) -> tuple[int, int, int, int]:
    """(citadels, penetrations, overpens, shatters) for one player.

    Reads the server's authoritative end-of-match tallies from the raw
    row tail via PlayerBattleResult.ribbon_count(), which indexes
    raw[481 + ribbon_id].

    That base offset was extracted from build 12267945 (patch 15.3) and
    the parser warns it may differ on earlier builds. Guard on the row
    width: a short row means the schema moved, and four zeroed columns
    beat four columns of plausible garbage.
    """
    if len(getattr(player, "raw", ())) < BR_ROW_LEN:
        return (0, 0, 0, 0)
    return (
        player.ribbon_count(RIBBON_CITADEL),
        player.ribbon_count(RIBBON_PENETRATION),
        player.ribbon_count(RIBBON_OVERPEN),
        player.ribbon_count(RIBBON_SHATTER),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_stats_export.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add renderer/stats_export.py tests/test_stats_export.py
git commit -m "feat(stats): ribbon columns with short-row guard"
```

---

### Task 4: PlayerStats / MatchStats assembly

Turns the per-field helpers into the dataclasses the board consumes: display teams, sorting, anonymisation, killer resolution.

**Files:**
- Modify: `renderer/stats_export.py`
- Test: `tests/test_stats_export.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 2-3, plus `renderer.death_reasons.death_reason_label` from Task 1.
- Produces:
  - `renderer.stats_export.PlayerStats` (frozen dataclass, fields listed below)
  - `renderer.stats_export.MatchStats` (frozen dataclass)
  - `build_match_stats(results, ships_db, self_team_id, meta, flags) -> MatchStats` — the pure, testable core
  - `extract_match_stats(replay, vgd, flags) -> MatchStats | None` — the worker-facing wrapper

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_stats_export.py


def _build(battle_results, **kw):
    from renderer.stats_export import build_match_stats

    defaults = dict(
        results=battle_results,
        ships_db={},
        self_team_id=0,
        meta={"map_name": "Tierra del Fuego", "game_type": "ClanBattle", "duration_sec": 581},
        flags=frozenset(),
    )
    defaults.update(kw)
    return build_match_stats(**defaults)


def test_players_are_grouped_by_display_team_then_damage_desc(battle_results):
    stats = _build(battle_results)
    teams = [p.team for p in stats.players]
    assert teams == sorted(teams), "team 0 block must precede team 1 block"

    for team in (0, 1):
        block = [p.damage for p in stats.players if p.team == team]
        assert block == sorted(block, reverse=True)


def test_self_team_becomes_display_team_zero(battle_results):
    """Trap 5: the recorder's raw team id is 0 or 1 depending on the
    replay. After the swap their team always renders as 0."""
    swapped = _build(battle_results, self_team_id=1)
    raw_team_1_names = {
        p.stat("name") for p in battle_results.players.values() if p.team_id == 1
    }
    display_0_names = {p.name for p in swapped.players if p.team == 0}
    assert display_0_names == raw_team_1_names


def test_anonymize_replaces_names_and_drops_clan_tags(battle_results):
    stats = _build(battle_results, flags=frozenset({"anonymize"}))
    assert all(p.name.startswith("Player ") for p in stats.players)
    assert all(p.clan_tag == "" for p in stats.players)
    assert len({p.name for p in stats.players}) == len(stats.players)


def test_killed_by_resolves_to_a_name_and_weapon(battle_results):
    """killer_db_id joins back to another row in the same payload."""
    stats = _build(battle_results)
    killed = [p for p in stats.players if p.killed_by]
    assert killed, "fixture should contain at least one dead player"
    assert all(p.killer_weapon for p in killed)


def test_survivors_have_no_killer(battle_results):
    stats = _build(battle_results)
    for p in stats.players:
        if p.hp_remaining > 0:
            assert p.killed_by == ""
            assert p.killer_weapon == ""


def test_unknown_ship_id_falls_back_to_the_raw_index(battle_results):
    stats = _build(battle_results, ships_db={})
    assert all(p.ship_name for p in stats.players)
    assert all(p.ship_class == "" for p in stats.players)


def test_ship_name_and_class_resolve_from_ships_db(battle_results):
    sample = next(iter(battle_results.players.values()))
    ship_id = int(sample.stat("vehicle_type_id"))
    db = {ship_id: {"name": "PFSC210_Marseille", "short_name": "Marseille",
                    "species": "Cruiser", "level": 10, "index": "PFSC210"}}
    stats = _build(battle_results, ships_db=db)
    hit = [p for p in stats.players if p.ship_name == "Marseille"]
    assert hit and hit[0].ship_class == "CA"


def test_extract_returns_none_without_a_results_packet():
    """Incomplete or crashed replays carry no 0x22 packet. The button
    hides itself on None, so this path must not raise."""
    from renderer.stats_export import extract_match_stats

    class _NoResults:
        def battle_results(self):
            return None

    assert extract_match_stats(_NoResults(), vgd=None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stats_export.py -v -k "team or anonymize or killed or survivor or ship_name or unknown_ship or extract"`
Expected: FAIL with `ImportError: cannot import name 'build_match_stats'`

- [ ] **Step 3: Implement the dataclasses and builders**

Append to `renderer/stats_export.py`:

```python
from dataclasses import dataclass

from renderer.death_reasons import death_reason_label


@dataclass(frozen=True)
class PlayerStats:
    """One row of the statistics board. All display-ready."""

    name: str
    clan_tag: str
    ship_name: str
    ship_class: str          # DD / CA / BB / CV / SS / AUX; "" if unknown
    team: int                # display team: 0 = recorder's side, 1 = other
    is_self: bool

    damage: int
    received: int
    spotting: int
    potential: int

    kills: int
    hits: int
    shots: int
    accuracy: float | None   # None when the main battery never fired
    fires: int
    floods: int
    citadels: int
    penetrations: int
    overpens: int
    shatters: int

    crits: int
    major_crits: int
    module_breaks: int

    caps: int
    caps_reset: int
    first_spots: int
    torps_spotted: int
    planes_killed: int
    aa_damage: int
    distance_km: float
    xp: int

    hp_remaining: int
    max_health: int
    life_time_sec: int
    killed_by: str           # "" when the player survived
    killer_weapon: str       # "" when the player survived


@dataclass(frozen=True)
class MatchStats:
    """Everything the board needs. Picklable, cairo-free."""

    players: tuple[PlayerStats, ...]   # pre-sorted for display
    map_name: str
    game_type: str
    duration_sec: int
    winner_team: int                   # display team; -1 = draw/unknown
    neutral_perspective: bool          # True for dual renders (no recorder)


def _display_team(raw_team_id: int, self_team_id: int) -> int:
    """Trap 5: the recorder may be raw team 0 or 1. Display team 0 is
    always the recorder's side, so colours match the video."""
    return 0 if raw_team_id == self_team_id else 1


def _player_row(
    player: Any,
    ships_db: dict[int, dict],
    self_team_id: int,
    self_db_id: int,
    name_by_db_id: dict[int, str],
) -> PlayerStats:
    stats = player.stats
    ship_id = int(_num(stats, "vehicle_type_id"))
    ship = ships_db.get(ship_id) or {}
    species = str(ship.get("species") or "")

    killer_id = int(_num(stats, "killer_db_id"))
    survived = int(_num(stats, "remained_hp")) > 0
    killed_by = "" if survived else name_by_db_id.get(killer_id, "")
    weapon = "" if survived else death_reason_label(int(_num(stats, "killer_weapon")))

    citadels, pens, overpens, shatters = ribbon_columns(player)

    return PlayerStats(
        name=str(stats.get("name") or ""),
        clan_tag=str(stats.get("clan_tag") or ""),
        ship_name=str(ship.get("short_name") or ship.get("name") or ship_id),
        ship_class=SPECIES_TAG.get(species, ""),
        team=_display_team(int(_num(stats, "team_id")), self_team_id),
        is_self=player.db_id == self_db_id,
        damage=ship_damage(stats),
        received=total_received_damage(stats),
        spotting=int(_num(stats, "scouting_damage")),
        potential=potential_damage(stats),
        kills=int(_num(stats, "ships_killed")),
        hits=main_battery_hits(stats),
        shots=main_battery_shots(stats),
        accuracy=accuracy(stats),
        fires=int(_num(stats, "hits_fire")),
        floods=int(_num(stats, "hits_flood")),
        citadels=citadels,
        penetrations=pens,
        overpens=overpens,
        shatters=shatters,
        crits=int(_num(stats, "module_crits")),
        major_crits=int(_num(stats, "module_major_crits")),
        module_breaks=int(_num(stats, "module_breaks")),
        caps=int(_num(stats, "capture_points")),
        caps_reset=int(_num(stats, "dropped_capture_points")),
        first_spots=int(
            _num(stats, "first_ships_spotted_by_ship")
            + _num(stats, "first_ships_spotted_by_plane")
        ),
        torps_spotted=int(_num(stats, "tpds_spotted")),
        planes_killed=int(_num(stats, "planes_killed_by_ship")),
        aa_damage=int(_num(stats, "damage_airdefense")),
        distance_km=_num(stats, "distance"),
        xp=int(_num(stats, "exp")),
        hp_remaining=int(_num(stats, "remained_hp")),
        max_health=int(_num(stats, "max_health")),
        life_time_sec=int(_num(stats, "life_time_sec")),
        killed_by=killed_by,
        killer_weapon=weapon,
    )


def _anonymize(rows: list[PlayerStats]) -> list[PlayerStats]:
    """Replace names with stable positional labels and drop clan tags.

    Numbered over the display-sorted list so the same replay always
    produces the same labels.
    """
    import dataclasses

    return [
        dataclasses.replace(row, name=f"Player {i + 1}", clan_tag="")
        for i, row in enumerate(rows)
    ]


def build_match_stats(
    *,
    results: Any,
    ships_db: dict[int, dict],
    self_team_id: int,
    meta: dict[str, Any],
    flags: frozenset[str] = frozenset(),
    neutral_perspective: bool = False,
) -> MatchStats:
    """Assemble display-ready stats. Pure — no replay, no gamedata I/O."""
    name_by_db_id = {
        db_id: str(p.stats.get("name") or "")
        for db_id, p in results.players.items()
    }
    self_db_id = results.own_db_id

    rows = [
        _player_row(p, ships_db, self_team_id, self_db_id, name_by_db_id)
        for p in results.players.values()
    ]
    rows.sort(key=lambda r: (r.team, -r.damage, r.name))

    if "anonymize" in flags:
        rows = _anonymize(rows)

    raw_winner = results.common.get("winner_team_id")
    winner = (
        _display_team(int(raw_winner), self_team_id)
        if isinstance(raw_winner, int) and raw_winner >= 0
        else -1
    )

    return MatchStats(
        players=tuple(rows),
        map_name=str(meta.get("map_name") or ""),
        game_type=str(meta.get("game_type") or "Unknown"),
        duration_sec=int(meta.get("duration_sec") or 0),
        winner_team=winner,
        neutral_perspective=neutral_perspective,
    )


def extract_match_stats(
    replay: Any,
    vgd: Any,
    flags: frozenset[str] = frozenset(),
    *,
    neutral_perspective: bool = False,
) -> MatchStats | None:
    """Worker-facing entry point. Returns None when the replay carries no
    BattleResults packet — an incomplete or crashed recording."""
    results = replay.battle_results()
    if results is None or not results.players:
        return None

    self_team_id = 0
    if not neutral_perspective:
        for player in getattr(replay, "players", ()):
            if getattr(player, "relation", None) == 0:
                self_team_id = int(getattr(player, "team_id", 0) or 0)
                break

    meta = {
        "map_name": getattr(replay, "map_name", "") or "",
        "game_type": (getattr(replay, "meta", {}) or {}).get("gameType", "Unknown"),
        "duration_sec": int(getattr(replay, "duration", 0) or 0),
    }

    return build_match_stats(
        results=results,
        ships_db=(vgd.ships_db if vgd is not None else {}),
        self_team_id=self_team_id,
        meta=meta,
        flags=flags,
        neutral_perspective=neutral_perspective,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_stats_export.py -v`
Expected: 17 passed

- [ ] **Step 5: Lint and type-check**

Run: `ruff check renderer/stats_export.py && mypy renderer/stats_export.py`
Expected: no errors. Fix anything reported before committing.

- [ ] **Step 6: Commit**

```bash
git add renderer/stats_export.py tests/test_stats_export.py
git commit -m "feat(stats): PlayerStats/MatchStats assembly with display-team swap"
```

---

### Task 5: Cairo stats board

**Files:**
- Create: `renderer/stats_board.py`
- Test: `tests/test_stats_board.py`

**Interfaces:**
- Consumes: `MatchStats`, `PlayerStats` from Task 4; `renderer.themes.THEMES`.
- Produces: `renderer.stats_board.render_stats_board(stats: MatchStats, theme: str = "default") -> bytes` returning PNG bytes; `renderer.stats_board.COLUMNS: tuple[Column, ...]`.

**Design note for the implementer:** column widths are measured from rendered content, not hardcoded. Trimming a column later must be deleting one `Column` entry and nothing else — that is the whole reason this table is data rather than a sequence of draw calls.

Every numeric cell is right-aligned. Cairo's toy text API has no tabular figures, so left-aligned digits make column edges jitter row to row.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stats_board.py
"""Statistics board rendering.

The board is pure presentation: given a MatchStats it produces PNG bytes
with no parser, gamedata or Discord involvement. These tests hold that
boundary and guard the layout against regressions.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from renderer.stats_export import MatchStats, PlayerStats

pytest.importorskip("cairo")


def _player(name: str, team: int, damage: int, **kw) -> PlayerStats:
    base = dict(
        name=name, clan_tag="CL0", ship_name="Marseille", ship_class="CA",
        team=team, is_self=False, damage=damage, received=50_000,
        spotting=6_336, potential=1_077_000, kills=1, hits=46, shots=120,
        accuracy=38.3, fires=2, floods=5, citadels=1, penetrations=13,
        overpens=3, shatters=11, crits=5, major_crits=2, module_breaks=1,
        caps=12, caps_reset=4, first_spots=3, torps_spotted=7,
        planes_killed=14, aa_damage=9_000, distance_km=54.0, xp=1_805,
        hp_remaining=0, max_health=75_050, life_time_sec=635,
        killed_by="Player09", killer_weapon="HE",
    )
    base.update(kw)
    return PlayerStats(**base)


def _match(n: int = 6) -> MatchStats:
    rows = [
        _player(f"Player{i:02d}", i % 2, 150_000 - i * 10_000)
        for i in range(n)
    ]
    rows.sort(key=lambda r: (r.team, -r.damage))
    return MatchStats(
        players=tuple(rows), map_name="Tierra del Fuego",
        game_type="ClanBattle", duration_sec=581, winner_team=0,
        neutral_perspective=False,
    )


def test_returns_valid_png_bytes():
    from renderer.stats_board import render_stats_board

    data = render_stats_board(_match())
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_empty_match_does_not_crash():
    """A results packet with no usable rows must render a header-only
    sheet rather than raising out of the button handler."""
    from renderer.stats_board import render_stats_board

    empty = MatchStats(
        players=(), map_name="", game_type="Unknown", duration_sec=0,
        winner_team=-1, neutral_perspective=False,
    )
    assert render_stats_board(empty)[:8] == b"\x89PNG\r\n\x1a\n"


def test_accuracy_none_renders_without_error():
    """Submarines have accuracy=None; the formatter must not choke."""
    from renderer.stats_board import render_stats_board

    m = _match()
    rows = list(m.players)
    rows[0] = _player("Sub", 0, 99_000, accuracy=None, shots=0, hits=0)
    import dataclasses
    assert render_stats_board(dataclasses.replace(m, players=tuple(rows)))


def test_board_module_has_no_parser_or_discord_import():
    """The presentation boundary is the point of the split — hold it."""
    src = Path("renderer/stats_board.py").read_text()
    assert "wows_replay_parser" not in src
    assert "import discord" not in src
    assert "gamedata" not in src


def test_theme_changes_output():
    """Team colours must come from THEMES, not be hardcoded."""
    from renderer.stats_board import render_stats_board

    assert render_stats_board(_match(), theme="default") != \
        render_stats_board(_match(), theme="brandon")


def test_golden_image(tmp_path):
    from tests.golden_image import compare_images, load_reference
    from renderer.stats_board import render_stats_board

    if load_reference("stats_board") is None:
        pytest.skip("no baseline yet — generate with UPDATE_GOLDEN=1")

    out = tmp_path / "stats_board.png"
    out.write_bytes(render_stats_board(_match()))
    passed, mse = compare_images(out, "stats_board")
    assert passed, f"stats board drifted from baseline (mse={mse:.5f})"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stats_board.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renderer.stats_board'` (golden test skips)

- [ ] **Step 3: Implement the board**

Create `renderer/stats_board.py`. Structure:

```python
"""Post-battle statistics board — Cairo table renderer.

Pure presentation. Given a MatchStats it produces PNG bytes; it knows
nothing about replays, gamedata or Discord, which is what lets it run in
the bot process on a button click rather than in the render worker.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable

import cairo

from renderer.stats_export import MatchStats, PlayerStats
from renderer.themes import THEMES

BG = (0x0D / 255, 0x15 / 255, 0x20 / 255)
LABEL_PRIMARY = (0xE8 / 255, 0xE4 / 255, 0xD9 / 255)
LABEL_SECONDARY = (0x9B / 255, 0xA4 / 255, 0xAB / 255)
ACCENT_FIRE = (1.0, 0.55, 0.2)
ACCENT_FLOOD = (0.35, 0.65, 1.0)
ACCENT_CIT = (1.0, 0.35, 0.35)
DIM = 0.35            # alpha for zero-valued accent cells

ROW_H = 30
HEADER_H = 34
TITLE_H = 56
PAD_X = 14
COL_GAP = 18
FONT = "sans-serif"
FONT_SIZE = 15


@dataclass(frozen=True)
class Column:
    key: str                                  # PlayerStats attribute
    label: str
    fmt: Callable[[PlayerStats], str]
    align: str = "right"                      # "right" | "left"
    accent: tuple[float, float, float] | None = None   # dim when zero


def _thousands(n: int) -> str:
    return f"{n:,}"


def _mmss(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


COLUMNS: tuple[Column, ...] = (
    Column("name", "Player", lambda p: (f"[{p.clan_tag}] " if p.clan_tag else "") + p.name, "left"),
    Column("ship_name", "Ship", lambda p: (f"{p.ship_class} " if p.ship_class else "") + p.ship_name, "left"),
    Column("damage", "Dmg", lambda p: _thousands(p.damage)),
    Column("received", "Recv", lambda p: _thousands(p.received)),
    Column("spotting", "Spot", lambda p: _thousands(p.spotting)),
    Column("potential", "Potential", lambda p: _thousands(p.potential)),
    Column("kills", "K", lambda p: str(p.kills)),
    Column("hits", "Hits", lambda p: str(p.hits)),
    Column("accuracy", "Acc", lambda p: "—" if p.accuracy is None else f"{p.accuracy:.0f}%"),
    Column("fires", "Fire", lambda p: str(p.fires), accent=ACCENT_FIRE),
    Column("floods", "Flood", lambda p: str(p.floods), accent=ACCENT_FLOOD),
    Column("citadels", "Cit", lambda p: str(p.citadels), accent=ACCENT_CIT),
    Column("penetrations", "Pen", lambda p: str(p.penetrations)),
    Column("overpens", "OvP", lambda p: str(p.overpens)),
    Column("shatters", "Shtr", lambda p: str(p.shatters)),
    Column("crits", "Crit", lambda p: str(p.crits)),
    Column("major_crits", "Maj", lambda p: str(p.major_crits)),
    Column("module_breaks", "Brk", lambda p: str(p.module_breaks)),
    Column("caps", "Caps", lambda p: str(p.caps)),
    Column("caps_reset", "Rst", lambda p: str(p.caps_reset)),
    Column("first_spots", "1st", lambda p: str(p.first_spots)),
    Column("torps_spotted", "TpdSp", lambda p: str(p.torps_spotted)),
    Column("planes_killed", "Planes", lambda p: str(p.planes_killed)),
    Column("aa_damage", "AA", lambda p: _thousands(p.aa_damage)),
    Column("distance_km", "Dist", lambda p: f"{p.distance_km:.1f}km"),
    Column("xp", "XP", lambda p: _thousands(p.xp)),
    Column("hp_remaining", "HP", lambda p: "—" if not p.max_health
           else f"{100 * p.hp_remaining // p.max_health}%"),
    Column("life_time_sec", "Time", lambda p: _mmss(p.life_time_sec)),
    Column("killed_by", "Killed by", lambda p: p.killed_by or "—", "left"),
)
```

Then implement, in this order:

1. `_measure(stats) -> list[float]` — create a throwaway `cairo.ImageSurface(FORMAT_ARGB32, 1, 1)`, select `FONT` at `FONT_SIZE`, and for each column take `max(text_extents(label).width, max(text_extents(fmt(p)).width for p in players))`, plus `COL_GAP`. Returns per-column widths.
2. `_row_fill(player, theme) -> tuple[float, float, float, float]` — team colour from `THEMES[theme].team_colors[player.team]` at alpha `0.10`; `is_self` rows at alpha `0.22`.
3. `_draw_title(cr, stats, width)` — map name, game type, `_mmss(duration_sec)`, and the outcome. Outcome is `"Victory"` when `winner_team == 0`, `"Defeat"` when `winner_team == 1`, `"Draw"` when `-1` — except when `stats.neutral_perspective` is set, where it reads `"Team 1 wins"` / `"Team 2 wins"`, because a merged dual render has no recorder to be victorious.
4. `_draw_header(cr, widths, y)` — column labels in `LABEL_SECONDARY`, then a 1px separator.
5. `_draw_row(cr, player, widths, y, theme)` — fill, then each cell. Accent columns render in their accent colour when the value is non-zero and in `LABEL_SECONDARY` at `DIM` alpha when zero. `is_self` rows get a 3px left accent bar in the team colour.
6. `render_stats_board(stats, theme="default") -> bytes` — measure, size the surface as `sum(widths) + 2 * PAD_X` by `TITLE_H + HEADER_H + len(players) * ROW_H + PAD_X`, paint `BG`, draw title/header/rows with a 10px gap between the two team blocks, then `surface.write_to_png(buf)` into a `io.BytesIO` and return `buf.getvalue()`.

Right-alignment: for `align == "right"`, move to `x + width - COL_GAP / 2 - text_extents(s).width`.

Guard the empty case: when `stats.players` is empty, still emit title and header so the surface has non-zero size.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_stats_board.py -v`
Expected: 5 passed, 1 skipped (golden baseline not yet generated)

- [ ] **Step 5: Eyeball the output before baselining it**

A golden image locks in whatever it is given, including a broken layout. Look at it first.

```bash
python -c "
from tests.test_stats_board import _match
from renderer.stats_board import render_stats_board
open('/tmp/stats_board.png','wb').write(render_stats_board(_match(24)))
print('wrote /tmp/stats_board.png')
"
```
Open `/tmp/stats_board.png`. Confirm: no clipped text, numeric columns right-aligned with flush edges, team blocks visually separated, zero-valued Fire/Flood/Cit dimmed. Fix the layout before the next step if not.

- [ ] **Step 6: Generate the golden baseline**

`update_reference()` refuses to overwrite a baseline unless `UPDATE_GOLDEN=1` is set in the environment, so the assignment must precede the command:

```bash
UPDATE_GOLDEN=1 python -c "
from pathlib import Path
from tests.test_stats_board import _match
from tests.golden_image import update_reference
from renderer.stats_board import render_stats_board
p = Path('/tmp/stats_board_ref.png')
p.write_bytes(render_stats_board(_match()))
print(update_reference(p, 'stats_board'))
"
```

Then re-run: `python -m pytest tests/test_stats_board.py -v`
Expected: 6 passed, 0 skipped

- [ ] **Step 7: Lint and commit**

```bash
ruff check renderer/stats_board.py && mypy renderer/stats_board.py
git add renderer/stats_board.py tests/test_stats_board.py tests/golden_images/stats_board.png
git commit -m "feat(stats): cairo statistics board renderer"
```

---

### Task 6: Worker returns a RenderResult

The worker returns an 8-tuple unpacked positionally at three sites. Appending a ninth element invites a silent mis-order, and `tests/test_worker_signature.py:25` only *comments* about the arity contract. Replace the tuple with a dataclass and give the contract a real assertion.

**Files:**
- Modify: `bot/worker.py:67-238` (`render_replay`), `bot/worker.py:241-364` (`render_dual_replay`)
- Modify: `tests/test_worker_signature.py`

**Interfaces:**
- Consumes: `extract_match_stats` from Task 4.
- Produces: `bot.worker.RenderResult` — frozen dataclass with fields `output_path: str`, `duration: float`, `timings: dict`, `game_version: str`, `num_players: int`, `game_type: str`, `build_urls: list`, `chat_text: str`, `stats: MatchStats | None`. Both worker functions return it.

- [ ] **Step 1: Write the failing test**

```python
# replace tests/test_worker_signature.py's arity comment with real assertions;
# append these tests.


def test_render_result_field_order_is_pinned():
    """The cog reads these by name now, but pickling across the process
    pool and any future positional construction still depend on the
    field set. Adding a field is fine; renaming or dropping one is not."""
    import dataclasses

    from bot.worker import RenderResult

    assert dataclasses.is_dataclass(RenderResult)
    assert [f.name for f in dataclasses.fields(RenderResult)] == [
        "output_path", "duration", "timings", "game_version",
        "num_players", "game_type", "build_urls", "chat_text", "stats",
    ]


def test_render_result_is_picklable():
    """It crosses a ProcessPoolExecutor boundary."""
    import pickle

    from bot.worker import RenderResult

    r = RenderResult(
        output_path="/tmp/a.mp4", duration=1.0, timings={}, game_version="15.6",
        num_players=24, game_type="RandomBattle", build_urls=[], chat_text="",
        stats=None,
    )
    assert pickle.loads(pickle.dumps(r)).game_version == "15.6"


def test_render_result_stats_defaults_to_none():
    """A replay with no BattleResults packet must construct cleanly."""
    import dataclasses

    from bot.worker import RenderResult

    stats_field = next(
        f for f in dataclasses.fields(RenderResult) if f.name == "stats"
    )
    assert stats_field.default is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_worker_signature.py -v`
Expected: FAIL with `ImportError: cannot import name 'RenderResult' from 'bot.worker'`

- [ ] **Step 3: Add the dataclass**

At module level in `bot/worker.py`, after the `PRESETS` constant:

```python
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from renderer.stats_export import MatchStats


@dataclass(frozen=True)
class RenderResult:
    """What a render worker hands back to the cog.

    A dataclass rather than a tuple: this is unpacked at three call sites
    in cog_render.py, and a positional tuple makes a silent mis-order the
    likely failure mode when a field is added.
    """

    output_path: str
    duration: float
    timings: dict
    game_version: str
    num_players: int
    game_type: str
    build_urls: list
    chat_text: str
    stats: "MatchStats | None" = None
```

- [ ] **Step 4: Extract stats in `render_replay`**

In `bot/worker.py`, immediately after `chat_text = _format_chat_log(replay)` (currently line 232), add:

```python
    # Post-battle stats for the Statistics button. Mirrors the build_urls
    # treatment above: a schema break after a WoWs patch must degrade to a
    # missing button, never a failed render.
    t_stats = perf_counter()
    stats = None
    try:
        from renderer.stats_export import extract_match_stats
        stats = extract_match_stats(replay, vgd, flags)
    except Exception:
        log.exception("worker: stats extraction failed")
    timings["stats"] = perf_counter() - t_stats
    log.info(
        "worker: stats %s in %.2fs",
        "extracted" if stats else "unavailable", timings["stats"],
    )
```

Replace the closing `return (...)` tuple with:

```python
    return RenderResult(
        output_path=output_path,
        duration=replay.duration,
        timings=timings,
        game_version=replay.game_version,
        num_players=len(replay.players),
        game_type=game_type,
        build_urls=build_urls,
        chat_text=chat_text,
        stats=stats,
    )
```

Update the return type annotation on `render_replay` from the tuple to `RenderResult`, and update its docstring `Returns:` block to name the dataclass.

- [ ] **Step 5: Do the same for `render_dual_replay`**

After `chat_text = _format_chat_log(merged)` (currently line 358):

```python
    t_stats = perf_counter()
    stats = None
    try:
        from renderer.stats_export import extract_match_stats
        # Either replay's BattleResults covers every player in the match;
        # A is tried first, B only if A's recorder left before the packet.
        stats = extract_match_stats(
            replay_a, vgd, flags, neutral_perspective=True,
        ) or extract_match_stats(
            replay_b, vgd, flags, neutral_perspective=True,
        )
    except Exception:
        log.exception("worker: dual stats extraction failed")
    timings["stats"] = perf_counter() - t_stats
```

Replace its return tuple with a `RenderResult` built the same way, using `merged.duration`, `merged.game_version`, `len(merged.players)`, `build_urls=[]`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_worker_signature.py -v`
Expected: 6 passed

- [ ] **Step 7: Commit**

```bash
git add bot/worker.py tests/test_worker_signature.py
git commit -m "refactor(bot): worker returns RenderResult dataclass, carrying match stats"
```

---

### Task 7: Statistics button

**Files:**
- Modify: `bot/cog_render.py:138-199` (`_RenderResultView`)
- Modify: `bot/cog_render.py:459-462`, `:676-679`, `:1144-1147` (unpack sites)
- Modify: `bot/cog_render.py:503-506`, `:1179-1182` (view construction)
- Test: `tests/test_stats_button.py`

**Interfaces:**
- Consumes: `bot.worker.RenderResult` (Task 6), `renderer.stats_board.render_stats_board` (Task 5), `renderer.stats_export.MatchStats` (Task 4).
- Produces: `_RenderResultView(..., stats: MatchStats | None, theme: str)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stats_button.py
"""The Statistics button follows the same presence rule as Download Chat:
present when there is something to show, removed otherwise."""
from __future__ import annotations

import pytest

pytest.importorskip("discord")

from bot.cog_render import _RenderResultView
from renderer.stats_export import MatchStats


def _view(stats):
    return _RenderResultView(
        build_urls=[], chat_text="", chat_filename="c.txt",
        stats=stats, theme="default",
    )


def _labels(view):
    return {item.label for item in view.children}


def test_button_removed_when_no_stats():
    assert "Statistics" not in _labels(_view(None))


def test_button_present_when_stats_available():
    stats = MatchStats(
        players=(), map_name="Ocean", game_type="RandomBattle",
        duration_sec=600, winner_team=0, neutral_perspective=False,
    )
    assert "Statistics" in _labels(_view(stats))


def test_view_with_nothing_to_offer_has_no_children():
    """cog_render only attaches the view when it has children; an empty
    view must stay empty so no bare button row is posted."""
    assert _view(None).children == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stats_button.py -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'stats'`

- [ ] **Step 3: Extend the view**

In `bot/cog_render.py`, change `_RenderResultView.__init__` to accept the two new keyword arguments and store them, then remove the button when there is nothing to render:

```python
    def __init__(
        self,
        *,
        build_urls: list[tuple[str, str, int, str | None]],
        chat_text: str,
        chat_filename: str,
        stats: "MatchStats | None" = None,
        theme: str = "default",
    ) -> None:
        super().__init__(timeout=RESULT_VIEW_TIMEOUT_S)
        self._build_urls = build_urls
        self._chat_text = chat_text
        self._chat_filename = chat_filename
        self._stats = stats
        self._theme = theme
        self.message: discord.Message | None = None
        if not build_urls:
            self.remove_item(self.show_builds)
        if not chat_text:
            self.remove_item(self.download_chat)
        if stats is None:
            self.remove_item(self.show_statistics)
```

Add the import at the top of the file:

```python
from renderer.stats_export import MatchStats
```

Add the button after `download_chat`:

```python
    @discord.ui.button(label="Statistics", style=discord.ButtonStyle.secondary)
    async def show_statistics(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        button.disabled = True
        # Defer first: cairo needs a moment and Discord expects a response
        # within 3s. Rendering goes to a thread because the bot exports
        # event-loop lag as a metric — a ~100ms synchronous draw here would
        # show up as a regression on that panel.
        await interaction.response.defer()
        assert self._stats is not None
        png = await asyncio.to_thread(
            render_stats_board, self._stats, self._theme,
        )
        file = discord.File(io.BytesIO(png), filename="match_statistics.png")
        await interaction.followup.send(file=file)
        await interaction.message.edit(view=self)
```

Add the render import at the top of `show_statistics`'s module scope:

```python
from renderer.stats_board import render_stats_board
```

- [ ] **Step 4: Update the three unpack sites**

At `bot/cog_render.py:459-462`, replace:

```python
            (
                _, replay_duration, timings, game_version, num_players,
                game_type, build_urls, chat_text,
            ) = await future
```
with:
```python
            result = await future
            replay_duration = result.duration
            timings = result.timings
            game_version = result.game_version
            num_players = result.num_players
            game_type = result.game_type
            build_urls = result.build_urls
            chat_text = result.chat_text
```

At `:676-679` (inside `asyncio.wait_for`), replace the tuple unpack with:

```python
                result = await asyncio.wait_for(future, timeout=timeout)
```

At `:1144-1147`, the same.

Then, at each of the three sites, bind **only the names the surrounding code already uses** — the existing unpacks show which those are, and the ones prefixed `_` (`_num_players`, `_build_urls`, `_chat_text`) were deliberately discarded, so do not bind them. Everywhere else, either bind the local or read `result.<field>` inline at the use site. Ruff's F841 will flag any unused local you introduce, so run `ruff check bot/cog_render.py` after this step and before the next.

- [ ] **Step 5: Pass stats into both view constructions**

At `bot/cog_render.py:503-506`:

```python
                    result_view = _RenderResultView(
                        build_urls=build_urls, chat_text=chat_text,
                        chat_filename=chat_filename,
                        stats=result.stats, theme=theme,
                    )
```

At `:1179-1182` the same, with `build_urls=[]`.

Confirm `theme` is in scope at both sites — it is the slash-command parameter. If a site shadows it, pass the command's value explicitly rather than renaming.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_stats_button.py tests/test_worker_signature.py -v`
Expected: 9 passed

- [ ] **Step 7: Full suite + lint**

Run: `python -m pytest tests/ -q && ruff check . && mypy renderer bot`
Expected: no failures; skips only where replay/gamedata fixtures are absent.

- [ ] **Step 8: Commit**

```bash
git add bot/cog_render.py tests/test_stats_button.py
git commit -m "feat(bot): Statistics button posting the post-battle stats board"
```

---

### Task 8: Documentation

Three docs are stale or wrong once this lands. The spec correction matters most — it currently describes a column this plan deliberately drops, for a reason the spec gets wrong.

**Files:**
- Modify: `docs/superpowers/specs/2026-08-11-statistics-board-design.md`
- Modify: `CLAUDE.md`
- Modify: `CHANGELOG.md`
- Modify: `README.md`

- [ ] **Step 1: Correct the spec's `Pts` section**

Replace the "Open item — `Pts` units" section with a finding, and drop the `Pts` row from the column table (leaving 29 columns):

```markdown
## Resolved — `Pts` dropped from v1

`Σ victory_points_*` is **not** a scaled team score, so the planned `Pts`
column was cut before implementation.

Evidence from the 14-player sample: per-team sums are 261,720 and 34,660 in
a match that scores to 1000, which is not a clean multiple; the figure does
not reconcile after removing the end-of-match `victory_points_victory_*`
bonuses either; `victory_points_cp_hold = 34,800` appears **identically on
two different players**; and `23,400` appears on one player from *each*
team.

These are reward/bonus points partly duplicated across teammates, not
per-player score contribution. A column whose per-row meaning cannot be
stated does not ship. `Caps` and `Rst` already carry unambiguous objective
signal. Revisit if the semantics are ever pinned down.
```

- [ ] **Step 2: Footnote the CLAUDE.md damage-breakdown claim**

`CLAUDE.md`'s "Damage Breakdown → Limitations" section says per-player typed damage is "not possible". That is true of the live wire stream and false of the post-battle packet. Append to that section:

```markdown
**Post-battle exception.** The above describes the *live wire stream*.
`BattleResults` (packet `0x22`, via `replay.battle_results()`) does carry a
full typed damage breakdown for **every** player — `damage_main_ap/he/cs`,
`damage_tpd_*`, `damage_fire`, `damage_flood`, `damage_ram` and ~40 more
weapon categories. It is only unavailable *during* the match, and absent
entirely from replays that end before the results packet. See
`renderer/stats_export.py`.
```

- [ ] **Step 3: Add the layer/feature docs**

In `CLAUDE.md`'s architecture tree, add under `renderer/`:

```
│   ├── stats_export.py         # BattleResults → PlayerStats/MatchStats (no cairo)
│   ├── stats_board.py          # MatchStats → PNG stats board (no parser/gamedata)
│   ├── death_reasons.py        # Shared DEATH_REASON id → (label, icon) table
```

In the "Discord Bot" section, document the third button alongside the existing two.

- [ ] **Step 4: CHANGELOG entry**

Add under the unreleased heading, matching the file's existing style:

```markdown
### Added
- **Statistics button** on render results — posts a 29-column post-battle
  stats board covering every player in the match, rendered from the `0x22`
  BattleResults packet. Hidden when a replay ends before that packet
  arrives. Respects the `anonymize` flag and the theme dropdown.

### Changed
- Render workers return a `RenderResult` dataclass instead of an 8-tuple.
```

- [ ] **Step 5: README feature list**

Add the Statistics button to the bot's documented buttons.

- [ ] **Step 6: Verify docs match reality**

Run: `python -m pytest tests/ -q`
Then confirm by reading that the column count claimed in CHANGELOG matches `len(COLUMNS)`:
```bash
python -c "from renderer.stats_board import COLUMNS; print(len(COLUMNS))"
```
Expected: `29`

- [ ] **Step 7: Commit**

```bash
git add docs/ CLAUDE.md CHANGELOG.md README.md
git commit -m "docs: statistics board; correct the per-player typed-damage limitation"
```

---

## Verification

End-to-end, after all tasks:

```bash
# 1. Whole suite, lint, types
python -m pytest tests/ -q
ruff check .
mypy renderer bot

# 2. Real replay → real board. Uses the repo-root replays and the
#    gamedata submodule; no Discord involved.
python -c "
from pathlib import Path
from wows_replay_parser import parse_replay
from renderer.gamedata_cache import resolve_for_replay
from renderer.stats_export import extract_match_stats
from renderer.stats_board import render_stats_board

p = '20260723_221703_PVSC110-San-Martin_22_tierra_del_fuego.wowsreplay'
vgd = resolve_for_replay(p, Path('wows-gamedata'))
replay = parse_replay(p, str(vgd.entity_defs_path))
stats = extract_match_stats(replay, vgd)
print(f'{len(stats.players)} players, winner=display team {stats.winner_team}')
for row in stats.players[:3]:
    print(f'  {row.clan_tag:6} {row.name:18} {row.ship_name:14} '
          f'dmg={row.damage:>7,} recv={row.received:>7,} acc={row.accuracy}')
Path('/tmp/board_real.png').write_bytes(render_stats_board(stats))
print('wrote /tmp/board_real.png')
"
```

Open `/tmp/board_real.png` and check against the reference screenshot in the spec: names and ships readable, numeric columns flush right, teams separated, the recorder's row highlighted, Fire/Flood/Cit dimmed at zero and coloured when set.

**Cross-check three numbers by hand** against the postbattle JSON — this is what catches a formula that is wrong but plausible:

```bash
python -c "
import json
d = json.load(open('20260723_221703_PVSC110-San-Martin_22_tierra_del_fuego.postbattle.json'))
for p in d['players'].values():
    s = p['stats']
    if s['name'] == 'Lucytus':
        print('damage      ', s['damage'], '(board must show 85,382, not 111,818)')
        print('received    ', int(sum(v for k,v in s.items()
              if k.startswith('received_damage_') and isinstance(v,(int,float)))))
        print('life_time   ', s['life_time_sec'])
"
```

Then, with a bot token in `.env`:

```bash
python -m bot.main
```
In Discord: `/render` with a completed replay → three buttons appear → click **Statistics** → a PNG posts within a few seconds and the button greys out. Repeat with `theme: Brandon` and confirm the row tints turn cyan/magenta. Repeat with `flags: anonymize` and confirm names read `Player 1..N`. Finally, render a replay whose recorder left early and confirm only two buttons appear.

## Self-Review Notes

- **Spec coverage.** Architecture → Tasks 2-5; `death_reasons` extraction → Task 1; `RenderResult` → Task 6; button + failure behaviour → Task 7; docs including the CLAUDE.md footnote → Task 8. The spec's `Pts` open item is resolved by deletion in Task 8 Step 1, with the evidence recorded.
- **Ribbon offset open item** is covered by `test_ribbon_columns_zero_out_on_short_rows` (Task 3) rather than left as a runtime hope.
- **Type consistency.** `MatchStats`/`PlayerStats` field names are defined once in Task 4 and referenced unchanged in Tasks 5-7; `RenderResult`'s field list appears identically in Task 6's test and dataclass; `ribbon_columns` returns the same 4-tuple order everywhere.
- **Known deferral.** `anonymize` currently only suppresses names on the minimap (`renderer/layers/ships.py:197`) — the team roster panel still shows them. This plan makes the stats board honour the flag but does not change the roster layer, which is pre-existing behaviour and out of scope.
