# tests/test_api_auth.py
"""Bearer-token auth on the render API.

The token is the only thing between the public Cloudflare tunnel and the
render pool, so every route except /healthz must reject anything that is not
an exact match.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from tests.api_helpers import AUTH, TOKEN, form, run  # noqa: E402


def test_healthz_needs_no_auth():
    async def scenario(client, _registry):
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"
    run(scenario)


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": ""},
    {"Authorization": TOKEN},                      # missing "Bearer " prefix
    {"Authorization": "Bearer"},
    {"Authorization": "Basic " + TOKEN},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": "Bearer " + TOKEN[:-1]},     # one char short
    {"Authorization": "Bearer " + TOKEN + "x"},    # one char long
])
def test_bad_credentials_are_rejected_on_every_route(headers):
    async def scenario(client, _registry):
        for method, path in (
            ("get", "/v1/jobs/whatever"),
            ("get", "/v1/jobs/whatever/result"),
            ("post", "/v1/jobs"),
        ):
            resp = await getattr(client, method)(path, headers=headers)
            assert resp.status == 401, f"{method} {path} with {headers}"
            body = await resp.json()
            assert body["error"] == "unauthorized"
            # Never echo the expected credential back to an unauthorized caller.
            assert TOKEN not in str(body)
    run(scenario)


def test_valid_token_is_accepted():
    async def scenario(client, _registry):
        resp = await client.post("/v1/jobs", data=form(), headers=AUTH)
        assert resp.status == 202
    run(scenario)


def test_unknown_job_is_a_404_not_a_401_leak():
    """An authorized caller asking for a missing job gets 404; the distinction
    must not depend on whether the job exists (no enumeration signal)."""
    async def scenario(client, _registry):
        resp = await client.get("/v1/jobs/deadbeef", headers=AUTH)
        assert resp.status == 404
    run(scenario)
