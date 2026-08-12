# Statistics Board — Design

**Status:** Approved — ready for implementation
**Date:** 2026-08-11

## Goal

Add a third button, **Statistics**, to the render result view alongside `Show Builds`
and `Download Chat`. Clicking it posts a single wide PNG: a post-battle stats board
covering every player in the match, in the style of the WoWS end-of-battle screen.

## Motivation

The video shows what happened; it does not let anyone compare players afterwards.
The replay already carries a complete, server-authoritative post-battle results
packet for **all** players — not just the recorder — and the bot currently throws
it away. Surfacing it costs one button and no new data source.

## Data source

`ParsedReplay.battle_results()` decodes packet `0x22` into
`wows_replay_parser.battle_results.BattleResults`, which exposes
`players: dict[db_id, PlayerBattleResult]` with **466 named fields per player**
plus per-player ribbon tallies read from the raw row tail.

Verified against a real 14-player clan-battle sample
(`20260723_221703_PVSC110-San-Martin_22_tierra_del_fuego`). Every column in the
reference screenshot except PR resolves to a field or a small sum of fields.

### Notable capability unlock

`CLAUDE.md` currently states that per-player typed damage breakdown for all
players is "not possible (game protocol limitation)". That is true of the **live
wire stream** — `receiveDamageStat` carries ammo type only for the recording
player. It is **not** true of `battle_results`, which carries
`damage_main_ap/he/cs`, `damage_tpd_*`, `damage_fire`, `damage_flood`,
`damage_ram` and ~40 more per weapon category, for every player.

`CLAUDE.md` must gain a footnote to that effect as part of this work. The
distinction is live-stream vs post-battle packet, not present vs absent.

## Two verified traps

Both were found by running the formulas against the real sample. Both would have
produced plausible-looking wrong numbers.

### Trap A — `damage` is authoritative; do not sum `damage_*`

`sum(v for k, v in stats.items() if k.startswith("damage_"))` overshoots the true
ship damage for any player who shot down aircraft, because `damage_airdefense`
and `damage_planes_by_plane` share the prefix but are not ship damage.

Observed in 5 of 14 sample players:

| Player | `damage` | naive `Σ damage_*` |
|---|---|---|
| Lucytus | 85,382 | 111,818 |
| tobitobiXX | 100,413 | 121,184 |
| _Cpt_Marv_ | 61,652 | 82,694 |
| Unholy_Jinx | 7,930 | 22,171 |
| Frissdas | 43,919 | 45,034 |

**Use the `damage` field directly.**

### Trap B — `Recv Dmg` is the received sum, not HP lost

`max_health - remained_hp` under-reports for any ship that healed. BlueMidhir
received 86,887 but lost only 75,050 HP; repair party covered the difference.
The reference screenshot confirms the sum is the intended semantic — it shows
Slava receiving 130,357 on a ship with roughly 97k HP.

**Use `Σ received_damage_*`.** That prefix is precise: it matches only damage
fields, including `received_damage_from_buildings_*`, and excludes
`received_hits_*` and `received_module_*`.

## Architecture

Extraction and rendering are separate modules in separate processes.

```
worker process (already holds the parsed replay + gamedata)
  renderer/stats_export.py
      extract_match_stats(replay, vgd, flags) -> MatchStats | None
      - reads replay.battle_results(); None if the packet is absent
      - resolves ship names via vgd.ships_db, killer names via the roster
      - returns frozen dataclasses: no Cairo, ~24x30 numbers, cheap to pickle
  returned as a field on RenderResult

bot process (runs only when the button is clicked)
  renderer/stats_board.py
      render_stats_board(stats, theme) -> bytes
      - pure Cairo presentation; no parser, no gamedata, no Discord
      - invoked through asyncio.to_thread so the event loop does not stall
```

**Why the split.** Extraction needs the parsed replay and the gamedata cache,
both already paid for inside the worker. Rendering is pure presentation and most
renders will never have the button clicked, so doing it eagerly would burn
~100 ms and several hundred KB per render for nothing. The boundary also gives
each half the test strategy it wants: extraction is fixture-driven with no Cairo
dependency, rendering is a golden image through the existing
`tests/golden_image.py`.

`renderer/` gains no new third-party dependency. `stats_board.py` reuses
`Layer.get_cached_text` for text metrics and `renderer.themes.THEMES` for colors.

## Components

### `renderer/stats_export.py` (new)

```python
@dataclass(frozen=True)
class PlayerStats:
    name: str
    clan_tag: str
    ship_name: str
    ship_class: str            # two-letter tag: DD / CA / BB / CV / SS
    team: int                  # display team: 0 ally, 1 enemy
    is_self: bool
    # ... one field per column in the table below
    killed_by: str             # "" when the player survived
    killer_weapon: str         # DEATH_REASON label, "" when survived


@dataclass(frozen=True)
class MatchStats:
    players: tuple[PlayerStats, ...]     # pre-sorted for display
    map_name: str
    game_type: str
    duration_sec: int
    winner_team: int                     # display team, -1 = draw
    neutral_perspective: bool            # True for dual renders (no recorder)


def extract_match_stats(replay, vgd, flags) -> MatchStats | None: ...
```

