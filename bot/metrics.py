"""Prometheus metrics for the render bot.

Everything here runs in the **parent** (bot) process only. Renders happen in
``ProcessPoolExecutor`` children, but each child returns its ``timings`` dict
to the parent (see :func:`bot.worker.render_replay`), so there is nothing to
collect inside a worker. That is what lets this module use a plain default
registry instead of ``prometheus_client``'s file-backed multiprocess mode.

``start_http_server`` runs a daemon thread. ``fork`` does not carry threads
into the child, so workers never inherit the listening socket — true for the
``spawn`` start method as well (which is what ``RENDER_MAX_TASKS_PER_CHILD``
silently switches the pool to).

The renderer library itself stays free of any prometheus dependency: it keeps
reporting through its ``timings`` dict, and the translation to metrics happens
here.
"""
from __future__ import annotations

import logging
from asyncio import Future
from collections.abc import Mapping
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, start_http_server

log = logging.getLogger(__name__)

# Outcome label values for `wows_renders_total`.
OUTCOME_SUCCESS = "success"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_WORKER_CRASH = "worker_crash"
OUTCOME_ERROR = "error"
# The render itself succeeded but the mp4 exceeded Discord's attachment limit,
# so the user got no video. Kept distinct from `success` so a size regression
# is visible as a failure rate, not just as a shifting output_bytes p99.
OUTCOME_OVERSIZE = "oversize"
# The render succeeded and the video was within limits, but handing it to
# Discord failed or hung. Distinct from `timeout`/`error` so an outage on
# Discord's side is never misread as the renderer being slow or broken — a
# hung upload raises TimeoutError, which is the *same* exception type as the
# render deadline.
OUTCOME_UPLOAD_FAILED = "upload_failed"

# Phases pulled straight out of the worker's timings dict. `upload` is not in
# here because it is measured in the parent, after the future resolves, and is
# absent entirely in batch mode.
_PHASES = ("resolve", "parse", "setup", "render", "encode")

# One bucket set has to span the whole phase family, from a warm gamedata
# cache resolve (single-digit ms) to an encode on a long match (minutes), so
# it is deliberately wide rather than tuned per phase.
#
# Two bands are deliberately dense, both chosen from measured renders rather
# than guesswork:
#   0.1-1s   - encode lands at 0.2-0.7s. With only 0.25 and 1.0 around it,
#              44 of 47 real samples fell in a single bucket and p99 reported
#              0.99s against an observed maximum of 0.7s.
#   15-120s  - where the render phase lives.
_PHASE_BUCKETS = (
    0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0,
    15.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0, 600.0,
)

# End-to-end covers queue wait + render + Discord upload. Prod runs
# RENDER_TIMEOUT=300, so the tail needs headroom past that.
_E2E_BUCKETS = (1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0, 600.0)

# Layer init is per-layer setup inside one render — always sub-second.
_LAYER_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)

_MB = 1024 * 1024
_GB = 1024 * _MB

# Sized against DISCORD_ATTACHMENT_LIMIT_MB (25 MB, cog_render.py) so the p99
# panel shows how close output is drifting to a hard failure. 25 MB is a
# bucket boundary on purpose.
_OUTPUT_BUCKETS = (
    1 * _MB, 2 * _MB, 5 * _MB, 10 * _MB, 15 * _MB, 20 * _MB,
    25 * _MB, 30 * _MB, 40 * _MB, 50 * _MB, 75 * _MB, 100 * _MB,
)

# Sized against the 4.5 GB container cap (docker-compose.yml) — this bot has
# been OOM-killed before at a lower cap.
_RSS_BUCKETS = (
    256 * _MB, 512 * _MB, 768 * _MB, 1 * _GB, 1.5 * _GB, 2 * _GB,
    2.5 * _GB, 3 * _GB, 3.5 * _GB, 4 * _GB, 4.5 * _GB,
)

_LAG_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

RENDERS_TOTAL = Counter(
    "wows_renders_total",
    "Render attempts by command and terminal outcome.",
    ["command", "preset", "outcome"],
)

