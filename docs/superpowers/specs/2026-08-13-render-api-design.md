# HTTP Render API + Cloudflare Tunnel — Design

**Status:** Implemented
**Date:** 2026-08-13

## Goal

Make the render pipeline callable over HTTP so replays can be rendered from
tooling other than Discord, reachable from anywhere through a Cloudflare
Tunnel, authenticated by a single bearer token held by the operator.

## Motivation

Rendering has only ever been reachable through Discord slash commands. That
couples every consumer to a Discord client, an interaction token, and
Discord's 25 MB attachment ceiling. An HTTP endpoint makes the renderer usable
from scripts and other services, and — because the tunnel terminates at
Cloudflare — needs no inbound port on the host.

## The constraint that shapes the design

Cloudflare's edge aborts a proxied request that has not produced its first
response byte in roughly 100 seconds (error 524) on non-Enterprise plans.
Renders take 30 s to 5 min; production runs `RENDER_TIMEOUT=300`. A
synchronous `POST replay → receive mp4` endpoint would therefore fail on most
real replays.

So the API is **asynchronous**: submit a job, poll its state, download the
artifact. Every response returns promptly; only the artifact download moves
bulk bytes, and transfer duration is unbounded — the limit is
time-to-first-byte, not total time.

## Architecture

One aiohttp server inside the existing bot process, started in
`bot.main.setup_hook` the same way the Prometheus endpoint is, sharing **one**
`ProcessPoolExecutor` with the Discord cog.

```
                    ┌───────────────── bot process ─────────────────┐
Cloudflare edge     │                                               │
  │                 │  RenderCog ──┐                                │
  ▼                 │              ├──► RenderService ──► pool ──► worker procs
cloudflared ──►:8080│  api.py ──► jobs.py                           │
  (compose net)     │              └──► JobRegistry (in memory)     │
                    └───────────────────────────────────────────────┘
```

**Why one pool.** `MAX_WORKERS` is sized against the host's 4 vCPU / 4.5 GB
cap, and this box has been OOM-killed before. A second pool for the API would
oversubscribe both. Sharing one means API and Discord renders queue against
each other, which is the intended behaviour, and keeps the
`wows_renders_in_flight` gauge meaningful.

**Modules.**

| File | Responsibility |
|---|---|
| `bot/render_service.py` | Owns the pool, the pool lock, the progress-queue `Manager`, and rebuild-on-broken. Extracted from `RenderCog`; Discord-free so the API can use it. |
| `bot/jobs.py` | `Job`, `JobRegistry`, the per-job runner, the TTL sweeper. No HTTP, no Discord — the lifecycle is testable on its own. |
| `bot/api.py` | aiohttp app factory, auth and error middleware, multipart intake, validation, download handler. |
| `bot/worker.py` | Gains `render_stats()` (parse → extract → draw PNG in the worker) and `StatsUnavailableError`. Also now hosts `KNOWN_FLAGS`/`parse_flags`, so the API can validate flags without importing discord. |

## API contract

Auth on every route except `/healthz`: `Authorization: Bearer <API_TOKEN>`,
compared with `hmac.compare_digest`. Without `API_TOKEN` the server does not
start at all — there is no unauthenticated mode.

| Endpoint | Behaviour |
|---|---|
| `POST /v1/jobs` | multipart: `replay`, optional `replay_b`, fields `type`/`preset`/`theme`/`flags`/`speed`/`fps`/`layout` → `202 {"job_id"}` |
| `GET /v1/jobs/{id}` | `state` (`queued`/`running`/`done`/`failed`), `progress`, `status`, `error`, `result` metadata when done |
| `GET /v1/jobs/{id}/result` | the mp4/png, with explicit `Content-Type` and `Content-Disposition` |
| `GET /healthz` | unauthenticated liveness |

Job types: `render` (one replay → mp4), `render_dual` (two paired replays →
merged mp4), `stats` (one replay → post-battle board PNG).

Errors: `400` validation · `401` auth · `404` unknown job · `409` not ready or
failed · `413` over `MAX_UPLOAD_MB` · `429` over `API_MAX_PENDING` (with
`Retry-After`) · `500` generic message, details logged only.

**Inapplicable options are rejected, not ignored.** `preset` on a `stats` job,
or `layout` on a video job, returns `400`. Silently dropping them would let a
caller believe they got something they did not ask for.

**Bounds** on `speed` (1–100) and `fps` (1–60) are fail-fast courtesies;
`RENDER_TIMEOUT` remains the actual protection against a pathological request.

## Job lifecycle

1. Handler rejects early if `API_MAX_PENDING` is already reached, before
   reading the body.
2. Body streams to a per-job temp directory, counting bytes per part.
3. `JobRegistry.create` re-checks the pending cap — reading the upload awaits,
   so two requests can both pass the early check. This is the authoritative one.
4. A runner task submits to the pool and polls the worker's progress queue
   every 2 s, mapping `("status", str)` and `(current, total)` messages onto
   the job's `status`/`progress`.
5. `RENDER_TIMEOUT` is enforced by a deadline; on expiry the future is
   cancelled and the job fails.
6. On success, metrics are recorded the moment the future resolves and
   delivery afterwards — the ordering the Discord path already relies on.
   Outcomes (`success`/`timeout`/`worker_crash`/`error`) mirror the cog, under
   new `command` labels `api_render`, `api_dual`, `api_stats`.
7. Finished artifacts are swept `API_RESULT_TTL` seconds later. Failed jobs
   release their temp directory immediately but keep the record so the client
   can still read the error.

## Deployment

A `cloudflared` sidecar in `docker-compose.yml` runs a remotely-managed tunnel
(`tunnel run --token $CLOUDFLARE_TUNNEL_TOKEN`); the hostname → service
mapping lives in the Cloudflare dashboard, pointing at `http://bot:8080` over
the compose network. The API port uses `expose:`, never `ports:`, so the
tunnel and the token are the only ingress.

## Accepted trade-offs

- **In-memory registry.** A restart loses queued jobs and undownloaded
  results. For a single-operator API that is cheaper than adding a datastore,
  and the TTL sweep bounds disk use.
- **API availability is coupled to the bot process.** A Discord-motivated
  restart also restarts the API. The alternative — a separate container —
  costs a second pool or cross-container pool coordination.
- **No graceful shutdown.** Job state is in memory and expendable by design;
  SIGTERM tearing down the loop is sufficient.
- **Progress for stats jobs has no percentage** — there is no frame loop to
  count. The `status` string carries the phase instead.

## Rejected alternatives

- **Separate API container with its own pool** — clean isolation, but two
  pools competing for 4 vCPUs and a 4.5 GB cap on a host with an OOM history,
  plus duplicated gamedata-cache coordination.
- **Redis/broker-backed queue** — the correct answer at multi-host scale;
  pure overhead for one operator on one VPS.
- **Synchronous render endpoint** — impossible within the edge's TTFB limit
  for anything but the shortest replays.
