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
import json
from pathlib import Path

from renderer.gamedata_cache import (
    _extract_projectiles as extract_projectiles,
    _extract_ship_consumables as extract_ship_consumables,
    _extract_ships as extract_ships,
    _generate_ship_names as generate_ship_names,
)
from renderer.gameparams import (
    decode_gameparams,
    make_serializable,
    split_by_type,
)


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

    # Generate ship consumable loadouts
    consumables = extract_ship_consumables(gp)
    cons_path = args.output_dir / "ship_consumables.json"
    cons_path.write_text(json.dumps(consumables, separators=(",", ":")))
    print(f"Saved {len(consumables)} ship consumable loadouts to {cons_path}")

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
