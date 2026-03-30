from __future__ import annotations
import json
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

import cairo


DEFAULT_MAP_SIZE = 1400.0  # Reasonable fallback

# Cache: map_name (without "spaces/" prefix) -> space_size
_map_size_cache: dict[str, float] = {}
_json_loaded = False


def _load_map_sizes_json(gamedata_path: Path) -> None:
    """Load map_sizes.json from gamedata into the cache."""
    global _json_loaded
    if _json_loaded:
        return

    json_path = gamedata_path / "map_sizes.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text())
            for map_key, info in data.items():
                _map_size_cache[map_key] = float(info["space_size"])
            _json_loaded = True
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.warning("Failed to load map_sizes.json: %s", e)


def get_map_size(map_name: str, gamedata_path: Path | None = None) -> float:
    """Get the space_size for a map from map_sizes.json.

    Args:
        map_name: e.g. "spaces/56_AngelWings"
        gamedata_path: Root of wows-gamedata/data
    """
    if gamedata_path is not None:
        _load_map_sizes_json(gamedata_path)

    # map_name comes as "spaces/56_AngelWings", JSON keys are "56_AngelWings"
    key = map_name.removeprefix("spaces/")

    if key in _map_size_cache:
        return _map_size_cache[key]

    log.warning(
        "Map '%s' not found in map_sizes.json, using fallback %s. "
        "Positions may be inaccurate. Re-run extract_map_sizes.py to update.",
        map_name, DEFAULT_MAP_SIZE,
    )
    return DEFAULT_MAP_SIZE


def load_minimap(gamedata_path: Path, map_name: str) -> cairo.ImageSurface:
    """Load the minimap PNG as a cairo ImageSurface.

    Args:
        gamedata_path: Root of wows-gamedata/data
        map_name: e.g. "spaces/01_solomon_islands"
    """
    minimap_path = gamedata_path / map_name / "minimap.png"
    if not minimap_path.exists():
        # Try minimap_water as fallback
        minimap_path = gamedata_path / map_name / "minimap_water.png"
    if not minimap_path.exists():
        raise FileNotFoundError(f"No minimap found for {map_name} at {gamedata_path / map_name}")

    return cairo.ImageSurface.create_from_png(str(minimap_path))


def load_minimap_water(gamedata_path: Path, map_name: str) -> cairo.ImageSurface | None:
    """Load the minimap water layer PNG, or None if not available."""
    water_path = gamedata_path / map_name / "minimap_water.png"
    if not water_path.exists():
        return None
    return cairo.ImageSurface.create_from_png(str(water_path))


_ships_db: dict[int, dict] | None = None


def load_ships_db(gamedata_path: Path) -> dict[int, dict]:
    """Load ships.json → {ship_id(int): {name, species, nation, level}}."""
    global _ships_db
    if _ships_db is not None:
        return _ships_db

    json_path = gamedata_path / "ships.json"
    if not json_path.exists():
        log.warning("ships.json not found at %s", json_path)
        _ships_db = {}
        return _ships_db

    try:
        data = json.loads(json_path.read_text())
        _ships_db = {int(k): v for k, v in data.items()}
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to load ships.json: %s", e)
        _ships_db = {}

    # Merge display names from ship_names.json if available
    names_path = gamedata_path / "ship_names.json"
    if names_path.exists():
        try:
            names = json.loads(names_path.read_text())
            for k, display_name in names.items():
                sid = int(k)
                if sid in _ships_db:
                    _ships_db[sid]["short_name"] = display_name
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("Failed to load ship_names.json: %s", e)

    return _ships_db


# Species name → icon file base name
_SPECIES_ICON_MAP: dict[str, str] = {
    "Destroyer": "destroyer",
    "Cruiser": "cruiser",
    "Battleship": "battleship",
    "AirCarrier": "aircarrier",
    "Submarine": "submarine",
    "Auxiliary": "auxiliary",
}


