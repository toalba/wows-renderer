"""Unit tests for achievement extraction in gamedata_cache.

Pure-Python, no fixtures: pass a synthetic GameParams dict through the
extractor and assert the resulting id -> ui_name mapping.

Real GameParams convention (verified against v12506899): entry name is
'PCH<n>_<TitleCase>'; the icon-filename suffix lives in the entry's
'uiName' field.
"""
from __future__ import annotations

import json
from pathlib import Path

from renderer.gamedata_cache import (
    VersionedGamedata,
    _extract_achievement_map,
    _write_achievements_json,
)


def test_extract_achievement_map_reads_ui_name():
    gp = {
        "PCH034_ScienceOfWinning1": {
            "id": 4258456496,
            "uiName": "SCIENCE_OF_WINNING_ARSONIST",
            "typeinfo": {"type": "Achievement"},
        },
        "PCH144_BD2_RANKS": {
            "id": 4143113136,
            "uiName": "BD2_RANKS",
            "typeinfo": {"type": "Achievement"},
        },
    }
    result = _extract_achievement_map(gp)
    assert result == {
        "4258456496": "SCIENCE_OF_WINNING_ARSONIST",
        "4143113136": "BD2_RANKS",
    }


def test_extract_achievement_map_ignores_other_types():
    gp = {
        "PCH001_BattleHero": {
            "id": 1, "uiName": "BATTLE_HERO",
            "typeinfo": {"type": "Achievement"},
        },
        "PASA001_Iowa": {
            "id": 9999, "uiName": "IOWA",
            "typeinfo": {"type": "Ship"},
        },
        "PASUM001": {
            "id": 8888,
            "typeinfo": {"type": "Modernization"},
        },
    }
    result = _extract_achievement_map(gp)
    assert result == {"1": "BATTLE_HERO"}


def test_extract_achievement_map_skips_entries_without_id():
    gp = {
        "PCH002_NoId": {
            "uiName": "NO_ID",
            "typeinfo": {"type": "Achievement"},
        },
        "PCH003_Good": {
            "id": 42, "uiName": "GOOD",
            "typeinfo": {"type": "Achievement"},
        },
    }
    result = _extract_achievement_map(gp)
    assert result == {"42": "GOOD"}


def test_extract_achievement_map_skips_entries_without_ui_name():
    """Entries with no uiName (~2/426 in real data) have no icon, so skip them."""
    gp = {
        "PCH004_NoUiName": {
            "id": 7,
            "typeinfo": {"type": "Achievement"},
        },
        "PCH005_EmptyUiName": {
            "id": 8, "uiName": "",
            "typeinfo": {"type": "Achievement"},
        },
        "PCH006_Good": {
            "id": 9, "uiName": "GOOD",
            "typeinfo": {"type": "Achievement"},
        },
    }
    result = _extract_achievement_map(gp)
    assert result == {"9": "GOOD"}


def test_extract_achievement_map_handles_non_dict_values():
    """GameParams contains non-dict entries (lists, primitives); must skip cleanly."""
    gp = {
        "PCH007_Good": {
            "id": 1, "uiName": "GOOD",
            "typeinfo": {"type": "Achievement"},
        },
        "some_metadata_key": "not a dict",
        "another_key": ["list", "value"],
    }
    result = _extract_achievement_map(gp)
    assert result == {"1": "GOOD"}


def test_write_achievements_json_creates_file(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    gp = {
        "PCH001_First": {
            "id": 1, "uiName": "FIRST",
            "typeinfo": {"type": "Achievement"},
        },
        "PCH002_Second": {
            "id": 2, "uiName": "SECOND",
            "typeinfo": {"type": "Achievement"},
        },
    }
    _write_achievements_json(gp, data_dir)
    out = data_dir / "achievements.json"
    assert out.exists()
    loaded = json.loads(out.read_text())
    assert loaded == {"1": "FIRST", "2": "SECOND"}


def test_write_achievements_json_writes_empty_dict_for_empty_input(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_achievements_json({}, data_dir)
    out = data_dir / "achievements.json"
    assert out.exists()
    assert json.loads(out.read_text()) == {}


def _make_versioned_gamedata(tmp_path: Path, gp: dict | None = None) -> VersionedGamedata:
    """Build a VersionedGamedata pointing at a tmp dir.

    If gp is supplied it's used as the pre-loaded gameparams dict (no pickle
    file needed). Otherwise the caller is responsible for setting up the
    pickle on disk.
    """
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return VersionedGamedata(
        version_dir=tmp_path,
        build_id="test",
        _gameparams=gp,
    )


def test_achievements_loads_existing_json(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "achievements.json").write_text(
        json.dumps({"1": "FIRST", "2": "SECOND"})
    )
    vgd = _make_versioned_gamedata(tmp_path, gp={})  # gp irrelevant — file present
    assert vgd.achievements == {1: "FIRST", 2: "SECOND"}


def test_achievements_backfills_from_gameparams_when_json_missing(tmp_path: Path):
    gp = {
        "PCH999_Backfill": {
            "id": 99,
            "uiName": "BACKFILL",
            "typeinfo": {"type": "Achievement"},
        },
    }
    vgd = _make_versioned_gamedata(tmp_path, gp=gp)
    assert vgd.achievements == {99: "BACKFILL"}
    # Backfill should have written the file for future loads.
    out = tmp_path / "data" / "achievements.json"
    assert out.exists()
    assert json.loads(out.read_text()) == {"99": "BACKFILL"}


def test_achievements_returns_empty_dict_when_backfill_fails(tmp_path: Path, monkeypatch):
    """If the JSON is missing AND we can't access GameParams, return {}."""
    vgd = _make_versioned_gamedata(tmp_path, gp=None)

    def _boom(self):  # noqa: ANN001
        raise FileNotFoundError("no pickle here")

    monkeypatch.setattr(VersionedGamedata, "gameparams", property(_boom))
    assert vgd.achievements == {}
