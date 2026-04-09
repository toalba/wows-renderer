from __future__ import annotations
import json
import logging
import struct
from functools import lru_cache
from pathlib import Path
from typing import Any

from renderer.gamedata_resolver import resolve_json_cache

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


def _build_ships(source_dir: Path) -> dict[str, Any]:
    """Scan split/Ship/*.json for ship metadata."""
    result = {}
    for f in source_dir.iterdir():
        if f.suffix != ".json":
            continue
        try:
            data = json.loads(f.read_text())
            ship_id = data.get("id")
            if ship_id is None:
                continue
            typeinfo = data.get("typeinfo", {})
            result[str(ship_id)] = {
                "name": data.get("name", ""),
                "index": data.get("index", ""),
                "species": typeinfo.get("species", "") if isinstance(typeinfo, dict) else "",
                "nation": typeinfo.get("nation", "") if isinstance(typeinfo, dict) else "",
                "level": data.get("level", 0),
            }
        except (json.JSONDecodeError, ValueError):
            continue
    return result


def load_ships_db(gamedata_path: Path, vgd: object | None = None) -> dict[int, dict]:
    """Load ships.json → {ship_id(int): {name, species, nation, level}}.

    If *vgd* (VersionedGamedata) is provided, returns its pre-built ships_db
    directly, bypassing file I/O.
    """
    if vgd is not None:
        return vgd.ships_db  # type: ignore[union-attr]

    global _ships_db
    if _ships_db is not None:
        return _ships_db

    data = resolve_json_cache(
        gamedata_path / "ships.json",
        gamedata_path / "split" / "Ship",
        _build_ships,
    )
    _ships_db = {int(k): v for k, v in data.items()}

    # Merge display names from ship_names.json if available
    names_path = gamedata_path / "ship_names.json"
    if names_path.exists():
        try:
            names = json.loads(names_path.read_text(encoding="utf-8"))
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


def _svg_to_surface(svg_text: str, target_height: int = 18) -> cairo.ImageSurface:
    """Rasterize an SVG string to a cairo surface at a fixed output height."""
    import cairosvg
    import io
    import re

    # Parse original viewport for aspect ratio
    w_match = re.search(r'width="(\d+)"', svg_text)
    h_match = re.search(r'height="(\d+)"', svg_text)
    svg_w = int(w_match.group(1)) if w_match else 9
    svg_h = int(h_match.group(1)) if h_match else 16

    out_h = target_height
    out_w = round(svg_w / svg_h * out_h)

    png_data = cairosvg.svg2png(
        bytestring=svg_text.encode("utf-8"),
        output_width=out_w,
        output_height=out_h,
    )
    return cairo.ImageSurface.create_from_png(io.BytesIO(png_data))


def _rgba_to_hex(r: float, g: float, b: float) -> str:
    """Convert 0-1 float RGB to hex color string."""
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def load_ship_icons(
    gamedata_path: Path,
    team_colors: dict[int, tuple[float, float, float, float]] | None = None,
    self_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
) -> dict[str, dict[str, cairo.ImageSurface]]:
    """Load ship class icons from SVG minimap assets, tinted per team.

    Uses vector SVGs from gui/fla/minimap/ship_icons/ for sharp rendering
    at any resolution. The white fill in each SVG is replaced with the
    appropriate team color before rasterization.

    Returns:
        {species_lower: {"ally": ..., "enemy": ..., "white": ..., "sunk": ...}}
    """
    if team_colors is None:
        team_colors = {
            0: (0.36, 0.90, 0.51, 1.0),
            1: (1.00, 0.42, 0.42, 1.0),
        }
    ally_hex = _rgba_to_hex(*team_colors[0][:3])
    enemy_hex = _rgba_to_hex(*team_colors[1][:3])
    self_hex = _rgba_to_hex(*self_color[:3])

    svg_dir = gamedata_path / "gui" / "fla" / "minimap" / "ship_icons"
    icons: dict[str, dict[str, cairo.ImageSurface]] = {}

    # Mapping: variant key → (svg filename pattern, fill color hex)
    variant_map = {
        "ally":     ("minimap_{base}.svg", ally_hex),
        "enemy":    ("minimap_{base}.svg", enemy_hex),
        "white":    ("minimap_{base}.svg", self_hex),
        "sunk":     ("minimap_{base}_dead.svg", None),  # keep original colors
    }

    for species, base in _SPECIES_ICON_MAP.items():
        key = base
        icons[key] = {}
        for variant, (pattern, color_hex) in variant_map.items():
            svg_path = svg_dir / pattern.format(base=base)
            if not svg_path.exists():
                continue
            try:
                svg_text = svg_path.read_text()
                if color_hex:
                    svg_text = svg_text.replace('fill="white"', f'fill="{color_hex}"')
                icons[key][variant] = _svg_to_surface(svg_text)
            except Exception as e:
                log.debug("Failed to load SVG icon %s: %s", svg_path, e)

    # Fallback: try PNG icons if SVG dir doesn't exist
    if not icons or not any(icons.values()):
        log.info("SVG icons not found, falling back to PNG icons")
        return _load_ship_icons_png(gamedata_path)

    return icons


def _load_ship_icons_png(
    gamedata_path: Path,
) -> dict[str, dict[str, cairo.ImageSurface]]:
    """Fallback: load ship class icons from PNG files."""
    icon_dir = gamedata_path / "gui" / "battle_hud" / "markers" / "ship"
    icons: dict[str, dict[str, cairo.ImageSurface]] = {}
    for species, base in _SPECIES_ICON_MAP.items():
        key = base
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


