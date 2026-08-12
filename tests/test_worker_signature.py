"""Light contract tests for bot.worker — verify the public function
signatures accept the new `theme` kwarg without actually rendering."""
from __future__ import annotations

import inspect
import sys

from bot.worker import _peak_rss_bytes, render_dual_replay, render_replay


def test_render_replay_accepts_theme_kwarg():
    sig = inspect.signature(render_replay)
    assert "theme" in sig.parameters
    assert sig.parameters["theme"].default == "default"


def test_render_dual_replay_accepts_theme_kwarg():
    sig = inspect.signature(render_dual_replay)
    assert "theme" in sig.parameters
    assert sig.parameters["theme"].default == "default"


def test_peak_rss_reports_plausible_bytes():
    """Worker RSS is reported through the timings dict rather than the
    RenderResult dataclass, precisely so the field set pinned below stays
    stable regardless of what per-process metrics get added.

    Guard against the KiB/bytes unit mix-up: ru_maxrss is KiB on Linux and
    bytes on macOS. Any live Python process is well over 1 MiB, and a worker
    that appears to use more than the 4.5 GB container cap means the scaling
    is wrong.
    """
    rss = _peak_rss_bytes()
    assert isinstance(rss, float)
    if sys.platform.startswith("win"):
        assert rss == 0.0  # `resource` is Unix-only
    else:
        assert 1024 * 1024 < rss < 8 * 1024 * 1024 * 1024


def test_render_result_field_order_is_pinned():
    """The cog reads these by name now, but pickling across the process
    pool and any future positional construction still depend on the
    field set. Adding a field is fine; renaming or dropping one is not."""
    import dataclasses

    from bot.worker import RenderResult

    assert dataclasses.is_dataclass(RenderResult)
    assert [f.name for f in dataclasses.fields(RenderResult)] == [
        "output_path", "duration", "timings", "game_version",
        "num_players", "game_type", "build_urls", "chat_text", "stats",
    ]


def test_render_result_is_picklable():
    """It crosses a ProcessPoolExecutor boundary."""
    import pickle

    from bot.worker import RenderResult

    r = RenderResult(
        output_path="/tmp/a.mp4", duration=1.0, timings={}, game_version="15.6",
        num_players=24, game_type="RandomBattle", build_urls=[], chat_text="",
        stats=None,
    )
    assert pickle.loads(pickle.dumps(r)).game_version == "15.6"


def test_render_result_stats_defaults_to_none():
    """A replay with no BattleResults packet must construct cleanly."""
    import dataclasses

    from bot.worker import RenderResult

    stats_field = next(
        f for f in dataclasses.fields(RenderResult) if f.name == "stats"
    )
    assert stats_field.default is None


def test_extract_dual_stats_falls_back_on_replay_a_exception():
    """When replay A's extraction raises, replay B is still attempted.

    This is a regression test for a bug where both attempts sat inside one
    try/except, so a raise from A prevented B from being tried at all.
    Only returning None triggered the or operator.
    """
    from unittest.mock import Mock, patch

    from bot.worker import _extract_dual_stats

    # Mock stats object
    mock_stats = Mock(name="MatchStats")
    timings = {}

    # Patch extract_match_stats with side_effect: first call raises, second returns stats
    with patch("renderer.stats_export.extract_match_stats") as mock_extract:
        mock_extract.side_effect = [
            Exception("replay_a extraction failed"),
            mock_stats,  # replay_b succeeds
        ]

        result = _extract_dual_stats(
            replay_a="mock_a",
            replay_b="mock_b",
            vgd="mock_vgd",
            flags=frozenset(),
            timings=timings,
        )

    # Verify result came from replay B
    assert result is mock_stats
    # Verify both calls were made (the second even though the first raised)
    assert mock_extract.call_count == 2
    # Verify timings were recorded
    assert "stats" in timings
    assert isinstance(timings["stats"], float)
