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
    """Extract ships lookup: {shipId: {name, species, nation, level}}."""
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
            "species": ti.get("species", ""),
            "nation": ti.get("nation", ""),
            "level": obj.get("level", 0),
        }
    return ships


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

    # Always extract ships.json
    ships = extract_ships(gp)
    ships_path = args.output_dir / "ships.json"
    ships_path.write_text(json.dumps(ships, indent=2, sort_keys=True))
    print(f"Saved {len(ships)} ships to {ships_path}")

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
