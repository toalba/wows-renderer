# tests/test_job_registry.py
"""Job lifecycle, backpressure and result expiry for the HTTP API's registry.

Driven through asyncio.run rather than pytest-asyncio so the suite needs no
extra plugin (same convention as test_metrics.py). The render pool is stubbed:
these tests are about the runner's state machine and its metrics bookkeeping,
not about rendering.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("prometheus_client")

from prometheus_client import REGISTRY  # noqa: E402

from bot import jobs as jobs_mod  # noqa: E402
from bot.config import BotConfig  # noqa: E402
from bot.jobs import (  # noqa: E402
    JOB_DONE,
    JOB_FAILED,
    JOB_RUNNING,
    JobRegistry,
    PendingLimitError,
)
from bot.worker import RenderResult, StatsUnavailableError  # noqa: E402


def _config(**overrides) -> BotConfig:
    base = {
        "discord_token": "x",
        "api_token": "t" * 32,
        "render_timeout": 30,
        "api_max_pending": 4,
        "api_result_ttl": 3600,
    }
    base.update(overrides)
    return BotConfig(**base)


def _result(output_path: str) -> RenderResult:
    return RenderResult(
        output_path=output_path,
        duration=600.0,
        timings={"resolve": 0.1, "parse": 1.0, "render": 2.0, "encode": 0.5},
        game_version="15,7,0,13015811",
        num_players=24,
        game_type="RandomBattle",
        build_urls=[],
        chat_text="",
    )


class _StubQueue:
    """Stands in for a Manager queue: the runner only calls empty/get_nowait."""

    def __init__(self, messages=()):
        self._messages = list(messages)

    def empty(self) -> bool:
        return not self._messages

    def get_nowait(self):
        return self._messages.pop(0)


class _StubService:
    """Implements just the RenderService surface the registry consumes."""

    def __init__(self, *, result=None, exc=None, never=False, messages=()):
        # Public so a test can attach a result that references the job's own
        # output path, which only exists after registry.create().
        self.result = result
        self._exc = exc
        self._never = never
        self._messages = messages
        self.rebuilt = False
        self.future: asyncio.Future | None = None

    async def submit(self, render_call):
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        if self._never:
            pass  # left pending forever — exercises the deadline
        elif self._exc is not None:
            fut.set_exception(self._exc)
        else:
            fut.set_result(self.result)
        self.future = fut
        return None, fut

    async def replace_broken_pool(self, pool):
        self.rebuilt = True
        return pool

    def progress_queue(self):
        return _StubQueue(self._messages)


def _sample(command: str, outcome: str) -> float:
    value = REGISTRY.get_sample_value(
        "wows_renders_total", {"command": command, "preset": "full", "outcome": outcome},
    )
    return value or 0.0


def _new_job(registry: JobRegistry, *, kind: str = "render", suffix: str = ".mp4"):
    tmp_dir = Path(tempfile.mkdtemp(prefix="jobtest_"))
    output = tmp_dir / f"out{suffix}"
    return registry.create(
        kind=kind, tmp_dir=tmp_dir, output_path=output,
        output_filename=f"out{suffix}", content_type="video/mp4",
    )


def test_successful_job_reaches_done_with_result_metadata():
    async def go():
        service = _StubService(messages=[("status", "Parsing replay..."), (50, 100)])
        registry = JobRegistry(_config(), service)
        job = _new_job(registry)
        job.output_path.write_bytes(b"x" * 4096)
        service.result = _result(str(job.output_path))

        before = _sample("api_render", "success")
        registry.start(
            job, render_call=lambda: None, progress_queue=service.progress_queue(),
            command="api_render", preset="full",
        )
        await asyncio.wait_for(job.task, 5)

        assert job.state == JOB_DONE
        assert job.error is None
        assert job.progress == 100
        assert job.result_meta["size_bytes"] == 4096
        assert job.result_meta["game_version"] == "15,7,0,13015811"
        assert job.result_meta["game_type"] == "RandomBattle"
        assert _sample("api_render", "success") - before == 1
        await registry.close()

    asyncio.run(go())


def test_progress_messages_update_state_while_running():
    """Both wire shapes land: ("status", str) and (current, total)."""
    async def go():
        service = _StubService(never=True, messages=[("status", "Parsing replay..."), (25, 100)])
        registry = JobRegistry(_config(render_timeout=30), service)
        job = _new_job(registry)
        registry.start(
            job, render_call=lambda: None, progress_queue=service.progress_queue(),
            command="api_render", preset="full",
        )
        # One poll iteration is enough to drain the queue.
        await asyncio.sleep(0.05)
        assert job.state == JOB_RUNNING
        assert job.progress == 25
        assert "25%" in job.status_text
        await registry.close()

    asyncio.run(go())


def test_worker_exception_fails_the_job_with_a_generic_message():
    async def go():
        service = _StubService(exc=RuntimeError("cairo exploded: /secret/path"))
        registry = JobRegistry(_config(), service)
        job = _new_job(registry)
        before = _sample("api_render", "error")
        registry.start(
            job, render_call=lambda: None, progress_queue=service.progress_queue(),
            command="api_render", preset="full",
        )
        await asyncio.wait_for(job.task, 5)

        assert job.state == JOB_FAILED
        # Internal detail must not leak to the client.
        assert "cairo" not in job.error
        assert "/secret/path" not in job.error
        assert _sample("api_render", "error") - before == 1
        await registry.close()

    asyncio.run(go())


def test_stats_unavailable_surfaces_its_real_message():
    """The one user-caused failure worth explaining: no results packet."""
    async def go():
        service = _StubService(exc=StatsUnavailableError("replay carries no post-battle results"))
        registry = JobRegistry(_config(), service)
        job = _new_job(registry, kind="stats", suffix=".png")
        registry.start(
            job, render_call=lambda: None, progress_queue=service.progress_queue(),
            command="api_stats", preset="stats",
        )
        await asyncio.wait_for(job.task, 5)

        assert job.state == JOB_FAILED
        assert "no post-battle results" in job.error
        await registry.close()

    asyncio.run(go())


def test_timeout_cancels_the_future_and_fails_the_job():
    async def go():
        service = _StubService(never=True)
        registry = JobRegistry(_config(render_timeout=0), service)
        job = _new_job(registry)
        before = _sample("api_render", "timeout")
        registry.start(
            job, render_call=lambda: None, progress_queue=service.progress_queue(),
            command="api_render", preset="full",
        )
        await asyncio.wait_for(job.task, 5)

        assert job.state == JOB_FAILED
        assert "timed out" in job.error
        assert service.future.cancelled()
        assert _sample("api_render", "timeout") - before == 1
        await registry.close()

    asyncio.run(go())


def test_pending_limit_is_enforced_on_create():
    async def go():
        service = _StubService(never=True)
        registry = JobRegistry(_config(api_max_pending=2), service)
        for _ in range(2):
            job = _new_job(registry)
            registry.start(
                job, render_call=lambda: None, progress_queue=service.progress_queue(),
                command="api_render", preset="full",
            )
        assert registry.pending_count() == 2
        with pytest.raises(PendingLimitError):
            _new_job(registry)
        await registry.close()

    asyncio.run(go())


def test_successful_job_keeps_the_artifact_but_drops_the_uploads():
    """The artifact is held for api_result_ttl; the submitted replay is not."""
    async def go():
        service = _StubService()
        registry = JobRegistry(_config(), service)
        job = _new_job(registry)
        upload = job.tmp_dir / "battle.wowsreplay"
        upload.write_bytes(b"replay bytes")
        job.output_path.write_bytes(b"x" * 128)
        service.result = _result(str(job.output_path))

        registry.start(
            job, render_call=lambda: None, progress_queue=service.progress_queue(),
            command="api_render", preset="full",
        )
        await asyncio.wait_for(job.task, 5)

        assert job.state == JOB_DONE
        assert job.output_path.exists()
        assert not upload.exists()
        await registry.close()

    asyncio.run(go())


def test_failed_job_frees_its_tmpdir_immediately():
    async def go():
        service = _StubService(exc=RuntimeError("nope"))
        registry = JobRegistry(_config(), service)
        job = _new_job(registry)
        registry.start(
            job, render_call=lambda: None, progress_queue=service.progress_queue(),
            command="api_render", preset="full",
        )
        await asyncio.wait_for(job.task, 5)
        assert not job.tmp_dir.exists()
        # The record itself survives so the client can still read the error.
        assert registry.get(job.id) is job
        await registry.close()

    asyncio.run(go())


def test_sweep_removes_expired_results_only():
    async def go():
        service = _StubService(result=None)
        registry = JobRegistry(_config(api_result_ttl=600), service)

        done = _new_job(registry)
        done.output_path.write_bytes(b"x")
        done.state = JOB_DONE
        done.finished_at = 1000.0

        fresh = _new_job(registry)
        fresh.output_path.write_bytes(b"x")
        fresh.state = JOB_DONE
        fresh.finished_at = 1500.0

        running = _new_job(registry)
        running.state = JOB_RUNNING

        removed = await registry.sweep_once(now=1700.0)  # done expired, fresh not

        assert removed == 1
        assert registry.get(done.id) is None
        assert not done.tmp_dir.exists()
        assert registry.get(fresh.id) is fresh
        assert fresh.tmp_dir.exists()
        assert registry.get(running.id) is running
        await registry.close()

    asyncio.run(go())


def test_close_cancels_running_jobs_and_clears_tmpdirs():
    async def go():
        service = _StubService(never=True)
        registry = JobRegistry(_config(), service)
        job = _new_job(registry)
        registry.start(
            job, render_call=lambda: None, progress_queue=service.progress_queue(),
            command="api_render", preset="full",
        )
        await asyncio.sleep(0.02)
        tmp_dir = job.tmp_dir
        await registry.close()
        assert job.task.done()
        assert not tmp_dir.exists()

    asyncio.run(go())


def test_poll_interval_is_module_level_so_tests_can_shorten_it():
    """Guards the knob these tests rely on rather than sleeping 2s per poll."""
    assert isinstance(jobs_mod.POLL_INTERVAL_S, (int, float))
    assert isinstance(jobs_mod.SWEEP_INTERVAL_S, (int, float))
