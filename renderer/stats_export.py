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
