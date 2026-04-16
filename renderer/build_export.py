"""Generate WoWs-ShipBuilder build URLs from replay data.

Produces links to https://app.wowssb.com/ship?shipIndexes={index}&build={compressed}
that open the exact loadout (modules, upgrades, consumables, captain, skills, signals)
each player used in the replay.

Build string format (v4):
    {ShipIndex};{Modules,csv};{Upgrades,csv};{Captain};{Skills,csv};
    {Consumables,csv};{Signals,csv};{Version}

Compressed format: JSON → Deflate → Base64 (matching ShipBuilder's Build.CreateStringFromBuild)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wows_replay_parser.api import ParsedReplay

    from renderer.gamedata_cache import VersionedGamedata

log = logging.getLogger(__name__)

_SHIPBUILDER_BASE = "https://app.wowssb.com/ship"


def _build_short_string(
    ship_index: str,
    modules: list[str],
    upgrades: list[str],
    captain: str,
    skills: list[int],
    consumables: list[str],
    signals: list[str],
) -> str:
    """Build a short-format build string (v4) matching ShipBuilder's CreateShortStringFromBuild."""
    parts = [
        ship_index,
        ",".join(modules),
        ",".join(upgrades),
        captain,
        ",".join(str(s) for s in skills),
        ",".join(consumables),
        ",".join(signals),
        "4",  # build version
        "",   # build name
    ]
    return ";".join(parts)


def _reduce_to_index(full_index: str) -> str:
    """Reduce a full index like 'PCM030_MainWeapon_Mod_I' to 'PCM030'."""
    return full_index.split("_")[0] if "_" in full_index else full_index


def generate_build_url(
    player,
    vgd: VersionedGamedata,
    replay: ParsedReplay,
) -> str | None:
    """Generate a ShipBuilder URL for a player's loadout.

    Args:
        player: PlayerInfo from the replay.
        vgd: VersionedGamedata with gameparams.
        replay: ParsedReplay for skill extraction.

    Returns:
        URL string, or None if required data is missing.
    """
    sc = player.ship_config
    if not sc or not sc.ship_params_id:
        return None

    gp = vgd.gameparams

    # Build GP ID → index lookup
    id_to_index: dict[int, str] = {}
    for _, obj in gp.items():
        if isinstance(obj, dict) and "id" in obj and "index" in obj:
            id_to_index[obj["id"]] = obj["index"]

    ship_index = id_to_index.get(sc.ship_params_id)
    if not ship_index:
        return None

    # Map IDs to indices
    modules = [_reduce_to_index(id_to_index[m]) for m in sc.units if m and m in id_to_index]
    upgrades = [_reduce_to_index(id_to_index[m]) for m in sc.modernizations if m and m in id_to_index]
    consumables = [_reduce_to_index(id_to_index[c]) for c in sc.consumables if c and c in id_to_index]
    signals = [_reduce_to_index(id_to_index[e]) for e in sc.exteriors if e and e in id_to_index]

    # Captain index
    captain_index = ""
    for _, obj in gp.items():
        if isinstance(obj, dict) and obj.get("id") == player.crew_id:
            ti = obj.get("typeinfo")
            if isinstance(ti, dict) and ti.get("type") == "Crew":
                captain_index = _reduce_to_index(obj.get("index", ""))
                break

    # Skills from crewModifiersCompactParams
    from wows_replay_parser.consumable_calc import SPECIES_INDEX
    species = vgd.ships_db.get(player.ship_id, {}).get("species", "")
    species_idx = SPECIES_INDEX.get(species, -1)
    skills: list[int] = []
    crew_modifiers = getattr(replay, "crew_modifiers", {}) or {}
    if species_idx >= 0:
        crew_props = crew_modifiers.get(player.entity_id)
        if crew_props:
            ls = getattr(crew_props, "learnedSkills", None)
            if ls and species_idx < len(ls):
                skills = list(ls[species_idx])

    build_str = _build_short_string(
        ship_index, modules, upgrades, captain_index, skills, consumables, signals,
    )
    return f"{_SHIPBUILDER_BASE}?shipIndexes={ship_index}&build={build_str}"


def generate_all_build_urls(
    replay: ParsedReplay,
    vgd: VersionedGamedata,
) -> list[tuple[str, str, int, str | None]]:
    """Generate build URLs for all players in a replay.

    Returns:
        List of (player_name, ship_display_name, display_team, url_or_none)
        tuples, sorted by team then name.
    """
    results: list[tuple[str, str, int, str | None]] = []

    for player in replay.players:
        ship_info = vgd.ships_db.get(player.ship_id, {})
        ship_name = ship_info.get("short_name", ship_info.get("name", "Unknown"))
        display_team = 0 if player.relation <= 1 else 1

        url = generate_build_url(player, vgd, replay)
        results.append((player.name, ship_name, display_team, url))

    results.sort(key=lambda x: (x[2], x[0].lower()))
    return [(name, ship, team, url) for name, ship, team, url in results]
