# tests/test_worker_stats_job.py
"""Contract for the stats-board pool job (bot.worker.render_stats).

Fixture-free by design: the theme/layout guards run before any gamedata or
cairo work, so the failure paths are testable without a replay. The happy
path needs a real replay + gamedata and is covered by the end-to-end API
verification, not here.
"""
from __future__ import annotations

import inspect
import pickle

import pytest

from bot.worker import RenderResult, StatsUnavailableError, render_stats


def test_signature_matches_the_other_pool_jobs():
    """Same four positional args as render_replay/render_dual_replay so the
    job runner can build every render_call the same way."""
    params = list(inspect.signature(render_stats).parameters.values())
    positional = [p.name for p in params if p.kind is p.POSITIONAL_OR_KEYWORD]
    assert positional == ["replay_path", "output_path", "gamedata_path", "progress_queue"]

    kwonly = {p.name: p.default for p in params if p.kind is p.KEYWORD_ONLY}
    assert kwonly == {"flags": frozenset(), "theme": "default", "layout": "compact"}


def test_returns_render_result():
    assert inspect.signature(render_stats).return_annotation in (RenderResult, "RenderResult")


def test_stats_unavailable_error_is_a_picklable_runtime_error():
    """It crosses the ProcessPoolExecutor boundary, so it must pickle, and it
    must not be mistaken for a bug — the replay simply has no results packet."""
    assert issubclass(StatsUnavailableError, RuntimeError)
    revived = pickle.loads(pickle.dumps(StatsUnavailableError("no results")))
    assert isinstance(revived, StatsUnavailableError)
    assert str(revived) == "no results"


@pytest.mark.parametrize("theme", ["nope", "", "DEFAULT"])
def test_unknown_theme_fails_fast(theme):
    """render_stats_board would raise a bare KeyError deep inside a draw call;
    the guard turns it into a message the API can hand back as a 400/500 and
    runs before any replay parsing."""
    with pytest.raises(ValueError, match="theme"):
        render_stats("nonexistent.wowsreplay", "/tmp/x.png", "/tmp/gamedata", theme=theme)


@pytest.mark.parametrize("layout", ["wide", "COMPACT", ""])
def test_unknown_layout_fails_fast(layout):
    with pytest.raises(ValueError, match="layout"):
        render_stats("nonexistent.wowsreplay", "/tmp/x.png", "/tmp/gamedata", layout=layout)