No Cairo import. No Discord import. Frozen and picklable so it crosses the
`ProcessPoolExecutor` boundary unchanged.

### `renderer/stats_board.py` (new)

```python
def render_stats_board(stats: MatchStats, theme: str = "default") -> bytes: ...
```

Renders to a `cairo.ImageSurface`, writes PNG to an in-memory buffer, returns the
bytes. No file I/O, so the cog can hand the buffer straight to `discord.File`.

### `renderer/death_reasons.py` (new, extracted)

`_DEATH_REASON` currently lives in `renderer/layers/killfeed.py:11`. The stats
board needs the same table for the "Killed by" column. Move it to a shared module
and have `killfeed.py` import it, so there is one table rather than two that
drift apart at the next patch.

### `bot/worker.py` (changed)

Both worker functions currently return an 8-tuple, unpacked positionally at three
sites in the cog (`cog_render.py` lines 461, 678, 1146). Growing it to 9 makes a
silent mis-order more likely, and `tests/test_worker_signature.py:25` only
*comments* about the arity contract without asserting it.

Replace the tuple with a frozen dataclass:

```python
@dataclass(frozen=True)
class RenderResult:
    output_path: str
    duration: float
    timings: dict
    game_version: str
    num_players: int
    game_type: str
    build_urls: list
    chat_text: str
    stats: MatchStats | None
```

Defined at module level in `bot/worker.py` so it pickles across the pool. The
three unpack sites become attribute access. `test_worker_signature.py` gains a
real field assertion.

Extraction is wrapped in try/except and logged on failure, mirroring how
`build_urls` is already handled — a schema break after a WoWs patch must degrade
to a missing button, never a failed render.

### `bot/cog_render.py` (changed)

`_RenderResultView` gains a `stats: MatchStats | None` parameter and a third
button:

```python
@discord.ui.button(label="Statistics", style=discord.ButtonStyle.secondary)
async def show_statistics(self, interaction, button) -> None: ...
```

Follows the existing pattern exactly: `button.disabled = True`, respond with the
file, then `await interaction.message.edit(view=self)`. When `stats is None` the
button is removed in `__init__`, the same way `download_chat` is removed for
chatless replays.

PNG generation runs through `asyncio.to_thread`. The bot tracks
`wows_bot_event_loop_lag_seconds` as a monitored metric, so a ~100 ms synchronous
Cairo render on the event loop would be a visible regression.

## Columns

29 columns, one row per player, grouped left to right. `R` marks a value read
from the ribbon tail rather than a named field.

| Group | Column | Source |
|---|---|---|
| Identity | Player | `clan_tag` + `name` |
| | Ship | `ships_db[vehicle_type_id]` localized name |
| Damage | Dmg | `damage` (Trap A) |
| | Recv | `Σ received_damage_*` (Trap B) |
| | Spot | `scouting_damage` |
| | Pot | `agro_art + agro_tpd + agro_air + agro_dbomb` |
| Gunnery | K | `ships_killed` |
| | Hits | `Σ hits_main_{ap,cs,he}` |
| | Acc | Hits / `Σ shots_main_{ap,cs,he}`; renders `—` when shots is 0 |
| | Fire | `hits_fire` |
| | Flood | `hits_flood` |
| | Cit | ribbon 8 `R` |
| | Pen | ribbon 15 `R` |
| | OvP | ribbon 14 `R` |
| | Shtr | ribbon 16 `R` |
| Crits | Crit | `module_crits` |
| | Maj | `module_major_crits` |
| | Brk | `module_breaks` |
| Objectives | Caps | `capture_points` |
| | Rst | `dropped_capture_points` |
| | 1st | `first_ships_spotted_by_ship + first_ships_spotted_by_plane` |
| | TpdSp | `tpds_spotted` |
| | Planes | `planes_killed_by_ship` |
| | AA | `damage_airdefense` |
| | Dist | `distance` (km, one decimal) |
| | XP | `exp` |
| Survival | HP | `remained_hp / max_health` — mini bar plus percentage |
| | Time | `life_time_sec` as mm:ss |
| | Killed by | `killer_db_id` → roster name, `killer_weapon` → DEATH_REASON label; `—` when survived |

### Deliberately excluded

- **PR (Personal Rating).** The only screenshot column with no in-replay source.
  It needs per-ship expected damage / frags / winrate from an external service,
  which would add a network dependency to the container and a staleness path when
  new ships ship. Single-battle PR is also dominated by the win term — winrate is
  0% or 100% at n=1 — so it would rank the winning team above the losing team more
  than it ranks skill. Revisit as its own feature if it turns out to be missed.
- **Achievements.** Available as `stats["achievements"]`, but it is a list of icon
  ids, not a scalar. It does not fit a numeric column and would need its own row
  treatment.

## Resolved — `Pts` dropped from v1

`Σ victory_points_*` is **not** a scaled team score, so the planned `Pts`
column was cut before implementation.