def _build_projectiles(source_dir: Path) -> dict[str, Any]:
    """Scan split/Projectile/*.json for ammoType + caliber."""
    result = {}
    for f in source_dir.iterdir():
        if f.suffix != ".json":
            continue
        try:
            data = json.loads(f.read_text())
            pid = data.get("id")
            if pid is None:
                continue
            ammo_type = data.get("ammoType", "")
            caliber = data.get("bulletDiametr", 0)
            entry: dict[str, Any] = {"a": ammo_type}
            if caliber:
                entry["c"] = round(caliber * 1000)  # meters to mm
            result[str(pid)] = entry
        except (json.JSONDecodeError, ValueError):
            continue
    return result


def load_projectiles_db(gamedata_path: Path, vgd: object | None = None) -> dict[int, dict]:
    """Load projectiles.json → {params_id(int): {a: ammo_type, c: caliber_mm, s: is_secondary}}.

    If *vgd* (VersionedGamedata) is provided, returns its pre-built projectiles_db.
    """
    if vgd is not None:
        return vgd.projectiles_db  # type: ignore[union-attr]

    global _projectiles_db
    if _projectiles_db is not None:
        return _projectiles_db

    data = resolve_json_cache(
        gamedata_path / "projectiles.json",
        gamedata_path / "split" / "Projectile",
        _build_projectiles,
    )
    _projectiles_db = {int(k): v for k, v in data.items()}
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
# Loaded from consumable_type_ids.json (generated by gamedata pipeline).
CONSUMABLE_TYPE_ID_MAP: dict[int, str] = {}


def _load_consumable_type_ids(gamedata_path: Path) -> dict[int, str]:
    """Load consumable type ID map from consumable_type_ids.json."""
    global CONSUMABLE_TYPE_ID_MAP
    if CONSUMABLE_TYPE_ID_MAP:
        return CONSUMABLE_TYPE_ID_MAP
    json_path = gamedata_path / "consumable_type_ids.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            CONSUMABLE_TYPE_ID_MAP.clear()
            CONSUMABLE_TYPE_ID_MAP.update(
                {int(k): v for k, v in data.items() if k.isdigit()}
            )
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("Failed to load consumable_type_ids.json: %s", e)
    if not CONSUMABLE_TYPE_ID_MAP:
        log.warning("consumable_type_ids.json not found, using empty map")
    return CONSUMABLE_TYPE_ID_MAP

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


def _build_ship_consumables(source_dir: Path) -> dict[str, Any]:
    """Scan split/Ship/*.json (and sibling split/Ability/*.json) for consumable loadouts."""
    # source_dir is split/Ship/; navigate to parent to find split/Ability/
    ability_dir = source_dir.parent / "Ability"
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

    result: dict[str, Any] = {}

    for ship_file in source_dir.iterdir():
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

        result[str(ship_id)] = {
            "slots": slot_categories,
            "abilities": ability_names,
            "has_repair_party": "repair_party" in slot_categories,
            "ranges": ranges,
        }

    log.info("Built consumable data for %d ships from split files", len(result))
    return result


def load_ship_consumables(gamedata_path: Path, vgd: object | None = None) -> dict[int, dict[str, list[str]]]:
    """Load consumable loadouts per ship.

    If *vgd* (VersionedGamedata) is provided, returns its pre-built ship_consumables.
    Otherwise prefers ship_consumables.json, falls back to scanning split files.
    """
    if vgd is not None:
        return vgd.ship_consumables  # type: ignore[union-attr]

    global _ship_consumables_cache
    if _ship_consumables_cache is not None:
        return _ship_consumables_cache

    json_path = gamedata_path / "ship_consumables.json"
    ship_dir = gamedata_path / "split" / "Ship"

    data = resolve_json_cache(
        json_path,
        ship_dir,
        _build_ship_consumables,
    )
    _ship_consumables_cache = {int(k): v for k, v in data.items()}
    return _ship_consumables_cache


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


# ── Localization (.mo) ──────────────────────────────────────────────

_mo_cache: dict[str, str] | None = None


def load_mo_strings(gamedata_path: Path) -> dict[str, str]:
    """Load global.mo → {key: translated_string}. Cached after first call."""
    global _mo_cache
    if _mo_cache is not None:
        return _mo_cache

    mo_path = gamedata_path / "global.mo"
    _mo_cache = {}
    if not mo_path.exists():
        return _mo_cache

    try:
        data = mo_path.read_bytes()
        nstrings = struct.unpack("<I", data[8:12])[0]
        orig_off = struct.unpack("<I", data[12:16])[0]
        trans_off = struct.unpack("<I", data[16:20])[0]
        for i in range(nstrings):
            olen, ooff = struct.unpack("<II", data[orig_off + i * 8 : orig_off + i * 8 + 8])
            key = data[ooff : ooff + olen].decode("utf-8", errors="replace")
            tlen, toff = struct.unpack("<II", data[trans_off + i * 8 : trans_off + i * 8 + 8])
            val = data[toff : toff + tlen].decode("utf-8", errors="replace")
            _mo_cache[key] = val
    except Exception as e:
        log.warning("Failed to load global.mo: %s", e)

    return _mo_cache


def get_ship_display_name(gamedata_path: Path, ship_index: str) -> str:
    """Look up localized ship name from global.mo by index (e.g. 'PHSC710')."""
    strings = load_mo_strings(gamedata_path)
    key = f"IDS_{ship_index}"
    name = strings.get(key)
    if name:
        return name
    # Fallback: strip index prefix from ships.json name
    return ship_index
