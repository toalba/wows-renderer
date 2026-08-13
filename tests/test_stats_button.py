# tests/test_stats_button.py
"""The Statistics button follows the same presence rule as Download Chat:
present when there is something to show, removed otherwise."""
from __future__ import annotations

import pytest

pytest.importorskip("discord")

from bot.cog_render import _RenderResultView
from renderer.stats_export import MatchStats


def _view(stats):
    return _RenderResultView(
        build_urls=[], chat_text="", chat_filename="c.txt",
        stats=stats, theme="default",
    )


def _labels(view):
    return {item.label for item in view.children}


def test_button_removed_when_no_stats():
    assert "Statistics" not in _labels(_view(None))


def test_button_present_when_stats_available():
    stats = MatchStats(
        players=(), map_name="Ocean", game_type="RandomBattle",
        duration_sec=600, winner_team=0, neutral_perspective=False,
    )
    assert "Statistics" in _labels(_view(stats))


def test_view_with_nothing_to_offer_has_no_children():
    """cog_render only attaches the view when it has children; an empty
    view must stay empty so no bare button row is posted."""
    assert _view(None).children == []
