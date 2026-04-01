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
    max_upload_mb: int = 50
    max_workers: int = 2
    render_timeout: int = 120
    cooldown_seconds: int = 60
    render_speed: float = 20.0
    render_fps: int = 20
    minimap_size: int = 1080
    panel_width: int = 420

    @classmethod
    def from_env(cls) -> BotConfig:
        load_dotenv()
        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            raise RuntimeError("DISCORD_TOKEN environment variable is required")
        return cls(
            discord_token=token,
            gamedata_path=Path(os.environ.get("GAMEDATA_PATH", "wows-gamedata/data")),
            max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "50")),
            max_workers=int(os.environ.get("MAX_WORKERS", "2")),
            render_timeout=int(os.environ.get("RENDER_TIMEOUT", "120")),
            cooldown_seconds=int(os.environ.get("COOLDOWN_SECONDS", "60")),
        )