Evidence from the 14-player sample: per-team sums are 261,720 and 34,660 in
a match that scores to 1000, which is not a clean multiple; the figure does
not reconcile after removing the end-of-match `victory_points_victory_*`
bonuses either; `victory_points_cp_hold = 34,800` appears **identically on
two different players**; and `23,400` appears on one player from each
team.

These are reward/bonus points partly duplicated across teammates, not
per-player score contribution. A column whose per-row meaning cannot be
stated does not ship. `Caps` and `Rst` already carry unambiguous objective
signal. Revisit if the semantics are ever pinned down.

## Open item — ribbon slot base index

`PlayerBattleResult.ribbon_count()` reads `raw[481 + ribbon_id]`. That offset was
extracted from build 12267945 (patch 15.3) and the parser's own docstring warns it
may differ on earlier 15.x builds. Cit / Pen / OvP / Shtr all depend on it.

Extraction asserts the raw row is 538 elements long — the sample confirms this
length — and returns zeros for the four ribbon columns rather than garbage if the
row is shorter or the schema has moved. A missing column beats a wrong one.

## Visual design

A dark sheet in the video's palette, roughly 2800 × 1150 px for a 24-player match.

**Structure.** Title bar carries map name, game type, outcome, duration and date.
The outcome reads Victory / Defeat when `winner_team == 0`, since display team 0
is always the recorder's team after the perspective swap; on dual renders
`neutral_perspective` is set and it reads `Team 1 wins` / `Team 2 wins` instead,
because a merged view has no recorder to be victorious. Then a header row of
column labels, then teams as two blocks — the recorder's team first — with
players sorted by damage descending inside each block. The existing watermark
sits bottom right.

The Ship column is prefixed with the two-letter class tag from `ship_class`
(`DD`, `CA`, `BB`, `CV`, `SS`) in `label-secondary` grey. It is a text tag rather
than the minimap's class icon on purpose: icons live in the gamedata tree, and
loading them would give `stats_board.py` a gamedata dependency and undo the clean
presentation-only boundary. Icons are a later option if the tag proves too weak.

**Color.** Ground is `#0D1520`, matching the video's `sea-bg`. Ally rows carry a
faint green wash and enemy rows a faint red one, taken from `THEMES[theme]` so the
`brandon` cyan/magenta dropdown carries through unchanged. The recording player's
row gets a brighter fill plus a left accent bar. Fire, Flood, Cit and Detonation
tint warm when non-zero and drop to about 35% grey at zero — that contrast is what
makes the reference screenshot scannable rather than a wall of digits.

**Alignment.** Every numeric cell is right-aligned. Cairo's toy text API has no
tabular figures, so left-aligned digits would make column edges visibly jitter
from row to row. Text cells (Player, Ship, Killed by) are left-aligned and
ellipsised at their column width.

**Sizing.** Column widths are computed from measured content extents at render
time rather than hardcoded, so trimming a column later is deleting one entry from
the column list and nothing else. This is what makes "ship it full, degrade later"
cheap.

## Failure behavior

| Condition | Behavior |
|---|---|
| No `0x22` packet (recorder quit early, crashed replay) | `extract_match_stats` returns `None`; button removed in `__init__` |
| Extraction raises | Caught and logged in the worker; `stats=None`; render still succeeds |
| Unknown `vehicle_type_id` | Falls back to the raw ship index string |
| `shots_main == 0` (subs, pure torp boats) | Acc renders `—`, never a division by zero |
| Player survived | Killed by renders `—` |
| `anonymize` flag set | Names become `Player 1..N`, clan tags dropped |
| `/render_dual` | Uses replay A's results — they already cover all players — with B as fallback |

A 2800 × 1150 PNG lands around 300–500 KB, so Discord's 25 MB attachment ceiling
is never in play and no oversize path is needed.

## Testing

**Fixture.** The sample's `raw` dict round-trips through `BattleResults._decode`,
so tests build a real 538-field, 14-player `BattleResults` with no gamedata and no
parsing. Store it under `tests/fixtures/`.

**`tests/test_stats_export.py`** — Trap A (`damage` field, not the `damage_*` sum,
verified against the five known-divergent sample players); Trap B (`Recv` as the
received sum, not HP lost, verified against BlueMidhir's 86,887 vs 75,050);
accuracy when `shots_main == 0`; ribbon columns zeroed when the raw row is short;
anonymize; the `None` path when there is no results packet; display sort order.

**`tests/test_stats_board.py`** — one golden image through the existing
`golden_image.py` helper, plus a check that a zero-player `MatchStats` does not
crash the renderer.

**`tests/test_worker_signature.py`** — replace the arity comment with a real
`RenderResult` field assertion.

**Cog test** — the Statistics button is absent from the view when `stats is None`
and present when it is not.

## Out of scope

- PR column and any external expected-values fetch.
- Per-target damage matrix. `stats["interactions"]` maps target db_id to a
  per-weapon damage array, which would make a genuinely interesting who-hit-whom
  matrix, but it is a second visualization rather than a column on this one.
- Achievements row.
- A `/stats` slash command that takes a replay without rendering a video. Cheap to
  add later on top of these two modules, but not part of this work.
