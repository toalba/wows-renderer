"""Unit tests for achievement extraction in gamedata_cache.

Pure-Python, no fixtures: pass a synthetic GameParams dict through the
extractor and assert the resulting id -> ui_name mapping.

Real GameParams convention (verified against v12506899): entry name is
'PCH<n>_<TitleCase>'; the icon-filename suffix lives in the entry's
'uiName' field.
"""
from __future__ import annotations

from renderer.gamedata_cache import _extract_achievement_map


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