def load_ship_icons(
    gamedata_path: Path,
) -> dict[str, dict[str, cairo.ImageSurface]]:
    """Load ship class icons for ally/enemy/white variants.

    Returns:
        {species_lower: {"ally": surface, "enemy": surface, "white": surface}}
    """
    icon_dir = gamedata_path / "gui" / "battle_hud" / "markers" / "ship"
    icons: dict[str, dict[str, cairo.ImageSurface]] = {}

    for species, base in _SPECIES_ICON_MAP.items():
        key = base  # e.g. "destroyer"
        icons[key] = {}
        variants = {
            "ally": f"icon_ally_{base}.png",
            "enemy": f"icon_enemy_{base}.png",
            "white": f"icon_white_{base}.png",
            "sunk": f"icon_sunk_{base}.png",
        }
        for variant, filename in variants.items():
            path = icon_dir / filename
            if path.exists():
                try:
                    icons[key][variant] = cairo.ImageSurface.create_from_png(str(path))
                except Exception as e:
                    log.debug("Failed to load icon %s: %s", path, e)
    return icons


_projectiles_db: dict[int, dict] | None = None


def load_projectiles_db(gamedata_path: Path) -> dict[int, dict]:
    """Load projectiles.json → {params_id(int): {a: ammo_type, c: caliber_mm, s: is_secondary}}.

    Ammo types: 'AP', 'HE', 'SAP'.
    """
    global _projectiles_db
    if _projectiles_db is not None:
        return _projectiles_db

    json_path = gamedata_path / "projectiles.json"
    if not json_path.exists():
        log.warning("projectiles.json not found at %s", json_path)
        _projectiles_db = {}
        return _projectiles_db

    try:
        data = json.loads(json_path.read_text())
        _projectiles_db = {int(k): v for k, v in data.items()}
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to load projectiles.json: %s", e)
        _projectiles_db = {}
    return _projectiles_db


# ── Ship consumable data from GameParams split ──────────────

# Ability name patterns → consumable category
_CONSUMABLE_PATTERNS: dict[str, str] = {
    "CrashCrew": "damage_control",
    "RegenCrew": "repair_party",
    "RegenerateHealth": "repair_party",
    "AirDefenseDisp": "defensive_aa",
    "Fighter": "catapult_fighter",
    "RegenPlane": "catapult_fighter",
    "SpeedBoost": "engine_boost",
    "Speedbooster": "engine_boost",
    "SonarSearch": "hydroacoustic",
    "RLSSearch": "surveillance_radar",
    "SmokeGenerator": "smoke_screen",
    "TorpedoReloader": "torpedo_reload",
    "MainWeaponReloader": "main_battery_reload",
    "AcousticWave": "submarine_surveillance",
    "SubmarineLocator": "submarine_surveillance",
}

# Wire consumableType name → timing category name in ship_consumables.json
CONSUMABLE_TYPE_TO_CATEGORY: dict[str, str] = {
    "crashCrew": "damage_control",
    "regenCrew": "repair_party",
    "regenerateHealth": "repair_party",
    "airDefenseDisp": "defensive_aa",
    "fighter": "catapult_fighter",
    "scout": "catapult_fighter",
    "speedBoosters": "engine_boost",
    "sonar": "hydroacoustic",
    "rls": "surveillance_radar",
    "smokeGenerator": "smoke_screen",
    "torpedoReloader": "torpedo_reload",
    "artilleryBoosters": "main_battery_reload",
    "hydrophone": "submarine_surveillance",
    "submarineLocator": "submarine_surveillance",
}

# Global consumable type ID → consumableType string.
# From decompiled server code: ConsumableIDsMap maps consumableType strings
# to integer IDs. The server replaces string keys with these IDs before
# sending over the wire (in setConsumables pickle and onConsumableUsed).
CONSUMABLE_TYPE_ID_MAP: dict[int, str] = {
    0: "crashCrew",
    1: "scout",
    2: "airDefenseDisp",
    3: "speedBoosters",
    4: "artilleryBoosters",
    5: "hangarBooster",
    6: "smokeGenerator",
    7: "unused",
    8: "regenCrew",
    9: "fighter",
    10: "sonar",
    11: "torpedoReloader",
    12: "rls",
    13: "trigger1",
    14: "trigger2",
    15: "trigger3",
    16: "trigger4",
    17: "trigger5",
    18: "trigger6",
    19: "invulnerable",
    20: "healForsage",
    21: "activeManeuvering",
    22: "callFighters",
    23: "regenerateHealth",
    24: "subsOxygenRegen",
    25: "subsWaveGunBoost",
    26: "subsFourthState",
    27: "depthCharges",
    28: "trigger7",
    29: "trigger8",
    30: "trigger9",
    31: "buff",
    32: "buffsShift",
    33: "circleWave",
    34: "goDeep",
    35: "weaponReloadBooster",
    36: "hydrophone",
    37: "fastRudders",
    38: "subsEnergyFreeze",
    39: "groupAuraBuff",
    40: "affectedBuffAura",
    41: "invisibilityExtraBuffConsumable",
    42: "submarineLocator",
}

