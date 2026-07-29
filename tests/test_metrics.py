"""Unit tests for bot.metrics.

These need no replay or gamedata fixtures, so unlike the integration suite
they actually run in CI rather than skipping.

Counters are process-global, so every assertion is written as a *delta*
against a value sampled before the call under test.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import metrics

prometheus_client = pytest.importorskip("prometheus_client")
REGISTRY = prometheus_client.REGISTRY


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Current value of a metric sample, 0.0 if it has no observations yet."""
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


@pytest.fixture
def timings() -> dict[str, object]:
    """A worker timings dict shaped like the real one, including the
    heterogeneous entries (`layer_init` is a nested dict)."""
    return {
        "resolve": 0.02,
        "parse": 1.5,
        "setup": 2.0,
        "render": 30.0,
        "encode": 4.0,
        "build_urls": 0.0,
        "_frames": 1200.0,
        "_peak_rss_bytes": 1_500_000_000.0,
        "layer_init": {"ShipLayer": 0.25, "MapBackgroundLayer": 0.5},
    }


# ---------------------------------------------------------------------------
# Outcome accounting
# ---------------------------------------------------------------------------


def test_tracker_defaults_to_error() -> None:
    """A path that neither succeeds nor sets an outcome must not be counted
    as a success."""
    tracker = metrics.RenderTracker("render", "full")
    assert tracker.outcome == metrics.OUTCOME_ERROR


@pytest.mark.parametrize(
    "outcome",
    [
        metrics.OUTCOME_SUCCESS,
        metrics.OUTCOME_TIMEOUT,
        metrics.OUTCOME_WORKER_CRASH,
        metrics.OUTCOME_ERROR,
        metrics.OUTCOME_OVERSIZE,
    ],
)
def test_finish_render_counts_each_outcome(outcome: str) -> None:
    labels = {"command": "test_cmd", "preset": "full", "outcome": outcome}
    before = _sample("wows_renders_total", labels)

    tracker = metrics.RenderTracker("test_cmd", "full")
    tracker.outcome = outcome
    metrics.finish_render(tracker)

    assert _sample("wows_renders_total", labels) == before + 1


def test_finish_render_is_idempotent() -> None:
    """The batch path can finalise on an early return *and* again in the
    caller — the second call must not double-count."""
    labels = {"command": "idem_cmd", "preset": "map", "outcome": metrics.OUTCOME_TIMEOUT}
    before = _sample("wows_renders_total", labels)

    tracker = metrics.RenderTracker("idem_cmd", "map")
    tracker.outcome = metrics.OUTCOME_TIMEOUT
    metrics.finish_render(tracker)
    metrics.finish_render(tracker)
    metrics.finish_render(tracker)

    assert _sample("wows_renders_total", labels) == before + 1


def test_record_render_does_not_count_by_itself(timings: dict[str, object]) -> None:
    """record_render only observes histograms; finish_render owns the
    counter, so an oversize downgrade after the fact still lands correctly."""
    labels = {"command": "nocount", "preset": "full", "outcome": metrics.OUTCOME_SUCCESS}
    before = _sample("wows_renders_total", labels)

    tracker = metrics.RenderTracker("nocount", "full")
    metrics.record_render(tracker, timings)

    assert tracker.outcome == metrics.OUTCOME_SUCCESS
    assert _sample("wows_renders_total", labels) == before


def test_oversize_overrides_success_outcome(timings: dict[str, object]) -> None:
    """Mirrors the cog: record_render runs first, then the oversize branch
    downgrades the outcome before the finally block records it."""
    success = {"command": "over", "preset": "full", "outcome": metrics.OUTCOME_SUCCESS}
    oversize = {"command": "over", "preset": "full", "outcome": metrics.OUTCOME_OVERSIZE}
    before_success = _sample("wows_renders_total", success)
    before_oversize = _sample("wows_renders_total", oversize)

    tracker = metrics.RenderTracker("over", "full")
    metrics.record_render(tracker, timings)
    metrics.record_delivery(tracker, elapsed=40.0)
    tracker.outcome = metrics.OUTCOME_OVERSIZE
    metrics.finish_render(tracker)

    assert _sample("wows_renders_total", success) == before_success
    assert _sample("wows_renders_total", oversize) == before_oversize + 1


