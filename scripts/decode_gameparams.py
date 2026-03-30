#!/usr/bin/env python3
"""Decode GameParams.data into JSON and extract ships.json lookup.

GameParams.data format (discovered via reverse engineering):
    1. Reverse ALL bytes of the file
    2. Zlib decompress
    3. Pickle load (Python 2 pickle, references GameParams module classes)

The pickle contains GameParams module classes (dict subclasses). We use a custom
unpickler that maps all GameParams.* classes to GPObject (a plain dict subclass
with __setstate__ support).

Outputs:
    --full:  GameParams-0.json (386MB+) — full decoded dump, useful for research
    --split: split/<Type>/<EntityName>.json — per-entity files by typeinfo.type
    --ships: ships.json — compact shipId → {name, species, nation, level} lookup
             (this is the one the renderer actually needs)

Usage:
    python scripts/decode_gameparams.py ../wows-gamedata/data/content/GameParams.data
    python scripts/decode_gameparams.py --ships-only ../wows-gamedata/data/content/GameParams.data
"""

from __future__ import annotations

import argparse
import copyreg
import io
import json
import pickle
import struct
import sys
import zlib
from pathlib import Path


class GPObject(dict):
    """Stand-in for GameParams module classes (dict subclasses in the pickle)."""

    def __setstate__(self, state):
        if isinstance(state, dict):
            self.update(state)


class GameParamsUnpickler(pickle.Unpickler):
    """Custom unpickler that maps GameParams.* classes to GPObject."""

    def find_class(self, module, name):
        if module == "GameParams":
            return GPObject
        return super().find_class(module, name)


def decode_gameparams(path: Path) -> dict:
    """Decode GameParams.data → Python dict.

    Format: reverse all bytes → zlib decompress → pickle load.
    """
    raw = path.read_bytes()
    reversed_data = struct.pack("B" * len(raw), *raw[::-1])
    decompressed = zlib.decompress(reversed_data)

    # Patch copyreg._reconstructor to handle dict subclass creation
    original = copyreg._reconstructor

    def patched(cls, base, state):
        if issubclass(cls, dict):
            obj = dict.__new__(cls)
            if state is not None:
                dict.__init__(obj, state)
            return obj
        return original(cls, base, state)

    copyreg._reconstructor = patched
    try:
        gp = GameParamsUnpickler(
            io.BytesIO(decompressed), encoding="latin1"
        ).load()
    finally:
        copyreg._reconstructor = original

    # Navigate to inner dict (gp is a list with one element containing a '' key)
    if isinstance(gp, (list, tuple)):
        gp = gp[0]
    if isinstance(gp, dict) and "" in gp:
        gp = gp[""]
    return gp


def extract_ships(gp: dict) -> dict[str, dict]:
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


def generate_ship_names(ships: dict[str, dict], mo_path: Path) -> dict[str, str]:
    """Generate {shipId: display_name} from ships.json + global.mo.

    Looks up IDS_{index} in the gettext catalog for each ship.
    Falls back to a cleaned version of the internal name.
    """
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
            print(f"Warning: failed to parse {mo_path}: {e}")

    names: dict[str, str] = {}
    for ship_id, info in ships.items():
        index = info.get("index", "")
        display = None

        # Try .mo lookup
        if catalog and index:
            key = f"IDS_{index}"
            val = catalog.gettext(key)
            if val != key:
                display = val

        # Fallback: strip prefix + underscores from internal name
        if not display:
            raw = info.get("name", "")
            display = re.sub(r"^P[A-Z]{3}\d+_", "", raw).replace("_", " ")

        names[ship_id] = display

    return names


def extract_projectiles(gp: dict) -> dict[str, dict]:
    """Extract projectiles lookup: {id: {a: ammoType, c: caliber_mm}}."""
    projectiles = {}
    for name, obj in gp.items():
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


def make_serializable(obj):
    """Recursively convert non-JSON-serializable types."""
    if isinstance(obj, dict):
        return {str(k): make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [make_serializable(v) for v in obj]
    if isinstance(obj, bytes):
        try:
            return obj.decode("latin1")
        except Exception:
            return obj.hex()
    if isinstance(obj, (int, float, str, bool, type(None))):
        return obj
    return str(obj)


def split_by_type(gp: dict, output_dir: Path) -> None:
    """Split GameParams into per-entity JSON files by typeinfo.type."""
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if not isinstance(ti, dict):
            continue
        entity_type = ti.get("type", "Other")
        type_dir = output_dir / entity_type
        type_dir.mkdir(parents=True, exist_ok=True)
        out = type_dir / f"{name}.json"
        out.write_text(json.dumps(make_serializable(obj), indent=4))


def main():
    parser = argparse.ArgumentParser(description="Decode GameParams.data")
    parser.add_argument("input", type=Path, help="Path to GameParams.data")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Output directory (default: cwd)",
    )
    parser.add_argument(
        "--ships-only",
        action="store_true",
        help="Only extract ships.json (fast, no full dump)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Write full GameParams-0.json (386MB+)",
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="Split into per-entity JSON files by type",
    )

    args = parser.parse_args()

    print(f"Decoding {args.input} ...")
    gp = decode_gameparams(args.input)
    print(f"Loaded {len(gp)} entities")

    # Always extract ships.json, projectiles.json, and ship_names.json
    ships = extract_ships(gp)
    ships_path = args.output_dir / "ships.json"
    ships_path.write_text(json.dumps(ships, indent=2, sort_keys=True))
    print(f"Saved {len(ships)} ships to {ships_path}")

    projectiles = extract_projectiles(gp)
    proj_path = args.output_dir / "projectiles.json"
    proj_path.write_text(json.dumps(projectiles, separators=(",", ":")))
    print(f"Saved {len(projectiles)} projectiles to {proj_path}")

    # Generate ship display names from global.mo
    mo_path = args.output_dir / "global.mo"
    ship_names = generate_ship_names(ships, mo_path)
    names_path = args.output_dir / "ship_names.json"
    names_path.write_text(json.dumps(ship_names, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    mo_count = sum(1 for n in ship_names.values() if n)
    print(f"Saved {mo_count} ship display names to {names_path}")

    if args.ships_only:
        return

    if args.full:
        full_path = args.output_dir / "GameParams-0.json"
        print(f"Writing full dump to {full_path} ...")
        full_path.write_text(json.dumps(make_serializable(gp), indent=2))
        print(f"Done ({full_path.stat().st_size / 1e6:.0f} MB)")

    if args.split:
        split_dir = args.output_dir / "split"
        print(f"Splitting by type into {split_dir}/ ...")
        split_by_type(gp, split_dir)
        print("Done")


if __name__ == "__main__":
    main()