RENDERS_BY_VERSION_TOTAL = Counter(
    "wows_renders_by_version_total",
    "Successful renders by game version and battle type. Kept off the "
    "histograms so their series count stays flat.",
    ["game_version", "game_type"],
)

RENDER_PHASE_SECONDS = Histogram(
    "wows_render_phase_seconds",
    "Per-phase render duration.",
    ["command", "phase"],
    buckets=_PHASE_BUCKETS,
)

RENDER_E2E_SECONDS = Histogram(
    "wows_render_e2e_seconds",
    "End-to-end wall time from dispatch to delivered video, upload included. "
    "Must stay inclusive of upload: the queue-wait panel derives itself by "
    "subtracting the sum of the phases, and the phases include upload.",
    ["command", "preset"],
    buckets=_E2E_BUCKETS,
)

RENDER_LAYER_INIT_SECONDS = Histogram(
    "wows_render_layer_init_seconds",
    "Per-layer initialisation time within a render's setup phase.",
    ["layer"],
    buckets=_LAYER_BUCKETS,
)

RENDER_FRAMES_TOTAL = Counter(
    "wows_render_frames_total",
    "Frames encoded. Divide by phase render time for frames/sec.",
    ["command"],
)

RENDER_OUTPUT_BYTES = Histogram(
    "wows_render_output_bytes",
    "Size of the produced mp4.",
    ["command"],
    buckets=_OUTPUT_BUCKETS,
)

RENDER_WORKER_PEAK_RSS_BYTES = Histogram(
    "wows_render_worker_peak_rss_bytes",
    "Worker process peak RSS (ru_maxrss). This is a per-process high-water "
    "mark that never resets, so with worker recycling disabled it covers the "
    "worker's whole lifetime, not a single render.",
    ["command"],
    buckets=_RSS_BUCKETS,
)

RENDERS_IN_FLIGHT = Gauge(
    "wows_renders_in_flight",
    "Renders dispatched to the process pool and not yet resolved. Compare "
    "against MAX_WORKERS to see pool saturation. Excludes the download and "
    "Discord-upload phases, which do not occupy a worker.",
)

POOL_REBUILDS_TOTAL = Counter(
    "wows_pool_rebuilds_total",
    "Times the ProcessPoolExecutor was rebuilt after breaking.",
)

GAMEDATA_CACHE_POPULATED_TOTAL = Counter(
    "wows_gamedata_cache_populated_total",
    "Gamedata version caches populated at startup.",
)

EVENT_LOOP_LAG_SECONDS = Histogram(
    "wows_bot_event_loop_lag_seconds",
    "Delay beyond the requested sleep on a 1s tick — detects a blocked "
    "asyncio loop.",
    buckets=_LAG_BUCKETS,
)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def start_metrics_server(port: int, addr: str = "0.0.0.0") -> bool:
    """Serve ``/metrics`` on ``addr:port`` from a daemon thread.

    Returns False (and logs) instead of raising if the port is unavailable —
    a missing metrics endpoint must never stop the bot from starting.
    """
    try:
        start_http_server(port, addr)
    except OSError:
        log.exception("metrics: could not bind %s:%d — metrics endpoint disabled", addr, port)
        return False
    log.info("metrics: serving Prometheus metrics on %s:%d/metrics", addr, port)
    return True


# ---------------------------------------------------------------------------
# Render tracking
# ---------------------------------------------------------------------------


class RenderTracker:
    """Mutable outcome holder for one render attempt.

    Defaults to ``error`` so a code path that neither succeeds nor hits a
    known handler is never silently counted as a success. The cog's own
    ``except`` blocks swallow their exceptions, so the outcome cannot be
    inferred from an exception — call sites set it explicitly.
    """

    __slots__ = ("_recorded", "command", "outcome", "preset")

    def __init__(self, command: str, preset: str) -> None:
        self.command = command
        self.preset = preset
        self.outcome = OUTCOME_ERROR
        self._recorded = False


def finish_render(tracker: RenderTracker) -> None:
    """Record the terminal outcome for ``tracker``.

    Idempotent, so it is safe both in a ``finally`` block and on an early
    return path.
    """
    if tracker._recorded:
        return
    tracker._recorded = True
    RENDERS_TOTAL.labels(
        command=tracker.command, preset=tracker.preset, outcome=tracker.outcome,
    ).inc()


