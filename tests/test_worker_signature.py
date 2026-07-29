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
    """Worker RSS is reported through the timings dict rather than the return
    tuple, precisely so the 8-tuple contract above stays unchanged.

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
