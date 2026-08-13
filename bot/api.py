"""HTTP render API — the Discord bot's render pool, reachable over the network.

Served from the bot process (started in bot.main.setup_hook, like the metrics
endpoint) so renders queue against the same ProcessPoolExecutor as the slash
commands. Published only through a Cloudflare tunnel: the port is never bound
on the host, and a single bearer token guards every route but /healthz.

Submissions return immediately with a job id — see bot/jobs.py for why the
API cannot simply answer with the finished video.

Endpoints:
    POST /v1/jobs             multipart submit → 202 {"job_id"}
    GET  /v1/jobs/{id}        status/progress JSON
    GET  /v1/jobs/{id}/result the finished mp4/png
    GET  /healthz             unauthenticated liveness
"""
from __future__ import annotations

import functools
import hmac
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import BodyPartReader, web
from aiohttp.typedefs import Handler

from bot.config import BotConfig
from bot.jobs import (
    JOB_DONE,
    JOB_FAILED,
    JobRegistry,
    PendingLimitError,
)
from bot.render_service import RenderService
from bot.worker import PRESETS, parse_flags, render_dual_replay, render_replay, render_stats

log = logging.getLogger(__name__)

JOB_RENDER = "render"
JOB_DUAL = "render_dual"
JOB_STATS = "stats"
JOB_TYPES = (JOB_RENDER, JOB_DUAL, JOB_STATS)

# Bounds are a fail-fast courtesy, not the real protection: RENDER_TIMEOUT is
# what actually stops a pathological request from occupying a worker.
SPEED_MIN, SPEED_MAX = 1.0, 100.0
FPS_MIN, FPS_MAX = 1, 60

REPLAY_SUFFIX = ".wowsreplay"
UPLOAD_CHUNK = 64 * 1024
RETRY_AFTER_S = 30

# Every option field is a short token; the largest legitimate one is a preset
# name. Capped because `client_max_size` does not apply to streamed multipart
# parts, so `part.text()` would otherwise buffer an unbounded field into
# memory — and this process also runs the Discord bot.
MAX_FIELD_BYTES = 4096

# Uploads are stored under fixed names rather than the client's. Deriving both
# names from client input let a caller collide them (`replay` named `b_x` vs
# `replay_b` named `x`), which silently rendered one perspective twice.
UPLOAD_NAMES = {"replay": "replay.wowsreplay", "replay_b": "replay_b.wowsreplay"}

# The upload name still names the artifact, and that lands in a
# Content-Disposition header — so it is reduced to this character set first.
_UNSAFE_STEM_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM_LENGTH = 80

# Form-field slack on top of the two replay uploads.
_CLIENT_MAX_OVERHEAD_MB = 4


# Typed app keys: aiohttp warns on bare string keys, and these give the
# handlers checked access without casts.
CONFIG_KEY: web.AppKey[BotConfig] = web.AppKey("config")
SERVICE_KEY: web.AppKey[RenderService] = web.AppKey("service")
REGISTRY_KEY: web.AppKey[JobRegistry] = web.AppKey("registry")


