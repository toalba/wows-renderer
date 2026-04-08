"""Dynamic gamedata resolution with stale-cache detection.

Generalises the ship_consumables.json mtime pattern from assets.py.
Each GameParams-derived JSON file can be dynamically rebuilt from split files
when the JSON is stale or missing, using the JSON as an optional cache.

Dynamically resolved files (rebuild from split/ when stale):
  - buff_drops.json       ← split/Drop/*.json
  - aircraft_icons.json   ← split/Aircraft/*.json + split/Projectile/*.json
  - projectiles.json      ← split/Projectile/*.json
  - ships.json            ← split/Ship/*.json
  - ship_names.json       ← global.mo localisation
  - ship_consumables.json ← split/Ship/*.json + split/Ability/*.json

Static files (NOT GameParams-derived, always loaded as-is):
  - map_sizes.json          ← space.settings XML per map
  - consumable_type_ids.json ← ConsumableConstants.pyc bytecode
  - arena_key_maps.json     ← entity bytecode
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


def _is_git_tracked(path: Path) -> bool:
    """Check if a file lives inside a git repo and is tracked.

    If True, we must NOT write to it — that would dirty the repo
    and block gamedata_sync from switching versions via git checkout.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path.name)],
            cwd=path.parent,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def resolve_json_cache(
    json_path: Path,
    source_dir: Path,
    builder: Callable[[Path], dict[str, Any]],
    *,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Load from JSON cache if fresh, rebuild from source directory if stale.

    Args:
        json_path: Path to the JSON cache file.
        source_dir: Directory containing source files (e.g. split/Ship/).
        builder: Function that scans source_dir and returns the data dict.
        encoding: Encoding for JSON file I/O.

    Returns:
        The loaded/rebuilt data dict.

    Logic:
        1. If json_path exists and (source_dir doesn't exist or json is newer) → load JSON.
        2. If source_dir exists and is newer → call builder, write cache, return.
        3. If neither exists → return empty dict.
    """
    # Fast path: JSON exists and is fresh (or source_dir is absent)
    if json_path.exists():
        if not source_dir.exists() or json_path.stat().st_mtime >= source_dir.stat().st_mtime:
            try:
                data = json.loads(json_path.read_text(encoding=encoding))
                return data
            except (json.JSONDecodeError, ValueError) as e:
                log.warning("Failed to load %s: %s", json_path.name, e)

    # Slow path: rebuild from source directory
    if not source_dir.exists():
        log.warning("Neither %s nor %s found", json_path.name, source_dir)
        return {}

    log.info("Rebuilding %s from %s", json_path.name, source_dir)
    data = builder(source_dir)

    # Write cache for next time — but only if the file isn't git-tracked.
    # Writing to a tracked file would dirty the gamedata repo and block
    # gamedata_sync from switching versions via git checkout.
    if not _is_git_tracked(json_path):
        try:
            json_path.write_text(
                json.dumps(data, separators=(",", ":")), encoding=encoding,
            )
        except OSError as e:
            log.warning("Failed to write cache %s: %s", json_path.name, e)
    else:
        log.debug("Skipping cache write for git-tracked %s", json_path.name)

    return data