# consumableType string → icon name (Premium first, then base).
# Maps the type string to the PCY icon file name.
CONSUMABLE_TYPE_TO_ICONS: dict[str, list[str]] = {
    "crashCrew": ["PCY009_CrashCrewPremium", "PCY001_CrashCrew"],
    "scout": ["PCY013_SpotterPremium", "PCY005_Spotter"],
    "airDefenseDisp": ["PCY011_AirDefenseDispPremium", "PCY003_AirDefenseDisp"],
    "speedBoosters": ["PCY015_SpeedBoosterPremium", "PCY007_SpeedBooster"],
    "artilleryBoosters": ["PCY022_ArtilleryBoosterPremium", "PCY021_ArtilleryBooster"],
    "smokeGenerator": ["PCY014_SmokeGeneratorPremium", "PCY006_SmokeGenerator"],
    "regenCrew": ["PCY010_RegenCrewPremium", "PCY002_RegenCrew"],
    "fighter": ["PCY012_FighterPremium", "PCY004_Fighter"],
    "sonar": ["PCY016_SonarSearchPremium", "PCY008_SonarSearch"],
    "torpedoReloader": ["PCY018_TorpedoReloaderPremium", "PCY017_TorpedoReloader"],
    "rls": ["PCY020_RLSSearchPremium", "PCY019_RLSSearch"],
    "invulnerable": ["PCY024_InvulnerablePremium", "PCY023_Invulnerable"],
    "regenerateHealth": ["PCY036_RegenerateHealth"],
    "callFighters": ["PCY035_CallFighters"],
    "hydrophone": ["PCY045_Hydrophone"],
    "submarineLocator": ["PCY048_SubmarineLocator"],
}

_ship_consumables_cache: dict[int, dict[str, list[str]]] | None = None


def load_ship_consumables(gamedata_path: Path) -> dict[int, dict[str, list[str]]]:
    """Load consumable loadouts per ship.

    Prefers ship_consumables.json (fast, ~200KB) if available.
    Falls back to scanning GameParams split/Ship/ directory (slow, reads 1171 files).

    Returns:
        {ship_id: {"slots": ["damage_control", "hydroacoustic", ...],
                    "abilities": ["PCY009_CrashCrewPremium", ...],
                    "has_repair_party": True/False}}
    """
    global _ship_consumables_cache
    if _ship_consumables_cache is not None:
        return _ship_consumables_cache

    # Fast path: pre-built JSON
    json_path = gamedata_path / "ship_consumables.json"
    ship_dir = gamedata_path / "split" / "Ship"
    # Use JSON if it exists and is either the only source or newer than split dir
    if json_path.exists() and (not ship_dir.exists() or json_path.stat().st_mtime >= ship_dir.stat().st_mtime):
        try:
            data = json.loads(json_path.read_text())
            _ship_consumables_cache = {int(k): v for k, v in data.items()}
            return _ship_consumables_cache
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("Failed to load ship_consumables.json: %s", e)

    # Slow path: scan split files
    ship_dir = gamedata_path / "split" / "Ship"
    if not ship_dir.exists():
        log.warning("GameParams split/Ship not found at %s", ship_dir)
        _ship_consumables_cache = {}
        return _ship_consumables_cache

    # Pre-load all Ability files for variant range lookup
    ability_dir = gamedata_path / "split" / "Ability"
    ability_data: dict[str, dict] = {}  # ability_name → full JSON
    if ability_dir.exists():
        for af in ability_dir.iterdir():
            if af.suffix == ".json":
                try:
                    ad = json.loads(af.read_text())
                    name = ad.get("name")
                    if name:
                        ability_data[name] = ad
                except (json.JSONDecodeError, ValueError):
                    pass

    result: dict[int, dict] = {}

    for ship_file in ship_dir.iterdir():
        if ship_file.suffix != ".json":
            continue
        try:
            data = json.loads(ship_file.read_text())
        except (json.JSONDecodeError, ValueError):
            continue

        ship_id = data.get("id")
        if ship_id is None:
            continue

        abilities_data = data.get("ShipAbilities", {})
        ability_names: list[str] = []
        slot_categories: list[str] = []
        # consumableType → range in meters (for radar/hydro circles)
        ranges: dict[str, float] = {}

        for slot_key in sorted(abilities_data.keys()):
            slot_val = abilities_data[slot_key]
            if not isinstance(slot_val, dict):
                abils = slot_val if isinstance(slot_val, list) else []
            else:
                abils = slot_val.get("abils", [])

            if not abils:
                continue

            for option in abils:
                if isinstance(option, list) and len(option) >= 1:
                    ability_name = option[0]
                    variant_name = option[1] if len(option) >= 2 else ""
                    ability_names.append(ability_name)
                    category = _classify_ability(ability_name)
                    if category and category not in slot_categories:
                        slot_categories.append(category)

                    # Look up detection range for radar/hydro/hydrophone
                    if category in ("hydroacoustic", "surveillance_radar", "submarine_surveillance"):
                        ab = ability_data.get(ability_name, {})
                        variant = ab.get(variant_name, {})
                        logic = variant.get("logic", {})
                        if isinstance(logic, dict):
                            dist_ship = logic.get("distShip", 0)
                            if dist_ship:
                                # distShip is in 1/30th of meters
                                ct = _classify_ability_to_consumable_type(ability_name)
                                if ct:
                                    ranges[ct] = dist_ship * 30.0

                    break  # Only first option per slot

        result[int(ship_id)] = {
            "slots": slot_categories,
            "abilities": ability_names,
            "has_repair_party": "repair_party" in slot_categories,
            "ranges": ranges,
        }

    _ship_consumables_cache = result
    log.info("Loaded consumable data for %d ships from split files", len(result))

    # Save as JSON for fast loading next time
    try:
        json_path = gamedata_path / "ship_consumables.json"
        json_path.write_text(json.dumps(result, separators=(",", ":")))
        log.info("Saved ship_consumables.json (%d ships)", len(result))
    except OSError as e:
        log.debug("Could not save ship_consumables.json: %s", e)

    return result


