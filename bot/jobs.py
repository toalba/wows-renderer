"""Async render jobs for the HTTP API.

Cloudflare's edge drops a proxied request that takes ~100s to produce its
first byte, and renders routinely take longer than that, so the API cannot
answer a submission with the finished video. Instead every submission becomes
a Job here: the client polls for state and downloads the artifact afterwards.

Everything in this module is Discord-free and pool-agnostic — it talks to a
RenderService and to picklable worker callables, which is what makes the
lifecycle testable without an HTTP client or a real pool.

Deliberately in-memory: the registry dies with the process, so a restart
loses queued jobs and undownloaded results. For a single-operator API that
beats carrying a database, and the TTL sweep bounds disk use meanwhile.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import time
import uuid
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot import metrics
from bot.config import BotConfig
from bot.render_service import RenderService
from bot.worker import StatsUnavailableError

log = logging.getLogger(__name__)

# How often a running job's progress queue is drained. Matches the Discord
# cog's cadence; module level so tests can shorten it.
POLL_INTERVAL_S = 2.0
# How often finished jobs are checked against api_result_ttl.
SWEEP_INTERVAL_S = 60.0

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"

# Phases worth reporting back to the client. Mirrors metrics._PHASES; a stats
# job simply has fewer of them.
_REPORTED_PHASES = ("resolve", "parse", "setup", "render", "encode")

_GENERIC_ERROR = "render failed"


class PendingLimitError(Exception):
    """Too many jobs queued or running — the caller should retry later."""


@dataclass
class Job:
    """One render request and everything the API needs to answer about it."""

    id: str
    kind: str
    tmp_dir: Path
    output_path: Path
    output_filename: str
    content_type: str
    created_at: float
    state: str = JOB_QUEUED
    progress: int = 0
    status_text: str = "Queued"
    error: str | None = None
    finished_at: float | None = None
    result_meta: dict[str, Any] | None = None
    # Strong reference to the runner. Without it the task is only referenced
    # by the event loop and can be garbage-collected mid-render.
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def is_pending(self) -> bool:
        return self.state in (JOB_QUEUED, JOB_RUNNING)

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "type": self.kind,
            "state": self.state,
            "progress": self.progress,
            "status": self.status_text,
            "error": self.error,
            "result": None if self.result_meta is None else {
                **self.result_meta,
                "filename": self.output_filename,
                "content_type": self.content_type,
            },
        }


class JobRegistry:
    """Owns live jobs, their runner tasks, and their temp directories."""

    def __init__(self, config: BotConfig, service: RenderService) -> None:
        self.config = config
        self._service = service
        self._jobs: dict[str, Job] = {}
        self._sweeper: asyncio.Task | None = None

    # --- registry -------------------------------------------------------

    def pending_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job.is_pending)

    def create(
        self, *, kind: str, tmp_dir: Path, output_path: Path,
        output_filename: str, content_type: str,
    ) -> Job:
        """Register a job, or raise PendingLimitError if the queue is full.

        This is the authoritative backpressure check: the handler's early
        check happens before the upload is read, and reading it awaits, so
        two requests can both pass that check before either creates a job.
        """
        if self.pending_count() >= self.config.api_max_pending:
            raise PendingLimitError(
                f"{self.pending_count()} jobs already queued or running "
                f"(limit {self.config.api_max_pending})",
            )
        job = Job(
            id=uuid.uuid4().hex,
            kind=kind,
            tmp_dir=tmp_dir,
            output_path=output_path,
            output_filename=output_filename,
            content_type=content_type,
            created_at=time.monotonic(),
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def discard(self, job: Job) -> None:
        """Drop a job that was created but never started.

        Such a job would otherwise sit in ``queued`` forever: it counts as
        pending against ``api_max_pending``, but has no ``finished_at`` for
        the sweeper to expire, so its slot would never come back.
        """
        self._jobs.pop(job.id, None)

    def start(
        self, job: Job, *, render_call: functools.partial | Any,
        progress_queue: Any, command: str, preset: str,
    ) -> None:
        """Kick off the runner task for an already-created job."""
        job.task = asyncio.create_task(
            self._run(job, render_call, progress_queue, command, preset),
            name=f"render-job-{job.id}",
        )

    # --- runner ---------------------------------------------------------

    async def _run(
        self, job: Job, render_call: Any, progress_queue: Any,
        command: str, preset: str,
    ) -> None:
        """Submit, follow progress, finalize. Mirrors the Discord cog's loop
        and its metrics discipline: record the render as soon as the future
        resolves, record delivery afterwards, finish exactly once."""
        tracker = metrics.RenderTracker(command, preset)
        t_start = time.monotonic()
        job.state = JOB_RUNNING
        job.status_text = "Starting..."
        pool = None
        try:
            pool, future = await self._service.submit(render_call)
            await self._follow(job, future, progress_queue)
            result = await future

            size = job.output_path.stat().st_size
            metrics.record_render(
                tracker, result.timings, output_bytes=size,
                game_version=result.game_version, game_type=result.game_type,
            )
            metrics.record_delivery(tracker, elapsed=time.monotonic() - t_start)

            job.result_meta = {
                "size_bytes": size,
                "replay_duration_sec": round(result.duration, 1),
                "game_version": result.game_version,
                "game_type": result.game_type,
                "num_players": result.num_players,
                "timings": {
                    phase: round(float(result.timings[phase]), 3)
                    for phase in _REPORTED_PHASES
                    if phase in result.timings
                },
            }
            job.progress = 100
            job.status_text = "Done"
            job.state = JOB_DONE
            # The uploads are dead weight now, and the artifact is held for
            # api_result_ttl: keeping them would roughly triple the disk a
            # steady stream of jobs occupies, and leaves submitted replays
            # lying around longer than necessary.
            _drop_source_uploads(job)
            log.info(
                "api job %s (%s) done in %.1fs (%.1f MB)",
                job.id, job.kind, time.monotonic() - t_start, size / 1024 / 1024,
            )
        except asyncio.CancelledError:
            # Shutdown, not a render failure — leave the outcome as-is and let
            # the finally block release the temp files.
            job.state = JOB_FAILED
            job.error = "cancelled"
            raise
        except TimeoutError:
            tracker.outcome = metrics.OUTCOME_TIMEOUT
            job.error = f"render timed out after {self.config.render_timeout}s"
            job.state = JOB_FAILED
            log.warning("api job %s timed out", job.id)
        except BrokenProcessPool:
            tracker.outcome = metrics.OUTCOME_WORKER_CRASH
            job.error = "render worker crashed"
            job.state = JOB_FAILED
            log.exception("api job %s: worker crashed", job.id)
            if pool is not None:
                await self._service.replace_broken_pool(pool)
        except StatsUnavailableError as exc:
            # Caused by the submitted replay, not by us: say so verbatim.
            tracker.outcome = metrics.OUTCOME_ERROR
            job.error = str(exc)
            job.state = JOB_FAILED
            log.info("api job %s: %s", job.id, exc)
        except Exception:
            tracker.outcome = metrics.OUTCOME_ERROR
            job.error = _GENERIC_ERROR  # details stay in the log
            job.state = JOB_FAILED
            log.exception("api job %s failed", job.id)
        finally:
            metrics.finish_render(tracker)
            job.finished_at = time.monotonic()
            if job.state == JOB_FAILED:
                # Nothing to download — reclaim the upload now rather than at
                # TTL. The record itself stays so the client can read the error.
                shutil.rmtree(job.tmp_dir, ignore_errors=True)

    async def _follow(self, job: Job, future: asyncio.Future, progress_queue: Any) -> None:
        """Poll until the future resolves, or raise TimeoutError at the deadline."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.render_timeout
        while not future.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                future.cancel()
                raise TimeoutError
            # Drain before sleeping so an already-queued message is visible to
            # the next status poll instead of one interval later.
            _drain_progress(job, progress_queue)
            await asyncio.sleep(min(POLL_INTERVAL_S, remaining))
        _drain_progress(job, progress_queue)

    # --- expiry ---------------------------------------------------------

    async def sweep_once(self, now: float | None = None) -> int:
        """Drop finished jobs whose results have outlived api_result_ttl."""
        now = time.monotonic() if now is None else now
        expired = [
            job for job in self._jobs.values()
            if job.finished_at is not None
            and not job.is_pending
            and now - job.finished_at >= self.config.api_result_ttl
        ]
        for job in expired:
            shutil.rmtree(job.tmp_dir, ignore_errors=True)
            self._jobs.pop(job.id, None)
        if expired:
            log.info("api: swept %d expired job(s)", len(expired))
        return len(expired)

    async def sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            try:
                await self.sweep_once()
            except Exception:
                log.exception("api: result sweep failed")

    def start_sweeper(self) -> None:
        self._sweeper = asyncio.create_task(self.sweep_loop(), name="api-result-sweeper")

    async def close(self) -> None:
        """Cancel the sweeper and every running job, then drop all temp dirs."""
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None
        tasks = [job.task for job in self._jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job in self._jobs.values():
            shutil.rmtree(job.tmp_dir, ignore_errors=True)
        self._jobs.clear()


def _drop_source_uploads(job: Job) -> None:
    """Delete everything in the job's directory except the finished artifact."""
    try:
        for path in job.tmp_dir.iterdir():
            if path != job.output_path and path.is_file():
                path.unlink(missing_ok=True)
    except OSError:
        # Not worth failing a finished render over; the TTL sweep will get it.
        log.warning("api job %s: could not clean up uploads", job.id, exc_info=True)


def _drain_progress(job: Job, progress_queue: Any) -> None:
    """Apply pending worker progress messages to the job.

    Wire shapes (bot/worker.py): ``("status", str)`` and ``(current, total)``.
    """
    if progress_queue is None:
        return
    while not progress_queue.empty():
        try:
            msg = progress_queue.get_nowait()
        except Exception:  # queue.Empty, or a dead Manager during shutdown
            return
        if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "status":
            job.status_text = str(msg[1])
        elif isinstance(msg, tuple) and len(msg) == 2:
            current, total = msg
            if total:
                job.progress = int(current / total * 100)
                job.status_text = f"Rendering... {job.progress}%"
