"""Bot configuration from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class BotConfig:
    discord_token: str
    gamedata_path: Path = Path("wows-gamedata/data")
    gamedata_repo_path: Path = Path("wows-gamedata")
    cache_root: Path | None = None  # None = ~/.cache/wows-gamedata/
    max_upload_mb: int = 50
    max_workers: int = 2
    # None = no recycling, pool keeps fork start method (fast cold-start).
    # Any positive int silently forces the "spawn" start method per Python
    # docs, which re-imports all modules + reloads the 15 MB GameParams
    # pickle every worker lifecycle — ~5-10s of overhead per spawn on ARM.
    render_max_tasks_per_child: int | None = None
    render_timeout: int = 120
    cooldown_seconds: int = 60
    render_speed: float = 20.0
    render_fps: int = 20
    minimap_size: int = 1080
    panel_width: int = 420
    authorized_guild_ids: frozenset[int] = frozenset()

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
        )
