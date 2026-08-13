# tests/api_helpers.py
"""Shared scaffolding for the HTTP API tests. Not a test module.

The API is exercised against a real JobRegistry wired to a stub render
service, so requests flow through validation, job creation, the runner and
the download handler — everything except the actual render.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from bot.api import create_app
from bot.config import BotConfig
from bot.jobs import JobRegistry
from bot.worker import RenderResult

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FAKE_OUTPUT = b"fake-render-output" * 8


def config(**overrides) -> BotConfig:
    base = {
        "discord_token": "x",
        "api_token": TOKEN,
        "render_timeout": 30,
        "api_max_pending": 4,
        "api_result_ttl": 3600,
        "max_upload_mb": 5,
    }
    base.update(overrides)
    return BotConfig(**base)


class StubService:
    """RenderService stand-in.

    Writes the output file the handler asked for, located by extension —
    render_dual_replay takes two replays, so the output is not at a fixed
    positional index across job types.
    """

    def __init__(self, *, exc=None, never=False, messages=()):
        self._exc = exc
        self._never = never
        self._messages = messages
        self.calls: list = []

    async def submit(self, render_call):
        self.calls.append(render_call)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        if self._never:
            return None, fut
        if self._exc is not None:
            fut.set_exception(self._exc)
            return None, fut
        output = Path(output_arg(render_call))
        output.write_bytes(FAKE_OUTPUT)
        fut.set_result(RenderResult(
            output_path=str(output),
            duration=612.5,
            timings={"resolve": 0.1, "parse": 1.0, "render": 2.0, "encode": 0.5},
            game_version="15,7,0,13015811",
            num_players=24,
            game_type="RandomBattle",
            build_urls=[],
            chat_text="",
        ))
        return None, fut

    async def replace_broken_pool(self, pool):
        return pool

    def progress_queue(self):
        return _StubQueue(self._messages)


def output_arg(render_call) -> str:
    """The artifact path from a worker call, whatever the job type."""
    paths = [a for a in render_call.args
             if isinstance(a, str) and a.endswith((".mp4", ".png"))]
    assert len(paths) == 1, f"expected exactly one output path, got {paths}"
    return paths[0]


class _StubQueue:
    def __init__(self, messages=()):
        self._messages = list(messages)

    def empty(self) -> bool:
        return not self._messages

    def get_nowait(self):
        return self._messages.pop(0)


def form(*, replay=b"replay-bytes", filename="battle.wowsreplay", replay_b=None,
         b_filename="battle_b.wowsreplay", **fields) -> aiohttp.FormData:
    """Build the multipart body a client would send."""
    data = aiohttp.FormData()
    if replay is not None:
        data.add_field("replay", replay, filename=filename,
                       content_type="application/octet-stream")
    if replay_b is not None:
        data.add_field("replay_b", replay_b, filename=b_filename,
                       content_type="application/octet-stream")
    for key, value in fields.items():
        if value is not None:
            data.add_field(key, str(value))
    return data


def run(scenario, *, cfg=None, service=None):
    """Run ``scenario(client, registry)`` against a live test server.

    asyncio.run + aiohttp.test_utils rather than a pytest plugin — the suite
    installs no async plugin (see test_metrics.py).
    """
    cfg = cfg or config()
    service = service if service is not None else StubService()

    async def go():
        registry = JobRegistry(cfg, service)  # type: ignore[arg-type]
        app = create_app(cfg, service, registry=registry)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            return await scenario(client, registry)

    return asyncio.run(go())


async def wait_for_state(registry, job_id, state, timeout=5.0):
    """Poll the registry until ``job_id`` reaches ``state``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = registry.get(job_id)
        if job is not None and job.state == state:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached {state!r}")
