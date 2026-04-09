"""Per-version gamedata cache system.

Creates an isolated, immutable directory per game version under a cache root
(default: ``~/.cache/wows-gamedata/v{build_id}/``).  Each directory contains
everything the renderer and parser need: entity definitions, decoded GameParams,
GUI assets, and static data files.

Once populated (marked by a ``.ready`` sentinel), a version directory is never
mutated.  Multiple workers can read from different version directories
concurrently with no locks or shared mutable state.

Cache population uses ``git archive`` to extract files for a specific tag
without touching the working tree — no ``git checkout``, no race conditions.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from renderer.gameparams import (
    decode_and_cache_gameparams,
    load_gameparams_cached,
    write_split_subset,
)

log = logging.getLogger(__name__)

_DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "wows-gamedata"

# Entity types needed by consumable_calc.py in the parser
_SPLIT_TYPES_FOR_CACHE = {"Modernization", "Crew"}


# ── Data extraction (filter GameParams → renderer lookups) ─────────


def _extract_ships(gp: dict) -> dict[str, dict]:
    """Extract ships lookup: {shipId: {name, index, species, nation, level}}."""
    ships = {}
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if not isinstance(ti, dict) or ti.get("type") != "Ship":
            continue
        ship_id = obj.get("id")
        if ship_id is None:
            continue
        ships[str(ship_id)] = {
            "name": name,
            "index": obj.get("index", ""),
            "species": ti.get("species", ""),
            "nation": ti.get("nation", ""),
            "level": obj.get("level", 0),
        }
    return ships


def _extract_projectiles(gp: dict) -> dict[str, dict]:
    """Extract projectiles lookup: {id: {a: ammoType, c: caliber_mm}}."""
    projectiles = {}
    for _, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if not isinstance(ti, dict) or ti.get("type") != "Projectile":
            continue
        proj_id = obj.get("id")
        if proj_id is None:
            continue
        ammo_type = obj.get("ammoType", "")
        caliber = obj.get("bulletDiametr", 0)  # in meters
        caliber_mm = round(caliber * 1000) if caliber else 0
        projectiles[str(proj_id)] = {
            "a": ammo_type,
            "c": caliber_mm,
        }
    return projectiles


# ── Consumable classification ──────────────────────────────────────

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
    "Spotter": "spotter_plane",
    "Scout": "spotter_plane",
    "ArtilleryBooster": "main_battery_reload",
    "Hydrophone": "hydrophone",
    "EnergyFreeze": "energy_freeze",
    "GoDeep": "go_deep",
    "ArmorBuff": "armor_buff",
}


def _classify_ability(ability_name: str) -> str:
    for pattern, category in _CONSUMABLE_PATTERNS.items():
        if pattern.lower() in ability_name.lower():
            return category
    return "unknown"


def _classify_consumable_type(ability_name: str) -> str:
    name_lower = ability_name.lower()
    if "sonarsearch" in name_lower:
        return "sonar"
    if "rlssearch" in name_lower:
        return "rls"
    if "hydrophone" in name_lower or "submarinelocator" in name_lower:
        return "hydrophone"
    return ""


def _extract_ship_consumables(gp: dict) -> dict[str, dict]:
    """Extract consumable loadouts per ship from GameParams.

    Returns:
        {ship_id: {"slots": [...], "abilities": [...],
                   "has_repair_party": bool, "ranges": {...}, "timings": {...}}}
    """
    abilities: dict[str, dict] = {}
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if isinstance(ti, dict) and ti.get("type") == "Ability":
            abilities[name] = obj

    result: dict[str, dict] = {}
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if not isinstance(ti, dict) or ti.get("type") != "Ship":
            continue
        ship_id = obj.get("id")
        if ship_id is None:
            continue

        ship_abilities = obj.get("ShipAbilities", {})
        ability_names: list[str] = []
        slots: list[list[str]] = []
        all_categories: set[str] = set()
        ranges: dict[str, float] = {}
        timings: dict[str, float] = {}

        for slot_key in sorted(ship_abilities.keys()):
            slot_val = ship_abilities[slot_key]
            if not isinstance(slot_val, dict):
                abils = slot_val if isinstance(slot_val, list) else []
            else:
                abils = slot_val.get("abils", [])

            if not abils:
                continue

            slot_options: list[str] = []
            for option in abils:
                if not isinstance(option, (list, tuple)) or len(option) < 1:
                    continue
                ability_name = option[0]
                variant_name = option[1] if len(option) >= 2 else ""
                ability_names.append(ability_name)
                category = _classify_ability(ability_name)
                if category and category not in slot_options:
                    slot_options.append(category)
                all_categories.add(category)

                ab = abilities.get(ability_name, {})
                variant = ab.get(variant_name, {})
                if isinstance(variant, dict):
                    reload_t = variant.get("reloadTime", 0)
                    if category and reload_t:
                        timings[category] = float(reload_t)
                    if category in ("hydroacoustic", "surveillance_radar", "submarine_surveillance"):
                        logic = variant.get("logic", {})
                        if isinstance(logic, dict):
                            dist_ship = logic.get("distShip", 0)
                            if dist_ship:
                                ct = _classify_consumable_type(ability_name)
                                if ct:
                                    ranges[ct] = dist_ship * 30.0

            if slot_options:
                slots.append(slot_options)

        result[str(ship_id)] = {
            "slots": slots,
            "abilities": ability_names,
            "has_repair_party": "repair_party" in all_categories,
            "ranges": ranges,
            "timings": timings,
        }

    return result


def _generate_ship_names(ships: dict[str, dict], mo_path: Path) -> dict[str, str]:
    """Generate {shipId: display_name} from ships dict + global.mo."""
    import gettext
    import re

    catalog = None
    if mo_path.exists():
        class _UTF8(gettext.GNUTranslations):
            def _parse(self, fp):
                self._charset = "utf-8"
                super()._parse(fp)

        try:
            with open(mo_path, "rb") as f:
                catalog = _UTF8(f)
        except Exception as e:
            log.warning("Failed to parse %s: %s", mo_path, e)

    names: dict[str, str] = {}
    for ship_id, info in ships.items():
        index = info.get("index", "")
        display = None

        if catalog and index:
            key = f"IDS_{index}"
            val = catalog.gettext(key)
            if val != key:
                display = val

        if not display:
            raw = info.get("name", "")
            display = re.sub(r"^P[A-Z]{3}\d+_", "", raw).replace("_", " ")

        names[ship_id] = display

    return names


def _extract_aircraft_icon_map(gp: dict) -> dict[str, str]:
    """Extract aircraft params_id → icon_base mapping from GameParams.

    Cross-references Projectile entities for bomb ammoType to distinguish
    skip bombers from regular bombers.
    """
    projectile_ammo: dict[str, str] = {}
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if not isinstance(ti, dict) or ti.get("type") != "Projectile":
            continue
        ammo = obj.get("ammoType", "")
        if name and ammo:
            projectile_ammo[name] = ammo

    result: dict[str, str] = {}
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if not isinstance(ti, dict) or ti.get("type") != "Aircraft":
            continue
        pid = obj.get("id")
        if pid is None:
            continue

        species = ti.get("species", "")
        bomb_name = obj.get("bombName", "")
        plane_subtype = obj.get("planeSubtype", "")

        icon_base = "bomber_depth_charge"  # default

        if species == "Fighter":
            icon_base = "fighter"
        elif species == "Scout":
            icon_base = "scout"
        elif species in ("Dive", "DiveBomber"):
            ammo = projectile_ammo.get(bomb_name, "")
            if ammo == "CS_SKIP_BOMB":
                icon_base = "bomber_skip"
            else:
                icon_base = "bomber"
        elif species in ("Torpedo", "TorpedoBomber"):
            icon_base = "torpedo"
        elif plane_subtype == "DepthCharge" or species == "DepthCharge":
            icon_base = "bomber_depth_charge"

        result[str(pid)] = icon_base

    return result


# ── VersionedGamedata ──────────────────────────────────────────────


@dataclass
class VersionedGamedata:
    """Resolved, ready-to-use gamedata for a specific game version.

    The GameParams dict is loaded lazily on first access (e.g. when a layer
    queries ``ships_db``).  This means constructing a ``VersionedGamedata``
    from a warm cache is near-instant — the 15 MB pickle load is deferred
    until rendering actually needs it.

    File-based assets (icons, minimaps, .mo) are served from ``version_dir``.
    """

    version_dir: Path
    """Cache directory (e.g. ``~/.cache/wows-gamedata/v12116141/``)."""

    build_id: str
    """Build ID string (e.g. ``"12116141"``)."""

    _gameparams: dict | None = field(default=None, repr=False)
    """Pre-loaded GameParams dict, or None for lazy load from pickle."""

    @cached_property
    def gameparams(self) -> dict:
        """Fully decoded GameParams dict (~15 MB in memory). Loaded lazily."""
        if self._gameparams is not None:
            return self._gameparams
        return load_gameparams_cached(self.version_dir)

    @property
    def entity_defs_path(self) -> Path:
        """Path to entity_defs directory for the parser.

        Located under ``data/`` to match the path structure the parser's
        ``consumable_calc.py`` expects when navigating up from entity_defs
        to find ``data/split/`` and ``data/ship_consumables.json``.
        """
        return self.version_dir / "data" / "scripts_entity" / "entity_defs"

    @cached_property
    def ships_db(self) -> dict[int, dict]:
        """Ship ID → {name, index, species, nation, level, short_name}."""
        ships = _extract_ships(self.gameparams)
        result = {int(k): v for k, v in ships.items()}

        # Merge display names from global.mo if available
        mo_path = self.version_dir / "data" / "global.mo"
        if mo_path.exists():
            names = _generate_ship_names(ships, mo_path)
            for k, display_name in names.items():
                sid = int(k)
                if sid in result:
                    result[sid]["short_name"] = display_name

        return result

    @cached_property
    def projectiles_db(self) -> dict[int, dict]:
        """Projectile params_id → {a: ammo_type, c: caliber_mm}."""
        return {int(k): v for k, v in _extract_projectiles(self.gameparams).items()}

    @cached_property
    def ship_consumables(self) -> dict[int, dict]:
        """Ship ID → {slots, abilities, has_repair_party, ranges, timings}."""
        return {int(k): v for k, v in _extract_ship_consumables(self.gameparams).items()}

    @cached_property
    def aircraft_icon_map(self) -> dict[int, str]:
        """Aircraft params_id → icon_base string."""
        return {int(k): v for k, v in _extract_aircraft_icon_map(self.gameparams).items()}

    @classmethod
    def from_gamedata_path(cls, gamedata_path: Path) -> VersionedGamedata:
        """Cold-load fallback: build from a raw gamedata directory.

        Decodes ``{gamedata_path}/content/GameParams.data`` directly (with
        blake2b-keyed pickle cache for warm reloads).  No git archive, no
        version tag — just the files on disk.

        Args:
            gamedata_path: Path to ``wows-gamedata/data`` (or equivalent).

        Returns:
            VersionedGamedata with version_dir set to gamedata_path.

        Raises:
            FileNotFoundError: If GameParams.data does not exist.
        """
        gp_path = gamedata_path / "content" / "GameParams.data"
        if not gp_path.exists():
            raise FileNotFoundError(
                f"GameParams.data not found at {gp_path}. "
                "Cannot build VersionedGamedata without it."
            )

        gp = decode_and_cache_gameparams(gp_path, gamedata_path)

        return cls(
            version_dir=gamedata_path,
            build_id="unknown",
            _gameparams=gp,
        )


# ── Tag resolution ─────────────────────────────────────────────────


def _find_closest_tag(
    gamedata_repo: Path,
    target_build: int,
) -> str | None:
    """Find the closest version tag to *target_build*.

    1. Exact match ``v{target_build}``.
    2. Smallest absolute delta; ties prefer the older build.
    """
    try:
        result = subprocess.run(
            ["git", "tag", "-l", "v*"],
            cwd=gamedata_repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    tags: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        tag = line.strip()
        build_str = tag.lstrip("v")
        if build_str.isdigit():
            tags.append((int(build_str), tag))

    if not tags:
        return None

    for build, tag in tags:
        if build == target_build:
            return tag

    best = min(
        tags,
        key=lambda bt: (abs(bt[0] - target_build), bt[0] > target_build),
    )
    return best[1]


def _list_all_tags(gamedata_repo: Path) -> list[str]:
    """List all version tags (build IDs) in the gamedata repo."""
    try:
        result = subprocess.run(
            ["git", "tag", "-l", "v*"],
            cwd=gamedata_repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    build_ids: list[str] = []
    for line in result.stdout.splitlines():
        tag = line.strip().lstrip("v")
        if tag.isdigit():
            build_ids.append(tag)
    return build_ids


# ── Cache population ───────────────────────────────────────────────


def _git_archive_extract(
    gamedata_repo: Path,
    tag: str,
    output_dir: Path,
    paths: list[str],
) -> None:
    """Extract specific paths from a git tag via ``git archive | tar``.

    This does NOT modify the working tree — files are extracted directly
    into *output_dir* with the ``data/`` prefix stripped.
    """
    git_cmd = [
        "git", "-C", str(gamedata_repo),
        "archive", tag, "--", *paths,
    ]
    tar_cmd = [
        "tar", "-x", "-C", str(output_dir),
    ]

    git_proc = subprocess.Popen(git_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar_proc = subprocess.Popen(tar_cmd, stdin=git_proc.stdout, stderr=subprocess.PIPE)
    git_proc.stdout.close()  # allow git to receive SIGPIPE if tar exits early

    _, tar_err = tar_proc.communicate(timeout=300)
    _, git_err = git_proc.communicate(timeout=30)

    if git_proc.returncode != 0:
        raise subprocess.CalledProcessError(
            git_proc.returncode, git_cmd, stderr=git_err,
        )
    if tar_proc.returncode != 0:
        raise subprocess.CalledProcessError(
            tar_proc.returncode, tar_cmd, stderr=tar_err,
        )


def ensure_version_cache(
    build_id: str,
    gamedata_repo: Path,
    cache_root: Path | None = None,
) -> VersionedGamedata:
    """Return a VersionedGamedata for the given build, populating the cache if needed.

    Fast path: ``{cache_root}/v{build_id}/.ready`` exists → load pickle, return.

    Slow path: extract from git, decode GameParams, write cache atomically.

    Args:
        build_id: Game build ID (e.g. ``"12116141"``).
        gamedata_repo: Path to the wows-gamedata git repo root.
        cache_root: Override cache directory (default: ``~/.cache/wows-gamedata/``).

    Returns:
        VersionedGamedata ready for use.

    Raises:
        RuntimeError: If no matching tag is found or git archive fails.
    """
    if cache_root is None:
        cache_root = _DEFAULT_CACHE_ROOT
    cache_root.mkdir(parents=True, exist_ok=True)

    version_dir = cache_root / f"v{build_id}"

    # ── Fast path ──────────────────────────────────────────────
    if (version_dir / ".ready").exists():
        log.debug("Cache hit for v%s", build_id)
        return VersionedGamedata(
            version_dir=version_dir,
            build_id=build_id,
        )

    # ── Slow path: populate cache ──────────────────────────────
    log.info("Populating cache for v%s...", build_id)
    t0 = time.monotonic()

    # Find matching tag
    tag = _find_closest_tag(gamedata_repo, int(build_id))
    if tag is None:
        raise RuntimeError(
            f"No matching tag found for build {build_id} in {gamedata_repo}. "
            "Run 'git fetch --tags' in the gamedata repo."
        )
    if tag != f"v{build_id}":
        log.warning(
            "Exact tag v%s not found, using closest: %s", build_id, tag,
        )

    # Create temp dir (PID-namespaced for concurrency safety)
    tmp_dir = cache_root / f"_tmp_{build_id}_{os.getpid()}"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # Extract all needed files via git archive
        _git_archive_extract(
            gamedata_repo, tag, tmp_dir,
            [
                "data/scripts_entity/",
                "data/content/GameParams.data",
                "data/gui/",
                "data/spaces/",
                # Static JSON/data files (pipeline-generated, small)
                "data/map_sizes.json",
                "data/consumable_type_ids.json",
                "data/buff_drops.json",
                "data/aircraft_icons.json",
                "data/arena_key_maps.json",
                "data/ships.json",
                "data/ship_names.json",
                "data/projectiles.json",
                "data/global.mo",
            ],
        )

        # Decode GameParams and write pickle cache
        data_dir = tmp_dir / "data"
        gp_source = data_dir / "content" / "GameParams.data"
        gp = decode_and_cache_gameparams(gp_source, tmp_dir)

        # Generate derived data for consumable_calc.py
        # These go under data/ so the parser finds them via entity_defs traversal
        ship_consumables = _extract_ship_consumables(gp)
        sc_path = data_dir / "ship_consumables.json"
        sc_path.write_text(json.dumps(
            {str(k): v for k, v in ship_consumables.items()},
            separators=(",", ":"),
        ))

        n_split = write_split_subset(gp, data_dir, _SPLIT_TYPES_FOR_CACHE)
        log.info("Wrote %d split files (Modernization + Crew + Drop)", n_split)

        # Remove raw GameParams.data to save disk (~16 MB)
        content_dir = data_dir / "content"
        if content_dir.exists():
            shutil.rmtree(content_dir)

        # Write sentinel
        (tmp_dir / ".ready").write_text(f"v{build_id}\n")

        # Atomic rename
        try:
            tmp_dir.rename(version_dir)
        except OSError:
            # Race: another worker already created version_dir
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if (version_dir / ".ready").exists():
                log.info("Cache for v%s populated by another worker", build_id)
                gp = load_gameparams_cached(version_dir)
                return VersionedGamedata(
                    version_dir=version_dir,
                    build_id=build_id,
                    _gameparams=gp,
                )
            raise

        elapsed = time.monotonic() - t0
        log.info("Cache for v%s populated in %.1fs", build_id, elapsed)

        return VersionedGamedata(
            version_dir=version_dir,
            build_id=build_id,
            _gameparams=gp,
        )

    except BaseException:
        # Clean up temp dir on any failure
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


# ── Replay resolution ──────────────────────────────────────────────


def resolve_for_replay(
    replay_path: str | Path,
    gamedata_repo: Path,
    cache_root: Path | None = None,
) -> VersionedGamedata:
    """Resolve the correct gamedata version for a replay file.

    Reads the replay JSON header to extract the game version, then
    ensures the cache is populated for that version.

    Args:
        replay_path: Path to the .wowsreplay file.
        gamedata_repo: Path to the wows-gamedata git repo root.
        cache_root: Override cache directory.

    Returns:
        VersionedGamedata for the replay's game version.
    """
    from wows_replay_parser.gamedata_sync import extract_build_id
    from wows_replay_parser.replay.reader import ReplayReader

    replay = ReplayReader().read(Path(replay_path))
    build_id = extract_build_id(replay.game_version)

    if not build_id:
        raise RuntimeError(
            f"Could not extract build ID from replay version: "
            f"{replay.game_version}"
        )

    return ensure_version_cache(build_id, gamedata_repo, cache_root)


# ── Bulk population ────────────────────────────────────────────────


def populate_all_caches(
    gamedata_repo: Path,
    cache_root: Path | None = None,
) -> list[str]:
    """Populate caches for all version tags in the gamedata repo.

    Skips versions that already have a ``.ready`` sentinel.

    Args:
        gamedata_repo: Path to the wows-gamedata git repo root.
        cache_root: Override cache directory.

    Returns:
        List of build IDs that were newly populated.
    """
    if cache_root is None:
        cache_root = _DEFAULT_CACHE_ROOT

    all_builds = _list_all_tags(gamedata_repo)
    if not all_builds:
        log.warning("No version tags found in %s", gamedata_repo)
        return []

    log.info("Found %d version tags, checking caches...", len(all_builds))
    populated: list[str] = []

    for build_id in all_builds:
        version_dir = cache_root / f"v{build_id}"
        if (version_dir / ".ready").exists():
            log.debug("Cache already exists for v%s", build_id)
            continue

        try:
            ensure_version_cache(build_id, gamedata_repo, cache_root)
            populated.append(build_id)
        except Exception:
            log.exception("Failed to populate cache for v%s", build_id)

    if populated:
        log.info("Populated %d new caches: %s", len(populated), populated)
    else:
        log.info("All caches up to date")

    return populated


def get_cache_status(
    cache_root: Path | None = None,
) -> dict[str, bool]:
    """Return {build_id: is_ready} for all cached versions.

    Useful for monitoring and diagnostics.
    """
    if cache_root is None:
        cache_root = _DEFAULT_CACHE_ROOT
    if not cache_root.exists():
        return {}

    status: dict[str, bool] = {}
    for entry in cache_root.iterdir():
        if entry.is_dir() and entry.name.startswith("v"):
            build_id = entry.name[1:]
            status[build_id] = (entry / ".ready").exists()
    return status