class _BadRequestError(Exception):
    """Client error carrying the status and message to return."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _json_error(status: int, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def _too_many_jobs() -> web.Response:
    return web.json_response(
        {"error": "too many jobs queued or running"},
        status=429, headers={"Retry-After": str(RETRY_AFTER_S)},
    )


def create_app(
    config: BotConfig,
    service: RenderService,
    registry: JobRegistry | None = None,
) -> web.Application:
    """Build the API application.

    ``registry`` is injectable so tests can hold a reference to it; in
    production the app owns one.
    """
    if not config.api_token:
        raise RuntimeError("create_app requires config.api_token")
    api_token = config.api_token  # narrowed to str for the middleware closure

    jobs = registry if registry is not None else JobRegistry(config, service)

    @web.middleware
    async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path == "/healthz":
            return await handler(request)
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        supplied = header[len(prefix):] if header.startswith(prefix) else ""
        # compare_digest, not ==: keeps the comparison constant-time, and the
        # empty-string case still fails rather than short-circuiting.
        if not supplied or not hmac.compare_digest(
            supplied.encode("utf-8"), api_token.encode("utf-8"),
        ):
            return _json_error(401, "unauthorized")
        return await handler(request)

    @web.middleware
    async def error_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except _BadRequestError as exc:
            return _json_error(exc.status, exc.message)
        except web.HTTPRequestEntityTooLarge:
            # aiohttp's own client_max_size guard — answer in JSON like the rest.
            return _json_error(413, f"upload exceeds {config.max_upload_mb} MB")
        except web.HTTPException:
            raise
        except Exception:
            # Never hand internals to the client; the log has the traceback.
            log.exception("api: unhandled error on %s %s", request.method, request.path)
            return _json_error(500, "internal error")

    app = web.Application(
        middlewares=[error_middleware, auth_middleware],
        # Belt to the per-part suspenders in _read_submission: this bound is
        # not applied to streamed multipart reads.
        client_max_size=(2 * config.max_upload_mb + _CLIENT_MAX_OVERHEAD_MB) * 1024 * 1024,
    )
    app[CONFIG_KEY] = config
    app[SERVICE_KEY] = service
    app[REGISTRY_KEY] = jobs
    app.router.add_get("/healthz", _healthz)
    app.router.add_post("/v1/jobs", _submit_job)
    app.router.add_get("/v1/jobs/{job_id}", _job_status)
    app.router.add_get("/v1/jobs/{job_id}/result", _job_result)

    async def _on_startup(_app: web.Application) -> None:
        jobs.start_sweeper()

    async def _on_cleanup(_app: web.Application) -> None:
        await jobs.close()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def _healthz(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# --- submission ---------------------------------------------------------


async def _submit_job(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    registry = request.app[REGISTRY_KEY]

    # Cheap rejection before reading the body. registry.create re-checks,
    # because reading the upload awaits and two requests can both pass here.
    if registry.pending_count() >= config.api_max_pending:
        return _too_many_jobs()

    tmp_dir = Path(tempfile.mkdtemp(prefix="wows_api_"))
    job = None
    try:
        replays, upload_names, fields = await _read_submission(request, tmp_dir, config)
        spec = _build_job_spec(replays, upload_names, fields, tmp_dir, config)
        job = registry.create(
            kind=spec["kind"], tmp_dir=tmp_dir, output_path=spec["output_path"],
            output_filename=spec["output_filename"], content_type=spec["content_type"],
        )
        # Inside the try: a job that is registered but never started would sit
        # in `queued` forever — pending for the cap's purposes, but with no
        # finished_at for the sweeper to expire, so it would leak a slot until
        # the process restarts.
        progress_queue = request.app[SERVICE_KEY].progress_queue()
        render_call = spec["render_call"](progress_queue)
        registry.start(
            job, render_call=render_call, progress_queue=progress_queue,
            command=spec["command"], preset=spec["preset"],
        )
    except PendingLimitError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _too_many_jobs()
    except Exception:
        # Validation failure or worse — never leave the upload or a
        # half-registered job behind.
        if job is not None:
            registry.discard(job)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    log.info(
        "api job %s accepted: type=%s artifact=%s", job.id, spec["kind"],
        spec["output_filename"],
    )
    return web.json_response({"job_id": job.id}, status=202)


async def _read_submission(
    request: web.Request, tmp_dir: Path, config: BotConfig,
) -> tuple[dict[str, Path], dict[str, str], dict[str, str]]:
    """Stream the multipart body: replay files to disk, text fields to a dict.

    Every size limit is enforced here, by counting bytes as they stream. That
    is the only place it *can* happen: `client_max_size` is applied by
    Request.read()/.post(), not to parts pulled off request.multipart().

    Returns ``(paths, upload_names, fields)`` — the on-disk paths are fixed
    names, so the client's names are carried separately for labelling only.
    """
    if not (request.content_type or "").startswith("multipart/"):
        raise _BadRequestError("expected a multipart/form-data body")

    max_bytes = config.max_upload_mb * 1024 * 1024
    replays: dict[str, Path] = {}
    upload_names: dict[str, str] = {}
    fields: dict[str, str] = {}

    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if not isinstance(part, BodyPartReader):
            raise _BadRequestError("nested multipart bodies are not supported")
        if part.name in UPLOAD_NAMES:
            if part.name in replays:
                raise _BadRequestError(f"{part.name}: sent more than once")
            field = part.name
            path, name = await _save_replay_part(part, field, tmp_dir, max_bytes, config)
            replays[field], upload_names[field] = path, name
        elif part.name:
            fields[part.name] = await _read_text_field(part)

    return replays, upload_names, fields


async def _read_text_field(part: BodyPartReader) -> str:
    """Read a short option field, refusing anything oversized."""
    buf = bytearray()
    while True:
        chunk = await part.read_chunk(MAX_FIELD_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_FIELD_BYTES:
            raise _BadRequestError(
                f"{part.name}: field exceeds {MAX_FIELD_BYTES} bytes", status=413,
            )
    return buf.decode("utf-8", errors="replace").strip()


def _safe_output_stem(upload_name: str) -> str:
    """Reduce an upload's name to something safe to use as a filename.

    The result names the artifact and is interpolated into a
    Content-Disposition header, so quotes, CRLF and separators must not
    survive — a hand-rolled HTTP client is not obliged to sanitize them the
    way aiohttp's does.
    """
    stem = Path(upload_name).name.removesuffix(REPLAY_SUFFIX)
    stem = _UNSAFE_STEM_CHARS.sub("_", stem).strip("._-")
    return stem[:_MAX_STEM_LENGTH] or "render"


async def _save_replay_part(
    part: BodyPartReader, field: str, tmp_dir: Path, max_bytes: int, config: BotConfig,
) -> tuple[Path, str]:
    """Stream one replay upload to disk. Returns ``(path, client_name)``."""
    # Path(...).name strips any directory component a client tried to smuggle in.
    client_name = Path(part.filename or "").name
    if not client_name.endswith(REPLAY_SUFFIX):
        raise _BadRequestError(f"{field}: expected a {REPLAY_SUFFIX} file")

    # A server-chosen name, so two uploads in one request can never collide
    # however they are named.
    dest = tmp_dir / UPLOAD_NAMES[field]
    written = 0
    with dest.open("wb") as fh:
        while True:
            chunk = await part.read_chunk(UPLOAD_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise _BadRequestError(
                    f"{field}: exceeds the {config.max_upload_mb} MB limit", status=413,
                )
            fh.write(chunk)
    if written == 0:
        raise _BadRequestError(f"{field}: file is empty")
    return dest, client_name


def _build_job_spec(
    replays: dict[str, Path], upload_names: dict[str, str], fields: dict[str, str],
    tmp_dir: Path, config: BotConfig,
) -> dict[str, Any]:
    """Validate the request and describe the job it maps to.

    ``render_call`` is a one-argument factory taking the progress queue, so
    the queue is only created once the request is known to be valid.
    """
    kind = fields.get("type") or JOB_RENDER
    if kind not in JOB_TYPES:
        raise _BadRequestError(f"type: must be one of {', '.join(JOB_TYPES)}")

    if "replay" not in replays:
        raise _BadRequestError("replay: a .wowsreplay file is required")
    if kind == JOB_DUAL and "replay_b" not in replays:
        raise _BadRequestError("replay_b: required for a render_dual job")
    if kind != JOB_DUAL and "replay_b" in replays:
        raise _BadRequestError("replay_b: only valid for a render_dual job")

    theme = fields.get("theme") or "default"
    _require_theme(theme)
    flags = parse_flags(fields.get("flags"))

    # Reject options that do not apply, rather than silently ignoring them:
    # a caller who passes preset=map to a stats job has misunderstood
    # something, and a 202 would hide that.
    if kind == JOB_STATS:
        _reject_unsupported(fields, ("preset", "speed", "fps"), kind)
        layout = fields.get("layout") or "compact"
        _require_layout(layout)
        output_filename = f"{_safe_output_stem(upload_names['replay'])}_stats.png"
        output_path = tmp_dir / output_filename
        return {
            "kind": kind, "command": "api_stats", "preset": "stats",
            "output_path": output_path, "output_filename": output_filename,
            "content_type": "image/png",
            "render_call": lambda q: functools.partial(
                render_stats,
                str(replays["replay"]), str(output_path), str(config.gamedata_path), q,
                flags=flags, theme=theme, layout=layout,
            ),
        }

    _reject_unsupported(fields, ("layout",), kind)
    speed = _float_field(fields, "speed", config.render_speed, SPEED_MIN, SPEED_MAX)
    fps = _int_field(fields, "fps", config.render_fps, FPS_MIN, FPS_MAX)

    if kind == JOB_DUAL:
        _reject_unsupported(fields, ("preset",), kind)
        output_filename = "dual_render.mp4"
        output_path = tmp_dir / output_filename
        return {
            "kind": kind, "command": "api_dual", "preset": "dual",
            "output_path": output_path, "output_filename": output_filename,
            "content_type": "video/mp4",
            "render_call": lambda q: functools.partial(
                render_dual_replay,
                str(replays["replay"]), str(replays["replay_b"]), str(output_path),
                str(config.gamedata_path), q,
                speed=speed, fps=fps, minimap_size=config.minimap_size,
                panel_width=config.panel_width, flags=flags, theme=theme,
            ),
        }

    preset = fields.get("preset") or "full"
    if preset not in PRESETS:
        raise _BadRequestError(f"preset: must be one of {', '.join(PRESETS)}")
    output_filename = f"{_safe_output_stem(upload_names['replay'])}.mp4"
    output_path = tmp_dir / output_filename
    return {
        "kind": kind, "command": "api_render", "preset": preset,
        "output_path": output_path, "output_filename": output_filename,
        "content_type": "video/mp4",
        "render_call": lambda q: functools.partial(
            render_replay,
            str(replays["replay"]), str(output_path), str(config.gamedata_path), q,
            preset=preset, speed=speed, fps=fps, minimap_size=config.minimap_size,
            panel_width=config.panel_width, flags=flags, theme=theme,
        ),
    }


def _require_theme(theme: str) -> None:
    from renderer.themes import THEMES

    if theme not in THEMES:
        raise _BadRequestError(f"theme: must be one of {', '.join(sorted(THEMES))}")


def _require_layout(layout: str) -> None:
    from renderer.stats_board import LAYOUTS

    if layout not in LAYOUTS:
        raise _BadRequestError(f"layout: must be one of {', '.join(LAYOUTS)}")


def _reject_unsupported(fields: dict[str, str], names: tuple[str, ...], kind: str) -> None:
    for name in names:
        if fields.get(name):
            raise _BadRequestError(f"{name}: not supported for a {kind} job")


def _float_field(fields: dict[str, str], name: str, default: float, lo: float, hi: float) -> float:
    raw = fields.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise _BadRequestError(f"{name}: must be a number") from None
    if not lo <= value <= hi:
        raise _BadRequestError(f"{name}: must be between {lo} and {hi}")
    return value


def _int_field(fields: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = fields.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _BadRequestError(f"{name}: must be a whole number") from None
    if not lo <= value <= hi:
        raise _BadRequestError(f"{name}: must be between {lo} and {hi}")
    return value


# --- status / download --------------------------------------------------


def _lookup(request: web.Request):
    job = request.app[REGISTRY_KEY].get(request.match_info["job_id"])
    if job is None:
        # Also covers jobs whose results have expired out of the registry.
        raise _BadRequestError("unknown job", status=404)
    return job


async def _job_status(request: web.Request) -> web.Response:
    return web.json_response(_lookup(request).to_status_dict())


async def _job_result(request: web.Request) -> web.StreamResponse:
    job = _lookup(request)
    if job.state == JOB_FAILED:
        return _json_error(409, job.error or "render failed", state=job.state)
    if job.state != JOB_DONE:
        return _json_error(409, "result not ready", state=job.state)
    if not job.output_path.exists():
        log.error("api job %s is done but its artifact is gone", job.id)
        return _json_error(410, "result no longer available")
    return web.FileResponse(
        job.output_path,
        headers={
            # Explicit rather than guessed from the extension.
            "Content-Type": job.content_type,
            "Content-Disposition": f'attachment; filename="{job.output_filename}"',
        },
    )