def test_phases_survive_a_failed_delivery(timings: dict[str, object]) -> None:
    """The whole point of splitting record_render out: a Discord upload that
    hangs raises TimeoutError — the same type as the render deadline — and
    must not erase timings for a render that actually completed."""
    phase = {"command": "faildel", "phase": "render"}
    upload_failed = {"command": "faildel", "preset": "full", "outcome": metrics.OUTCOME_UPLOAD_FAILED}
    timeout = {"command": "faildel", "preset": "full", "outcome": metrics.OUTCOME_TIMEOUT}
    before_phase = _sample("wows_render_phase_seconds_count", phase)
    before_upload_failed = _sample("wows_renders_total", upload_failed)
    before_timeout = _sample("wows_renders_total", timeout)

    tracker = metrics.RenderTracker("faildel", "full")
    metrics.record_render(tracker, timings)
    # Delivery blows up here — record_delivery is never reached.
    tracker.outcome = metrics.OUTCOME_UPLOAD_FAILED
    metrics.finish_render(tracker)

    assert _sample("wows_render_phase_seconds_count", phase) == before_phase + 1, (
        "render phase timing was lost when delivery failed"
    )
    assert _sample("wows_renders_total", upload_failed) == before_upload_failed + 1
    assert _sample("wows_renders_total", timeout) == before_timeout, (
        "a Discord failure must not be counted as a render timeout"
    )


def test_e2e_includes_upload() -> None:
    """The queue-wait panel derives itself as e2e minus the sum of the
    phases, and the phases include upload. If e2e excluded upload the panel
    would be biased negative by exactly the upload duration."""
    labels = {"command": "e2e", "preset": "full"}
    tracker = metrics.RenderTracker("e2e", "full")
    metrics.record_delivery(tracker, elapsed=50.0, upload_seconds=8.0)

    e2e_sum = _sample("wows_render_e2e_seconds_sum", labels)
    upload_sum = _sample("wows_render_phase_seconds_sum", {"command": "e2e", "phase": "upload"})
    assert e2e_sum == 50.0
    assert upload_sum == 8.0
    assert e2e_sum > upload_sum, "e2e must be measured after the upload, not before"


# ---------------------------------------------------------------------------
# Phase / resource observations
# ---------------------------------------------------------------------------


def test_record_render_observes_every_phase(timings: dict[str, object]) -> None:
    phases = ("resolve", "parse", "setup", "render", "encode")
    before = {
        p: _sample("wows_render_phase_seconds_count", {"command": "phases", "phase": p})
        for p in phases
    }

    tracker = metrics.RenderTracker("phases", "full")
    metrics.record_render(tracker, timings)
    metrics.record_delivery(tracker, elapsed=40.0, upload_seconds=3.0)

    for phase in phases:
        labels = {"command": "phases", "phase": phase}
        assert _sample("wows_render_phase_seconds_count", labels) == before[phase] + 1, phase

    upload = {"command": "phases", "phase": "upload"}
    assert _sample("wows_render_phase_seconds_count", upload) >= 1


def test_batch_mode_skips_upload_and_output_size(timings: dict[str, object]) -> None:
    """Batch delivers via a follow-up message, so it has neither phase."""
    upload = {"command": "batchy", "phase": "upload"}
    output = {"command": "batchy"}
    before_upload = _sample("wows_render_phase_seconds_count", upload)
    before_output = _sample("wows_render_output_bytes_count", output)

    tracker = metrics.RenderTracker("batchy", "full")
    metrics.record_render(tracker, timings)
    metrics.record_delivery(tracker, elapsed=40.0)

    assert _sample("wows_render_phase_seconds_count", upload) == before_upload
    assert _sample("wows_render_output_bytes_count", output) == before_output


def test_record_render_observes_frames_rss_and_layers(timings: dict[str, object]) -> None:
    frames_before = _sample("wows_render_frames_total", {"command": "res"})
    rss_before = _sample("wows_render_worker_peak_rss_bytes_count", {"command": "res"})
    layer_before = _sample("wows_render_layer_init_seconds_count", {"layer": "ShipLayer"})

    tracker = metrics.RenderTracker("res", "full")
    metrics.record_render(tracker, timings, output_bytes=12 * 1024 * 1024)

    assert _sample("wows_render_frames_total", {"command": "res"}) == frames_before + 1200
    assert _sample("wows_render_worker_peak_rss_bytes_count", {"command": "res"}) == rss_before + 1
    assert _sample("wows_render_layer_init_seconds_count", {"layer": "ShipLayer"}) == layer_before + 1
    assert _sample("wows_render_output_bytes_count", {"command": "res"}) == 1


