"""Light contract tests for bot.worker — verify the public function
signatures accept the new `theme` kwarg without actually rendering."""
from __future__ import annotations

import inspect

from bot.worker import render_dual_replay, render_replay


def test_render_replay_accepts_theme_kwarg():
    sig = inspect.signature(render_replay)
    assert "theme" in sig.parameters
    assert sig.parameters["theme"].default == "default"


def test_render_dual_replay_accepts_theme_kwarg():
    sig = inspect.signature(render_dual_replay)
    assert "theme" in sig.parameters
    assert sig.parameters["theme"].default == "default"
