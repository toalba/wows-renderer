# tests/test_api_jobs.py
"""Job submission, status polling and artifact download over HTTP."""
from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from bot.jobs import JOB_DONE, JOB_FAILED  # noqa: E402
from bot.worker import StatsUnavailableError, render_dual_replay, render_replay, render_stats  # noqa: E402
from tests.api_helpers import (  # noqa: E402
    AUTH,
    FAKE_OUTPUT,
    StubService,
    config,
    form,
    run,
    wait_for_state,
)


def test_render_job_runs_to_done_and_downloads():
    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(), headers=AUTH)
        assert resp.status == 202
        job_id = (await resp.json())["job_id"]

        await wait_for_state(registry, job_id, JOB_DONE)

        status = await client.get(f"/v1/jobs/{job_id}", headers=AUTH)
        assert status.status == 200
        body = await status.json()
        assert body["state"] == "done"
        assert body["progress"] == 100
        assert body["error"] is None
        assert body["type"] == "render"
        assert body["result"]["size_bytes"] == len(FAKE_OUTPUT)
        assert body["result"]["game_type"] == "RandomBattle"
        assert body["result"]["filename"].endswith(".mp4")
        assert body["result"]["content_type"] == "video/mp4"
        assert body["result"]["timings"]["parse"] == 1.0

        result = await client.get(f"/v1/jobs/{job_id}/result", headers=AUTH)
        assert result.status == 200
        assert result.headers["Content-Type"] == "video/mp4"
        assert "attachment" in result.headers["Content-Disposition"]
        assert "battle.mp4" in result.headers["Content-Disposition"]
        assert await result.read() == FAKE_OUTPUT

    run(scenario)


@pytest.mark.parametrize(("job_type", "worker", "extra", "out_idx", "suffix", "ctype"), [
    # out_idx: where output_path sits positionally — dual takes two replays
    # first, so its worker signature is one argument wider.
    ("render", render_replay, {}, 1, ".mp4", "video/mp4"),
    ("render_dual", render_dual_replay, {"replay_b": b"second"}, 2, ".mp4", "video/mp4"),
    ("stats", render_stats, {}, 1, ".png", "image/png"),
])
def test_each_job_type_dispatches_its_worker(job_type, worker, extra, out_idx, suffix, ctype):
    """Pins the positional call shape each worker expects."""
    service = StubService()

    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(type=job_type, **extra), headers=AUTH)
        assert resp.status == 202
        job_id = (await resp.json())["job_id"]
        job = await wait_for_state(registry, job_id, JOB_DONE)

        call = service.calls[-1]
        assert call.func is worker
        assert call.args[out_idx].endswith(suffix)
        assert call.args[0].endswith(".wowsreplay")
        assert job.content_type == ctype
        assert job.output_filename.endswith(suffix)

    run(scenario, service=service)


def test_dual_passes_both_replays_and_no_preset():
    service = StubService()

    async def scenario(client, registry):
        resp = await client.post(
            "/v1/jobs", data=form(type="render_dual", replay_b=b"second"), headers=AUTH,
        )
        job_id = (await resp.json())["job_id"]
        await wait_for_state(registry, job_id, JOB_DONE)

        call = service.calls[-1]
        assert call.func is render_dual_replay
        assert len(call.args) == 5  # a, b, output, gamedata, queue
        assert call.args[0] != call.args[1]  # both replays, kept distinct on disk
        assert "preset" not in call.keywords

    run(scenario, service=service)


def test_render_forwards_validated_options_to_the_worker():
    service = StubService()

    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(
            preset="map", theme="brandon", speed="40", fps="30", flags="anonymize,nonsense",
        ), headers=AUTH)
        job_id = (await resp.json())["job_id"]
        await wait_for_state(registry, job_id, JOB_DONE)

        kwargs = service.calls[-1].keywords
        assert kwargs["preset"] == "map"
        assert kwargs["theme"] == "brandon"
        assert kwargs["speed"] == 40.0
        assert kwargs["fps"] == 30
        assert kwargs["flags"] == frozenset({"anonymize"})  # unknown token dropped

    run(scenario, service=service)


def test_status_reports_progress_while_running():
    service = StubService(never=True, messages=[("status", "Parsing replay..."), (30, 100)])

    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(), headers=AUTH)
        job_id = (await resp.json())["job_id"]

        await wait_for_state(registry, job_id, "running")
        body = await (await client.get(f"/v1/jobs/{job_id}", headers=AUTH)).json()
        assert body["state"] == "running"
        assert body["progress"] == 30
        assert "30%" in body["status"]
        assert body["result"] is None

        # Not downloadable yet.
        pending = await client.get(f"/v1/jobs/{job_id}/result", headers=AUTH)
        assert pending.status == 409
        assert (await pending.json())["state"] == "running"

    run(scenario, service=service)


def test_unknown_job_returns_404_for_status_and_result():
    async def scenario(client, _registry):
        assert (await client.get("/v1/jobs/nope", headers=AUTH)).status == 404
        assert (await client.get("/v1/jobs/nope/result", headers=AUTH)).status == 404

    run(scenario)


def test_failed_job_reports_a_generic_error_and_409_on_result():
    service = StubService(exc=RuntimeError("cairo blew up in /srv/secret"))

    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(), headers=AUTH)
        job_id = (await resp.json())["job_id"]
        await wait_for_state(registry, job_id, JOB_FAILED)

        body = await (await client.get(f"/v1/jobs/{job_id}", headers=AUTH)).json()
        assert body["state"] == "failed"
        assert body["error"] == "render failed"
        assert "cairo" not in str(body)
        assert "/srv/secret" not in str(body)

        result = await client.get(f"/v1/jobs/{job_id}/result", headers=AUTH)
        assert result.status == 409

    run(scenario, service=service)


def test_stats_without_results_packet_explains_itself():
    service = StubService(exc=StatsUnavailableError(
        "replay carries no post-battle results — the recording ended before the results packet",
    ))

    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(type="stats"), headers=AUTH)
        job_id = (await resp.json())["job_id"]
        await wait_for_state(registry, job_id, JOB_FAILED)
        body = await (await client.get(f"/v1/jobs/{job_id}", headers=AUTH)).json()
        assert "no post-battle results" in body["error"]

    run(scenario, service=service)


def test_a_failure_after_job_creation_does_not_leak_a_pending_slot():
    """A job registered but never started would stay `queued` forever: pending
    for the cap, but with no finished_at for the sweeper to expire. It must be
    discarded instead, or repeated 500s would exhaust API_MAX_PENDING until the
    process restarts."""
    service = StubService()
    # Fails after registry.create(), at the point the progress queue is built.
    service.progress_queue = lambda: (_ for _ in ()).throw(RuntimeError("manager is down"))

    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(), headers=AUTH)
        return resp.status, registry.pending_count(), len(registry._jobs)

    status, pending, tracked = run(scenario, service=service)
    assert status == 500
    assert pending == 0
    assert tracked == 0


def test_backpressure_returns_429_when_the_queue_is_full():
    cfg = config(api_max_pending=2)
    service = StubService(never=True)

    async def scenario(client, registry):
        for _ in range(2):
            assert (await client.post("/v1/jobs", data=form(), headers=AUTH)).status == 202
        assert registry.pending_count() == 2

        resp = await client.post("/v1/jobs", data=form(), headers=AUTH)
        assert resp.status == 429
        assert resp.headers.get("Retry-After")
        assert (await resp.json())["error"]

    run(scenario, cfg=cfg, service=service)
