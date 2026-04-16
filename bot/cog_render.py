"""Render cog — /render slash command with async worker dispatch."""
from __future__ import annotations

import asyncio
import functools
import logging
import queue
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
from bot.worker import render_replay

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
        safe_name = Path(replay.filename).name  # strip directory traversal
        replay_path = Path(tmp_dir) / safe_name
        output_path = Path(tmp_dir) / "minimap.mp4"

        try:
            # Download replay
            await replay.save(replay_path)
            await interaction.edit_original_response(content="Parsing replay...")
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
            last_msg = "Parsing replay..."
            deadline = asyncio.get_event_loop().time() + self.config.render_timeout
            while not future.done():
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    future.cancel()
                    raise TimeoutError
                await asyncio.sleep(min(2, remaining))
                # Drain queue
                new_msg = last_msg
                while not progress_queue.empty():
                    try:
                        msg = progress_queue.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(msg, tuple) and msg[0] == "status":
                        new_msg = msg[1]
                    else:
                        current, total = msg
                        pct = int(current / total * 100) if total else 0
                        new_msg = f"Rendering... {pct}%"
                if new_msg != last_msg:
                    last_msg = new_msg
                    await interaction.edit_original_response(content=new_msg)

            # Collect result (raises if worker crashed)
            _, replay_duration, timings, game_version, num_players, game_type, build_urls = await future
            elapsed = time.monotonic() - t_start

            # Format durations
            replay_mins, replay_secs = divmod(int(replay_duration), 60)

            # Send video (timed as upload phase)
            file_size = output_path.stat().st_size
            t_upload_start = time.perf_counter()
            if file_size > 25 * 1024 * 1024:
                await interaction.edit_original_response(
                    content=f"Video is too large for Discord ({file_size / 1024 / 1024:.1f} MB > 25 MB limit).",
                )
            else:
                await interaction.edit_original_response(
                    content=(
                        f"Here's your minimap replay!\n"
                        f"{game_type} · {replay_mins}:{replay_secs:02d} · "
                        f"v{game_version} · "
                        f"Rendered in {elapsed:.1f}s · "
                        f"{file_size / 1024 / 1024:.1f} MB"
                    ),
                    attachments=[discord.File(str(output_path), filename="minimap.mp4")],
                )
            upload_time = time.perf_counter() - t_upload_start

            # Send build links as follow-up embed
            if build_urls:
                try:
                    team0: list[str] = []
                    team1: list[str] = []
                    for name, ship, team, url in build_urls:
                        line = f"[{name}]({url}) — {ship}" if url else f"{name} — {ship}"
                        (team0 if team == 0 else team1).append(line)

                    embed = discord.Embed(title="Ship Builds", color=0x3498db)
                    if team0:
                        embed.add_field(name="Allies", value="\n".join(team0), inline=True)
                    if team1:
                        embed.add_field(name="Enemies", value="\n".join(team1), inline=True)
                    embed.set_footer(text="Click a name to view their build on WoWs ShipBuilder")
                    await interaction.followup.send(embed=embed)
                except Exception as e:
                    log.warning("Failed to send build embed: %s", e)

            # Log timing breakdown
            resolve_time = timings.get("resolve", 0)
            parse_time = timings.get("parse", 0)
            setup_time = timings.get("setup", 0)
            render_time = timings.get("render", 0)
            encode_time = timings.get("encode", 0)
            total_time = resolve_time + parse_time + render_time + encode_time + upload_time
            frames = int(timings.get("_frames", 0))

            # Layer init breakdown
            layer_init = timings.get("layer_init", {})
            layer_lines = ""
            if isinstance(layer_init, dict) and layer_init:
                sorted_layers = sorted(layer_init.items(), key=lambda x: -x[1])
                layer_lines = "\n  layer_init:"
                for name, t in sorted_layers:
                    layer_lines += f"\n    {name:.<30s} {t:.3f}s"

            log.info(
                "\n[TIMING] replay=%s players=%d duration=%.1fs"
                "\n  resolve: %.3fs"
                "\n  parse  : %.2fs"
                "\n  setup  : %.2fs (assets + layer init)%s"
                "\n  render : %.2fs"
                "\n  encode : %.2fs"
                "\n  upload : %.2fs"
                "\n  TOTAL  : %.2fs"
                "\n  video_size=%.1fMB frames=%d version=%s",
                replay.filename,
                num_players,
                replay_duration,
                resolve_time,
                parse_time,
                setup_time,
                layer_lines,
                render_time,
                encode_time,
                upload_time,
                total_time,
                file_size / 1024 / 1024,
                frames,
                game_version,
            )

        except TimeoutError:
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