def test_version_counter_carries_version_and_type(timings: dict[str, object]) -> None:
    labels = {"game_version": "15.6.0", "game_type": "RandomBattle"}
    before = _sample("wows_renders_by_version_total", labels)

    tracker = metrics.RenderTracker("ver", "full")
    metrics.record_render(
        tracker, timings, game_version="15.6.0", game_type="RandomBattle",
    )

    assert _sample("wows_renders_by_version_total", labels) == before + 1


def test_missing_game_version_is_not_recorded(timings: dict[str, object]) -> None:
    """An empty version must not create a `game_version=""` series."""
    tracker = metrics.RenderTracker("nover", "full")
    metrics.record_render(tracker, timings)

    assert REGISTRY.get_sample_value(
        "wows_renders_by_version_total", {"game_version": "", "game_type": "Unknown"},
    ) is None


def test_empty_timings_does_not_raise() -> None:
    """A worker that died mid-way can return a sparse dict."""
    tracker = metrics.RenderTracker("sparse", "full")
    metrics.record_render(tracker, {})
    metrics.record_delivery(tracker, elapsed=1.0)
    assert tracker.outcome == metrics.OUTCOME_SUCCESS


def test_non_numeric_timings_entries_are_ignored() -> None:
    """`layer_init` is a dict, not a float — coercion must not explode."""
    tracker = metrics.RenderTracker("weird", "full")
    metrics.record_render(
        tracker,
        {"render": {"nested": "dict"}, "parse": None, "encode": "not a number", "layer_init": "nope"},
    )
    assert tracker.outcome == metrics.OUTCOME_SUCCESS


def test_as_float_rejects_bools() -> None:
    """bool is an int subclass; True must not become 1.0 of anything."""
    assert metrics._as_float(True) == 0.0
    assert metrics._as_float(2.5) == 2.5
    assert metrics._as_float(None) == 0.0


# ---------------------------------------------------------------------------
# Standalone recorders
# ---------------------------------------------------------------------------


def test_pool_rebuild_counter() -> None:
    before = _sample("wows_pool_rebuilds_total")
    metrics.record_pool_rebuild()
    assert _sample("wows_pool_rebuilds_total") == before + 1


def test_cache_populated_ignores_zero() -> None:
    before = _sample("wows_gamedata_cache_populated_total")
    metrics.record_cache_populated(0)
    assert _sample("wows_gamedata_cache_populated_total") == before
    metrics.record_cache_populated(3)
    assert _sample("wows_gamedata_cache_populated_total") == before + 3


def test_loop_lag_clamps_negative() -> None:
    """Clock jitter can make the measured lag slightly negative; a negative
    observation would corrupt the histogram sum."""
    before_sum = _sample("wows_bot_event_loop_lag_seconds_sum")
    metrics.observe_loop_lag(-0.5)
    assert _sample("wows_bot_event_loop_lag_seconds_sum") == before_sum


# Driven through asyncio.run rather than pytest-asyncio so the suite needs no
# extra plugin (see .github/workflows/ci.yml — CI installs only `uv sync`).


def test_track_pool_future_decrements_on_completion() -> None:
    async def scenario() -> tuple[float, float, float]:
        before = _sample("wows_renders_in_flight")
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        metrics.track_pool_future(future)
        during = _sample("wows_renders_in_flight")

        future.set_result("done")
        await asyncio.sleep(0)  # let the done-callback run
        return before, during, _sample("wows_renders_in_flight")

    before, during, after = asyncio.run(scenario())
    assert during == before + 1
    assert after == before


def test_track_pool_future_decrements_on_cancel() -> None:
    """A timed-out render cancels its future — the gauge must still drop."""

    async def scenario() -> tuple[float, float]:
        before = _sample("wows_renders_in_flight")
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        metrics.track_pool_future(future)

        future.cancel()
        await asyncio.sleep(0)
        return before, _sample("wows_renders_in_flight")

    before, after = asyncio.run(scenario())
    assert after == before
