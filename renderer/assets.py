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
