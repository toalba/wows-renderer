"""Bot configuration from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Short tokens are the footgun this guards: the API is reachable from the
# public internet through the Cloudflare tunnel, with no second factor.
MIN_API_TOKEN_LENGTH = 16


@dataclass(frozen=True)
class BotConfig:
    discord_token: str
    gamedata_path: Path = Path("wows-gamedata/data")
    gamedata_repo_path: Path = Path("wows-gamedata")
    cache_root: Path | None = None  # None = ~/.cache/wows-gamedata/
    max_upload_mb: int = 50
    max_workers: int = 2
    # None = no recycling. The pool always uses the "forkserver" start
    # method (see RenderCog._make_pool — a fork-safety fix for cairo, not
    # related to this setting), which forks from a clean single-threaded
    # helper rather than re-importing modules, so unlike the historical
    # "spawn" fallback, enabling recycling here does not reload the 15 MB
    # GameParams pickle or add the ~5-10s-per-worker cost that used to come
    # with it on ARM.
    render_max_tasks_per_child: int | None = None
    render_timeout: int = 120
    cooldown_seconds: int = 60
    render_speed: float = 20.0
    render_fps: int = 20
    minimap_size: int = 1080
    panel_width: int = 420
    authorized_guild_ids: frozenset[int] = frozenset()
    # Prometheus /metrics endpoint. Bound inside the container only — see the
    # `expose:` (not `ports:`) entry in docker-compose.yml.
    metrics_enabled: bool = True
    metrics_port: int = 9108
    # HTTP render API (bot/api.py). None disables the server entirely — the
    # token is the only thing standing between the Cloudflare tunnel and the
    # render pool, so there is no "run it unauthenticated" mode.
    api_token: str | None = None
    api_port: int = 8080
    # Queued + running jobs before POST /v1/jobs starts returning 429. Renders
    # are serialised by MAX_WORKERS anyway; this bounds the wait, the disk used
    # by pending uploads, and the blast radius of a runaway client.
    api_max_pending: int = 4
    # How long a finished job's artifact stays downloadable.
    api_result_ttl: int = 3600

    @classmethod
    def from_env(cls) -> BotConfig:
        load_dotenv()
        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            raise RuntimeError("DISCORD_TOKEN environment variable is required")
        cache_root_str = os.environ.get("GAMEDATA_CACHE_DIR")
        guild_ids_str = os.environ.get("AUTHORIZED_GUILD_IDS", "").strip()
        authorized_guild_ids = frozenset(
            int(s) for s in (part.strip() for part in guild_ids_str.split(",")) if s
        )
        # Empty string or "0" → None (no recycling, fast fork start method).
        max_tasks_raw = os.environ.get("RENDER_MAX_TASKS_PER_CHILD", "").strip()
        max_tasks_per_child = int(max_tasks_raw) if max_tasks_raw and max_tasks_raw != "0" else None
        metrics_enabled = os.environ.get("METRICS_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        # Empty/unset → None → API server never starts (same convention as
        # RENDER_MAX_TASKS_PER_CHILD above).
        api_token = os.environ.get("API_TOKEN", "").strip() or None
        if api_token is not None and len(api_token) < MIN_API_TOKEN_LENGTH:
            raise RuntimeError(
                f"API_TOKEN must be at least {MIN_API_TOKEN_LENGTH} characters — "
                "it is the only credential in front of a publicly tunnelled render "
                "endpoint. Generate one with `openssl rand -hex 32`.",
            )
        api_max_pending = int(os.environ.get("API_MAX_PENDING", "4"))
        if api_max_pending < 1:
            raise RuntimeError("API_MAX_PENDING must be >= 1")
        api_result_ttl = int(os.environ.get("API_RESULT_TTL", "3600"))
        if api_result_ttl < 60:
            raise RuntimeError("API_RESULT_TTL must be >= 60 seconds")
        return cls(
            discord_token=token,
            gamedata_path=Path(os.environ.get("GAMEDATA_PATH", "wows-gamedata/data")).resolve(),
            gamedata_repo_path=Path(os.environ.get("GAMEDATA_REPO_PATH", "wows-gamedata")).resolve(),
            cache_root=Path(cache_root_str).resolve() if cache_root_str else None,
            max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "50")),
            max_workers=int(os.environ.get("MAX_WORKERS", "2")),
            render_max_tasks_per_child=max_tasks_per_child,
            render_timeout=int(os.environ.get("RENDER_TIMEOUT", "120")),
            cooldown_seconds=int(os.environ.get("COOLDOWN_SECONDS", "60")),
            authorized_guild_ids=authorized_guild_ids,
            metrics_enabled=metrics_enabled,
            metrics_port=int(os.environ.get("METRICS_PORT", "9108")),
            api_token=api_token,
            api_port=int(os.environ.get("API_PORT", "8080")),
            api_max_pending=api_max_pending,
            api_result_ttl=api_result_ttl,
        )
