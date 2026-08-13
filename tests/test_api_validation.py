# tests/test_api_validation.py
"""Request validation for POST /v1/jobs.

Every bad request must be rejected before a job is created, so a malformed
call never occupies a pending slot or a worker.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

import aiohttp  # noqa: E402

from bot.api import _safe_output_stem  # noqa: E402
from tests.api_helpers import AUTH, StubService, config, form, run  # noqa: E402


def _post(**form_kwargs):
    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(**form_kwargs), headers=AUTH)
        body = await resp.json() if resp.content_type == "application/json" else {}
        return resp.status, body, registry.pending_count()
    return run(scenario)


def test_minimal_request_is_accepted():
    status, body, _ = _post()
    assert status == 202
    assert body["job_id"]


@pytest.mark.parametrize("kwargs", [
    {"type": "nope"},
    {"type": "RENDER"},                     # case-sensitive on purpose
    {"preset": "fancy"},
    {"theme": "neon"},
    {"filename": "battle.txt"},             # wrong extension
    {"filename": "battle.wowsreplay.exe"},
    {"speed": "abc"},
    {"speed": "0"},
    {"speed": "-5"},
    {"speed": "1000"},
    {"fps": "0"},
    {"fps": "120"},
    {"fps": "12.5"},                        # int field, not float
    {"type": "stats", "layout": "wide"},
    {"type": "render", "layout": "compact"},   # layout is stats-only
    {"type": "stats", "preset": "map"},        # preset is render-only
    {"type": "render_dual", "preset": "map"},  # dual has no preset
])
def test_invalid_requests_are_rejected_without_creating_a_job(kwargs):
    status, body, pending = _post(**kwargs)
    assert status == 400, f"{kwargs} → {status} {body}"
    assert body.get("error")
    assert pending == 0


def test_multipart_without_a_replay_part_is_rejected():
    async def scenario(client, registry):
        # content_type on a field is what makes aiohttp emit multipart at all;
        # without it FormData falls back to urlencoded.
        data = aiohttp.FormData()
        data.add_field("type", "render", content_type="text/plain")
        resp = await client.post("/v1/jobs", data=data, headers=AUTH)
        return resp.status, await resp.json(), registry.pending_count()

    status, body, pending = run(scenario)
    assert status == 400
    assert "replay" in body["error"]
    assert pending == 0


def test_non_multipart_body_is_rejected():
    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", json={"type": "render"}, headers=AUTH)
        return resp.status, await resp.json(), registry.pending_count()

    status, body, pending = run(scenario)
    assert status == 400
    assert "multipart" in body["error"]
    assert pending == 0


def test_dual_requires_two_replays():
    status, body, _ = _post(type="render_dual")
    assert status == 400
    assert "replay_b" in body["error"]


def test_dual_accepts_two_replays():
    status, body, _ = _post(type="render_dual", replay_b=b"second-replay")
    assert status == 202
    assert body["job_id"]


def test_second_replay_without_dual_is_rejected():
    """Silently ignoring it would render only one perspective while the caller
    believes they asked for a merge."""
    status, body, _ = _post(replay_b=b"second-replay")
    assert status == 400
    assert "replay_b" in body["error"]


def test_stats_and_dual_and_flags_are_accepted():
    for kwargs in (
        {"type": "stats"},
        {"type": "stats", "layout": "detailed"},
        {"type": "stats", "flags": "anonymize"},
        {"type": "render", "preset": "map", "theme": "brandon", "speed": "40", "fps": "30"},
        {"type": "render", "flags": "anonymize,bogus"},   # unknown flags dropped, like Discord
    ):
        status, body, _ = _post(**kwargs)
        assert status == 202, f"{kwargs} → {status} {body}"


def test_oversize_upload_is_rejected_with_413():
    """The per-part byte count is the real guard: aiohttp's client_max_size is
    not enforced on streamed multipart reads."""
    cfg = config(max_upload_mb=1)
    big = b"x" * (2 * 1024 * 1024)

    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(replay=big), headers=AUTH)
        return resp.status, registry.pending_count()

    status, pending = run(scenario, cfg=cfg)
    assert status == 413
    assert pending == 0


def test_oversized_text_field_is_rejected_without_buffering_it():
    """`client_max_size` does not apply to streamed multipart parts, so a text
    field is capped explicitly. Without that, a caller holding the token can
    push gigabytes into `theme` and OOM the process — which also kills the
    Discord bot, since they share it."""
    async def scenario(client, registry):
        data = aiohttp.FormData()
        data.add_field("replay", b"replay-bytes", filename="battle.wowsreplay",
                       content_type="application/octet-stream")
        data.add_field("theme", "x" * (512 * 1024))
        resp = await client.post("/v1/jobs", data=data, headers=AUTH)
        return resp.status, await resp.json(), registry.pending_count()

    status, body, pending = run(scenario)
    assert status == 413
    assert "theme" in body["error"]
    assert pending == 0


def test_colliding_replay_filenames_stay_distinct_on_disk():
    """`replay` named `b_x` and `replay_b` named `x` must not derive the same
    on-disk path — the merge would silently run one perspective twice."""
    # never=True so the job is still running and its uploads are still there;
    # a finished job drops them (see _drop_source_uploads).
    service = StubService(never=True)

    async def scenario(client, registry):
        resp = await client.post("/v1/jobs", data=form(
            type="render_dual",
            replay=b"perspective-A", filename="b_x.wowsreplay",
            replay_b=b"perspective-B", b_filename="x.wowsreplay",
        ), headers=AUTH)
        assert resp.status == 202, await resp.json()
        job = registry.get((await resp.json())["job_id"])
        uploads = sorted(p for p in job.tmp_dir.iterdir() if p.is_file())
        return [p.read_bytes() for p in uploads]

    contents = run(scenario, service=service)
    assert sorted(contents) == [b"perspective-A", b"perspective-B"]


@pytest.mark.parametrize(("upload_name", "expected"), [
    # Quotes and CRLF would otherwise be interpolated straight into the
    # Content-Disposition header. aiohttp's own client sanitizes them, so a
    # hand-rolled client is the threat this guards against.
    ('evil";\r\nX-Injected: yes\r\n.wowsreplay', "evil_X-Injected_yes"),
    ("../../etc/passwd.wowsreplay", "passwd"),   # Path().name drops the traversal outright
    ("wat\nnewline.wowsreplay", "wat_newline"),
    (".wowsreplay", "render"),            # nothing usable left → fallback
    ("", "render"),
    ("20260812_201635_PZSC105-Chung-King_50_Gold_harbor.wowsreplay",
     "20260812_201635_PZSC105-Chung-King_50_Gold_harbor"),  # real names survive intact
])
def test_output_stem_is_reduced_to_a_safe_character_set(upload_name, expected):
    assert _safe_output_stem(upload_name) == expected


def test_output_stem_is_length_bounded():
    assert len(_safe_output_stem("a" * 500 + ".wowsreplay")) == 80


def test_hostile_upload_name_yields_a_clean_artifact_name():
    async def scenario(client, registry):
        resp = await client.post(
            "/v1/jobs", data=form(filename="../../etc/passwd.wowsreplay"), headers=AUTH,
        )
        assert resp.status == 202, await resp.json()
        return registry.get((await resp.json())["job_id"]).output_filename

    name = run(scenario)
    assert name.endswith(".mp4")
    assert not any(c in name for c in '\r\n";/\\')


def test_upload_at_the_limit_is_accepted():
    cfg = config(max_upload_mb=1)
    ok = b"x" * (900 * 1024)

    async def scenario(client, _registry):
        resp = await client.post("/v1/jobs", data=form(replay=ok), headers=AUTH)
        return resp.status

    assert run(scenario, cfg=cfg) == 202
