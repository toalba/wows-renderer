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

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

from renderer.death_reasons import death_reason_label

log = logging.getLogger(__name__)

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


def ribbons_readable(player: Any) -> bool:
    """Whether this player's ribbon tail can be trusted.

    Same 538-element check ribbon_columns uses. Pre-15.3 replays carry
    shorter rows, and `PlayerBattleResult.ribbon_counts()` has no guard of
    its own — it happily reads whatever integers sit at ``raw[481 + id]``
    and returns them. On a 503-element row that yields a near-empty dict
    with the occasional bogus entry, which renders as a plausible-looking
    but wrong ribbon strip.
    """
    return len(getattr(player, "raw", ())) >= BR_ROW_LEN


def player_ribbons(player: Any) -> tuple[tuple[int, int], ...]:
    """Every ribbon this player earned, as (ribbon_id, count) pairs.

    Empty when the row is too short to trust — callers distinguish that
    from "earned nothing" via :func:`ribbons_readable`.
    """
    if not ribbons_readable(player):
        return ()
    return tuple(sorted(player.ribbon_counts().items()))


def resolve_ribbon_icons(gui_dir: Any) -> tuple[tuple[int, str], ...]:
    """(ribbon_id, absolute png path) for every ribbon icon on disk.

    Runs worker-side on purpose. The id -> filename mapping needs the
    parser's ``RIBBON_WIRE_IDS``, and the files live under the versioned
    gamedata tree — both dependencies the board must not have. Resolving to
    plain paths here means the board only ever opens files.

    Returns an empty tuple if the directory is missing or unreadable; the
    board falls back to text in that case rather than failing.
    """
    from pathlib import Path

    gui = Path(gui_dir)
    if not gui.is_dir():
        log.warning("stats: ribbon gui dir missing (%s); board falls back to text", gui)
        return ()
    try:
        from renderer.layers.ribbons import _build_icon_paths
    except Exception:
        # Broad on purpose: this runs on the path to a Discord button, and a
        # parser-side break must degrade the board rather than fail a render.
        # Logged because a silent permanent fallback is otherwise invisible.
        log.exception("stats: cannot import ribbon icon mapping")
        return ()

    out: list[tuple[int, str]] = []
    try:
        for rid, rel in _build_icon_paths(gui).items():
            path = gui / rel
            if path.is_file():
                out.append((rid, str(path)))
    except Exception:
        log.exception("stats: ribbon icon resolution failed under %s", gui)
        return ()
    if not out:
        log.warning("stats: no ribbon icons found under %s", gui)
    return tuple(sorted(out))


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

    # Every ribbon this player earned, as (ribbon_id, count) pairs in
    # descending-id-agnostic wire order. A tuple rather than a dict so the
    # dataclass stays frozen and hashable. Empty when the results row is too
    # short for the ribbon offset to be trustworthy — see ribbons_available
    # on MatchStats, which distinguishes "earned none" from "cannot read".
    ribbons: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class MatchStats:
    """Everything the board needs. Picklable, cairo-free."""

    players: tuple[PlayerStats, ...]   # pre-sorted for display
    map_name: str
    game_type: str
    duration_sec: int
    winner_team: int                   # display team; -1 = draw/unknown
    neutral_perspective: bool          # True for dual renders (no recorder)

    # False when this replay's results rows are too short for the ribbon
    # tail offset to be trustworthy (pre-15.3 builds). The board must say
    # "unavailable" rather than draw an empty strip, because an empty strip
    # is indistinguishable from a player who earned nothing.
    ribbons_available: bool = False

    # (ribbon_id, absolute png path) for every ribbon icon that exists on
    # disk. Resolved in the worker, which already has the parser and the
    # versioned gamedata; the board only opens files. That is what keeps
    # stats_board free of parser and gamedata imports while still drawing
    # real ribbon art.
    ribbon_icons: tuple[tuple[int, str], ...] = ()


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
    neutral_perspective: bool,
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
    ribbons = player_ribbons(player)

    return PlayerStats(
        name=str(stats.get("name") or ""),
        clan_tag=str(stats.get("clan_tag") or ""),
        ship_name=str(ship.get("short_name") or ship.get("name") or ship_id),
        ship_class=SPECIES_TAG.get(species, ""),
        team=_display_team(int(_num(stats, "team_id")), self_team_id),
        # A merged dual render has no recording player — that's the whole
        # meaning of neutral_perspective — so no row may be highlighted as
        # self there. Without this guard, self_db_id defaults to whichever
        # replay happened to win the A/B fallback in _extract_dual_stats,
        # making the highlighted row non-deterministic across runs.
        is_self=(not neutral_perspective) and player.db_id == self_db_id,
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
        # `capture_points` / `dropped_capture_points` exist in the schema but
        # are dead — verified zero for every player across builds 15.2 and
        # 15.6. The live values are on the `cp_` pair.
        caps=int(_num(stats, "cp_capture_points")),
        caps_reset=int(_num(stats, "cp_dropped_points")),
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
        ribbons=ribbons,
    )


def _anonymize(rows: list[PlayerStats]) -> list[PlayerStats]:
    """Replace names with stable positional labels and drop clan tags.

    Also rewrites killed_by to the killer's own anonymised label — it is
    resolved in _player_row from the real (pre-anonymisation) name, so
    without this step a dead player's row leaks exactly who killed them.
    Player names are unique within a single match (WoWS enforces unique
    account names), so a name -> label map built over this same rows
    list is a safe join back onto killed_by.

    Numbered over the display-sorted list so the same replay always
    produces the same labels.
    """
    labels = {row.name: f"Player {i + 1}" for i, row in enumerate(rows)}
    return [
        dataclasses.replace(
            row,
            name=labels[row.name],
            clan_tag="",
            killed_by=labels.get(row.killed_by, ""),
        )
        for row in rows
    ]


def build_match_stats(
    *,
    results: Any,
    ships_db: dict[int, dict],
    self_team_id: int,
    meta: dict[str, Any],
    flags: frozenset[str] = frozenset(),
    neutral_perspective: bool = False,
    ribbon_icons: tuple[tuple[int, str], ...] = (),
) -> MatchStats:
    """Assemble display-ready stats. Pure — no replay, no gamedata I/O."""
    name_by_db_id = {
        db_id: str(p.stats.get("name") or "")
        for db_id, p in results.players.items()
    }
    self_db_id = results.own_db_id

    rows = [
        _player_row(p, ships_db, self_team_id, self_db_id, name_by_db_id, neutral_perspective)
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
        # One replay's rows are all the same width, so any player answers
        # this. `all` rather than `any` so a mixed payload — which would
        # mean the schema assumption is already wrong — degrades to text.
        ribbons_available=bool(results.players) and all(
            ribbons_readable(p) for p in results.players.values()
        ),
        ribbon_icons=ribbon_icons,
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

    icons: tuple[tuple[int, str], ...] = ()
    if vgd is not None:
        try:
            icons = resolve_ribbon_icons(vgd.version_dir / "data" / "gui")
        except Exception:
            log.exception("stats: ribbon icon lookup failed; board falls back to text")
            icons = ()

    return build_match_stats(
        results=results,
        ships_db=(vgd.ships_db if vgd is not None else {}),
        self_team_id=self_team_id,
        meta=meta,
        flags=flags,
        neutral_perspective=neutral_perspective,
        ribbon_icons=icons,
    )