def track_pool_future(future: Future[Any]) -> None:
    """Count ``future`` as occupying a pool worker until it resolves.

    Note the decrement fires when the *future* resolves, including on
    cancellation — a timed-out render's worker may still be churning after
    the gauge drops.
    """
    RENDERS_IN_FLIGHT.inc()
    future.add_done_callback(lambda _: RENDERS_IN_FLIGHT.dec())


def _as_float(value: object) -> float:
    """Coerce a timings entry to float; 0.0 for anything non-numeric.

    The timings dict is heterogeneous — ``layer_init`` is a nested dict.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def record_render(
    tracker: RenderTracker,
    timings: Mapping[str, object],
    *,
    output_bytes: int | None = None,
    game_version: str = "",
    game_type: str = "",
) -> None:
    """Record a completed render and flip ``tracker`` to ``success``.

    Call this as soon as the worker future resolves — **before** attempting
    delivery. Everything observed here describes work that already happened,
    so it must not be lost when a later Discord upload fails or hangs.

    Reads exactly the keys the cog's ``[TIMING]`` log block already reads.
    Does not increment ``wows_renders_total`` — :func:`finish_render` owns
    that, so every path counts exactly once, and delivery may still downgrade
    the outcome to ``oversize`` or ``upload_failed`` afterwards.
    """
    tracker.outcome = OUTCOME_SUCCESS
    command = tracker.command

    for phase in _PHASES:
        seconds = _as_float(timings.get(phase))
        if seconds > 0:
            RENDER_PHASE_SECONDS.labels(command=command, phase=phase).observe(seconds)

    frames = _as_float(timings.get("_frames"))
    if frames > 0:
        RENDER_FRAMES_TOTAL.labels(command=command).inc(frames)

    if output_bytes is not None and output_bytes > 0:
        RENDER_OUTPUT_BYTES.labels(command=command).observe(output_bytes)

    peak_rss = _as_float(timings.get("_peak_rss_bytes"))
    if peak_rss > 0:
        RENDER_WORKER_PEAK_RSS_BYTES.labels(command=command).observe(peak_rss)

    layer_init = timings.get("layer_init")
    if isinstance(layer_init, dict):
        for layer_name, layer_seconds in layer_init.items():
            value = _as_float(layer_seconds)
            if value > 0:
                RENDER_LAYER_INIT_SECONDS.labels(layer=str(layer_name)).observe(value)

    if game_version:
        RENDERS_BY_VERSION_TOTAL.labels(
            game_version=game_version, game_type=game_type or "Unknown",
        ).inc()


def record_delivery(
    tracker: RenderTracker,
    *,
    elapsed: float,
    upload_seconds: float | None = None,
) -> None:
    """Record the delivery leg once the video has actually reached the user.

    ``elapsed`` must be measured *after* the upload so end-to-end means
    dispatch-to-delivered. Measuring it before makes the derived queue-wait
    panel negative, since that panel subtracts the phase sum and the phases
    include upload.

    ``upload_seconds`` is None where no real transfer happened: batch mode
    delivers via a separate follow-up message, and the oversize path only
    posts a text error, which would otherwise pollute the upload histogram
    with sub-second samples.
    """
    if upload_seconds is not None and upload_seconds > 0:
        RENDER_PHASE_SECONDS.labels(command=tracker.command, phase="upload").observe(upload_seconds)
    RENDER_E2E_SECONDS.labels(command=tracker.command, preset=tracker.preset).observe(elapsed)


# ---------------------------------------------------------------------------
# Standalone recorders
# ---------------------------------------------------------------------------


def record_pool_rebuild() -> None:
    POOL_REBUILDS_TOTAL.inc()


def record_cache_populated(count: int) -> None:
    if count > 0:
        GAMEDATA_CACHE_POPULATED_TOTAL.inc(count)


def observe_loop_lag(seconds: float) -> None:
    EVENT_LOOP_LAG_SECONDS.observe(max(0.0, seconds))
