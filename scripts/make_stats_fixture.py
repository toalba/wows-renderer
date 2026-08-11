"""Build the sanitised BattleResults test fixture.

Usage:
    python scripts/make_stats_fixture.py <input.postbattle.json>

Reads the `raw` dict (the exact payload `BattleResults._decode` consumes)
and rewrites identifying fields. Numeric stats pass through unchanged so
the fixture keeps its value as a regression baseline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from wows_replay_parser.battle_results import PLAYER_INFO_FIELDS, VEH_BASE_RESULT_FIELDS

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "battle_results_sample.json"

# Indices into the 538-element playersPublicInfo row. These come from
# PLAYER_INFO_FIELDS in wows_replay_parser/battle_results.py.
IDX_ACCOUNT_DB_ID = 0
IDX_NAME = 1
IDX_CLAN_ID = 2
IDX_CLAN_TAG = 3

# killer_db_id lives further into the row, past PLAYER_INFO_FIELDS, in the
# VEH_BASE_RESULT_FIELDS block. Computed rather than hard-coded so it tracks
# the parser's schema if a field gets inserted upstream.
IDX_KILLER_DB_ID = len(PLAYER_INFO_FIELDS) + VEH_BASE_RESULT_FIELDS.index("killer_db_id")


def main(src: Path) -> None:
    doc = json.loads(src.read_text())
    raw = doc["raw"]

    ppi = raw["playersPublicInfo"]
    id_map = {old: 100_000 + i for i, old in enumerate(sorted(ppi, key=str))}

    sanitised: dict[str, list] = {}
    for i, (old_key, row) in enumerate(sorted(ppi.items(), key=lambda kv: str(kv[0]))):
        row = list(row)
        row[IDX_ACCOUNT_DB_ID] = id_map[old_key]
        row[IDX_NAME] = f"Player{i + 1:02d}"
        row[IDX_CLAN_ID] = 0
        row[IDX_CLAN_TAG] = f"CL{i % 2}"
        # killer_db_id references another row's *original* db_id (or 0 for
        # "no killer" / environmental deaths). Remap it through the same
        # table so the sanitised fixture still joins to a row inside the
        # sanitised payload instead of a real account id that no longer
        # appears anywhere in the file.
        killer_key = str(row[IDX_KILLER_DB_ID])
        if killer_key in id_map:
            row[IDX_KILLER_DB_ID] = id_map[killer_key]
        sanitised[str(id_map[old_key])] = row

    raw["playersPublicInfo"] = sanitised
    raw["accountDBID"] = id_map[sorted(ppi, key=str)[0]]
    # privateDataList carries the recorder's economics — not needed and
    # the most identifying part of the payload.
    raw["privateDataList"] = []

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(raw, indent=1))
    print(f"wrote {OUT} ({len(sanitised)} players)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