_consumable_icons_cache: dict[str, cairo.ImageSurface] | None = None


def load_consumable_icons(
    gamedata_path: Path,
) -> dict[str, cairo.ImageSurface]:
    """Load consumable icon PNGs.

    Returns:
        {ability_name: cairo.ImageSurface}
        e.g. {"PCY009_CrashCrewPremium": <surface>, ...}
    """
    global _consumable_icons_cache
    if _consumable_icons_cache is not None:
        return _consumable_icons_cache

    icon_dir = gamedata_path / "gui" / "consumables"
    icons: dict[str, cairo.ImageSurface] = {}

    if not icon_dir.exists():
        log.warning("Consumable icons dir not found: %s", icon_dir)
        _consumable_icons_cache = icons
        return icons

    for png in icon_dir.glob("consumable_*.png"):
        name = png.stem  # e.g. "consumable_PCY009_CrashCrewPremium"
        if name.endswith("_empty"):
            continue  # Skip empty/depleted variants
        # Strip "consumable_" prefix to get ability name
        ability_name = name.removeprefix("consumable_")
        try:
            icons[ability_name] = cairo.ImageSurface.create_from_png(str(png))
        except Exception as e:
            log.debug("Failed to load consumable icon %s: %s", png, e)

    _consumable_icons_cache = icons
    return icons


def _classify_ability_to_consumable_type(ability_name: str) -> str:
    """Map ability name to consumableType string (matching CONSUMABLE_TYPE_ID_MAP values)."""
    name_lower = ability_name.lower()
    if "sonarsearch" in name_lower:
        return "sonar"
    if "rlssearch" in name_lower:
        return "rls"
    if "hydrophone" in name_lower or "submarinelocator" in name_lower:
        return "hydrophone"
    return ""


def _classify_ability(ability_name: str) -> str:
    """Classify an ability name into a consumable category."""
    for pattern, category in _CONSUMABLE_PATTERNS.items():
        if pattern.lower() in ability_name.lower():
            return category
    return "unknown"


@lru_cache(maxsize=4)
def load_font_face(font_path: str) -> str:
    """Get a font family name for cairo."""
    return "sans-serif"


def get_font_path(gamedata_path: Path, font_name: str = "Warhelios_Bold.ttf") -> Path:
    """Get path to a game font file."""
    candidates = [
        gamedata_path / "gui" / "fonts" / font_name,
        gamedata_path / "gui" / "fonts" / "WoWS" / font_name,
    ]
    for p in candidates:
        if p.exists():
            return p
    return Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
