"""Render cog — /render slash command with async worker dispatch."""
from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Manager
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import BotConfig
from bot.worker import PRESETS, render_replay

log = logging.getLogger(__name__)


class RenderCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: BotConfig) -> None:
        self.bot = bot
        self.config = config
        self._pool = ProcessPoolExecutor(max_workers=config.max_workers)
        self._manager = Manager()

    async def cog_unload(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._manager.shutdown()

    @app_commands.command(name="render", description="Render a WoWS replay to minimap video")
    @app_commands.describe(
        replay="Upload a .wowsreplay file",
        preset="Render preset (default: full)",
    )
    @app_commands.choices(preset=[
        app_commands.Choice(name="Full — all layers + both panels", value="full"),
        app_commands.Choice(name="Map — minimap only, no panels", value="map"),
        app_commands.Choice(name="Player data — minimap + killfeed/ribbons", value="playerdata"),
    ])
    @app_commands.checks.cooldown(1, 60)
    async def render(
        self,
        interaction: discord.Interaction,
        replay: discord.Attachment,
        preset: app_commands.Choice[str] | None = None,
    ) -> None:
        preset_value = preset.value if preset else "full"

        # Validate
        if not replay.filename.endswith(".wowsreplay"):
            await interaction.response.send_message(
                "Please upload a `.wowsreplay` file.", ephemeral=True,
            )
            return

        max_bytes = self.config.max_upload_mb * 1024 * 1024
        if replay.size > max_bytes:
            await interaction.response.send_message(
                f"File too large (max {self.config.max_upload_mb} MB).", ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Temp files
        tmp_dir = tempfile.mkdtemp(prefix="wows_render_")
        replay_path = Path(tmp_dir) / replay.filename
        output_path = Path(tmp_dir) / "minimap.mp4"

        try:
            # Download replay
            await replay.save(replay_path)
            await interaction.edit_original_response(content="Rendering... 0%")
            t_start = time.monotonic()

            # Dispatch to process pool
            progress_queue = self._manager.Queue()
            loop = asyncio.get_running_loop()
            cfg = self.config
            future = loop.run_in_executor(
                self._pool,
                functools.partial(
                    render_replay,
                    str(replay_path),
                    str(output_path),
                    str(cfg.gamedata_path),
                    progress_queue,
                    preset=preset_value,
                    speed=cfg.render_speed,
                    fps=cfg.render_fps,
                    minimap_size=cfg.minimap_size,
                    panel_width=cfg.panel_width,
                ),
            )

            # Poll progress with timeout
            current = 0
            total = 1
            last_pct = -1
            deadline = asyncio.get_event_loop().time() + self.config.render_timeout
            while not future.done():
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    future.cancel()
                    raise asyncio.TimeoutError
                await asyncio.sleep(min(2, remaining))
                # Drain queue
                while not progress_queue.empty():
                    try:
                        current, total = progress_queue.get_nowait()
                    except Exception:
                        break
                pct = int(current / total * 100) if total else 0
                if pct != last_pct:
                    last_pct = pct
                    await interaction.edit_original_response(content=f"Rendering... {pct}%")

            # Collect result (raises if worker crashed)
            _, replay_duration = await future
            elapsed = time.monotonic() - t_start

            # Format durations
            replay_mins, replay_secs = divmod(int(replay_duration), 60)

            # Send video
            file_size = output_path.stat().st_size
            if file_size > 25 * 1024 * 1024:
                await interaction.edit_original_response(
                    content=f"Video is too large for Discord ({file_size / 1024 / 1024:.1f} MB > 25 MB limit).",
                )
            else:
                await interaction.edit_original_response(
                    content=(
                        f"Here's your minimap replay!\n"
                        f"Match duration: {replay_mins}:{replay_secs:02d} · "
                        f"Rendered in {elapsed:.1f}s · "
                        f"{file_size / 1024 / 1024:.1f} MB"
                    ),
                    attachments=[discord.File(str(output_path), filename="minimap.mp4")],
                )

        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content=f"Render timed out after {self.config.render_timeout}s.",
            )
        except Exception:
            log.exception("Render failed for %s", replay.filename)
            await interaction.edit_original_response(content="Render failed. Check the replay file and try again.")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @render.error
    async def render_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"Please wait {error.retry_after:.0f}s before rendering again.",
                ephemeral=True,
            )
        else:
            log.exception("Unhandled error in /render", exc_info=error)
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong.", ephemeral=True)
