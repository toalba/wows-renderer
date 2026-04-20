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
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from multiprocessing import Manager
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import BotConfig
from bot.worker import render_replay

log = logging.getLogger(__name__)

BATCH_MAX_REPLAYS = 10
BATCH_COOLDOWN_SECONDS = 600
DISCORD_ATTACHMENT_LIMIT_MB = 25
DISCORD_EMBED_TOTAL_LIMIT = 5500  # conservative; discord's hard limit is 6000


def _batch_cooldown(interaction: discord.Interaction) -> app_commands.Cooldown | None:
    """Apply the 10-min cooldown only to authorized guilds; unauthorized users
    are rejected inside the command body, so their cooldown must not be burned.
    Returns None = no cooldown tracking for this invocation."""
    cog = interaction.client.get_cog("RenderCog")
    if cog is None:
        return None
    if interaction.guild_id in cog.config.authorized_guild_ids:  # type: ignore[attr-defined]
        return app_commands.Cooldown(1, BATCH_COOLDOWN_SECONDS)
    return None


@dataclass
class _BatchItem:
    index: int
    filename: str
    replay_path: Path
    output_path: Path


@dataclass
class _BatchResult:
    item: _BatchItem
    ok: bool = False
    error: str | None = None
    game_type: str = ""
    replay_duration: float = 0.0
    game_version: str = ""
    render_time: float = 0.0
    pool_died: bool = field(default=False)


class RenderCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: BotConfig) -> None:
        self.bot = bot
        self.config = config
        self._pool = self._make_pool()
        self._pool_lock = asyncio.Lock()
        self._manager = Manager()

    def _make_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=self.config.max_workers,
            max_tasks_per_child=self.config.render_max_tasks_per_child,
        )

    async def _replace_broken_pool(self, broken: ProcessPoolExecutor) -> ProcessPoolExecutor:
        async with self._pool_lock:
            if self._pool is broken:
                log.warning("ProcessPool broken, rebuilding (max_workers=%d, max_tasks_per_child=%d)",
                            self.config.max_workers, self.config.render_max_tasks_per_child)
                broken.shutdown(wait=False, cancel_futures=True)
                self._pool = self._make_pool()
            return self._pool

    async def _submit_render(self, render_call: functools.partial) -> tuple[ProcessPoolExecutor, asyncio.Future]:
        """Submit a render call to the pool, transparently rebuilding once if the pool is already broken."""
        loop = asyncio.get_running_loop()
        pool = self._pool
        try:
            future = loop.run_in_executor(pool, render_call)
        except BrokenProcessPool:
            pool = await self._replace_broken_pool(pool)
            future = loop.run_in_executor(pool, render_call)
        return pool, future

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

        log.info(
            "/render start: user=%s guild=%s replay=%s size=%.1fMB preset=%s",
            interaction.user.id, interaction.guild_id,
            replay.filename, replay.size / 1024 / 1024, preset_value,
        )
        await interaction.response.defer()

        # Temp files
        tmp_dir = tempfile.mkdtemp(prefix="wows_render_")
        safe_name = Path(replay.filename).name  # strip directory traversal
        replay_path = Path(tmp_dir) / safe_name
        output_path = Path(tmp_dir) / "minimap.mp4"

        pool = self._pool  # hoisted so the outer BrokenProcessPool handler can always rebuild
        try:
            # Download replay
            await replay.save(replay_path)
            await interaction.edit_original_response(content="Parsing replay...")
            t_start = time.monotonic()

            # Dispatch to process pool
            progress_queue = self._manager.Queue()
            cfg = self.config
            render_call = functools.partial(
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
            )
            pool, future = await self._submit_render(render_call)

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

            # Send video (timed as upload phase). Wrap in wait_for so a hung
            # Discord upload raises TimeoutError instead of silently eating
            # the handler — we saw a production case where this call just
            # never returned and the render vanished with no log trace.
            file_size = output_path.stat().st_size
            log.info(
                "Render done (%.1fs); uploading %.1fMB to Discord for %s",
                elapsed, file_size / 1024 / 1024, replay.filename,
            )
            t_upload_start = time.perf_counter()
            if file_size > DISCORD_ATTACHMENT_LIMIT_MB * 1024 * 1024:
                await asyncio.wait_for(
                    interaction.edit_original_response(
                        content=(
                            f"Video is too large for Discord "
                            f"({file_size / 1024 / 1024:.1f} MB > {DISCORD_ATTACHMENT_LIMIT_MB} MB limit)."
                        ),
                    ),
                    timeout=30,
                )
            else:
                await asyncio.wait_for(
                    interaction.edit_original_response(
                        content=(
                            f"Here's your minimap replay!\n"
                            f"{game_type} · {replay_mins}:{replay_secs:02d} · "
                            f"v{game_version} · "
                            f"Rendered in {elapsed:.1f}s · "
                            f"{file_size / 1024 / 1024:.1f} MB"
                        ),
                        attachments=[discord.File(str(output_path), filename="minimap.mp4")],
                    ),
                    timeout=120,
                )
            upload_time = time.perf_counter() - t_upload_start
            log.info("Upload complete in %.1fs for %s", upload_time, replay.filename)

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
        except BrokenProcessPool:
            log.exception("Render worker died for %s", replay.filename)
            await self._replace_broken_pool(pool)
            await interaction.edit_original_response(
                content="Render worker crashed (likely out of memory). Please try again.",
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

    async def _render_one_for_batch(
        self,
        item: _BatchItem,
        preset_value: str,
        timeout: float,
        semaphore: asyncio.Semaphore,
    ) -> _BatchResult:
        """Submit + await a single batch item, bounded by the semaphore so that
        at most ``max_workers`` submissions are in flight at once. This prevents
        a mid-batch pool rebuild from cancelling a queue full of already-submitted
        futures (cancel_futures=True would surface as CancelledError, which
        propagates past the per-item except Exception handler)."""
        async with semaphore:
            cfg = self.config
            render_call = functools.partial(
                render_replay,
                str(item.replay_path),
                str(item.output_path),
                str(cfg.gamedata_path),
                None,  # no progress queue in batch mode
                preset=preset_value,
                speed=cfg.render_speed,
                fps=cfg.render_fps,
                minimap_size=cfg.minimap_size,
                panel_width=cfg.panel_width,
            )
            try:
                _, future = await self._submit_render(render_call)
            except BrokenProcessPool:
                # _submit_render already tried one rebuild; if it still fails, give up on this item
                log.warning("Could not submit batch item #%d even after pool rebuild", item.index + 1)
                return _BatchResult(
                    item=item, ok=False,
                    error="worker pool unavailable",
                    pool_died=True,
                )

            try:
                _, replay_duration, timings, game_version, _num_players, game_type, _build_urls = (
                    await asyncio.wait_for(future, timeout=timeout)
                )
            except TimeoutError:
                future.cancel()
                return _BatchResult(item=item, ok=False, error=f"timed out after {int(timeout)}s")
            except BrokenProcessPool:
                log.warning("Worker died rendering batch item #%d (%s)", item.index + 1, item.filename)
                return _BatchResult(
                    item=item, ok=False,
                    error="worker crashed (likely OOM)",
                    pool_died=True,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("Batch render failed for item #%d (%s)", item.index + 1, item.filename)
                msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                return _BatchResult(item=item, ok=False, error=msg)

            # Actual worker time (excludes queue-wait inside the pool)
            worker_time = sum(
                float(timings.get(k, 0.0)) for k in ("parse", "setup", "render", "encode")
            )
            return _BatchResult(
                item=item,
                ok=True,
                game_type=game_type,
                replay_duration=replay_duration,
                game_version=game_version,
                render_time=worker_time,
            )

    @app_commands.command(
        name="render_batch",
        description=f"Render up to {BATCH_MAX_REPLAYS} replays in one batch (authorized servers only)",
    )
    @app_commands.describe(
        replay1="Replay 1 (required)",
        replay2="Replay 2",
        replay3="Replay 3",
        replay4="Replay 4",
        replay5="Replay 5",
        replay6="Replay 6",
        replay7="Replay 7",
        replay8="Replay 8",
        replay9="Replay 9",
        replay10="Replay 10",
        preset="Render preset (default: full)",
    )
    @app_commands.choices(preset=[
        app_commands.Choice(name="Full — all layers + both panels", value="full"),
        app_commands.Choice(name="Map — minimap only, no panels", value="map"),
        app_commands.Choice(name="Player data — minimap + killfeed/ribbons", value="playerdata"),
    ])
    @app_commands.checks.dynamic_cooldown(_batch_cooldown)
    async def render_batch(
        self,
        interaction: discord.Interaction,
        replay1: discord.Attachment,
        replay2: discord.Attachment | None = None,
        replay3: discord.Attachment | None = None,
        replay4: discord.Attachment | None = None,
        replay5: discord.Attachment | None = None,
        replay6: discord.Attachment | None = None,
        replay7: discord.Attachment | None = None,
        replay8: discord.Attachment | None = None,
        replay9: discord.Attachment | None = None,
        replay10: discord.Attachment | None = None,
        preset: app_commands.Choice[str] | None = None,
    ) -> None:
        # Guild authorization gate (cooldown factory already skipped tracking for unauthorized)
        if interaction.guild_id is None or interaction.guild_id not in self.config.authorized_guild_ids:
            await interaction.response.send_message(
                "This command isn't available in this server.", ephemeral=True,
            )
            return

        preset_value = preset.value if preset else "full"
        raw = [replay1, replay2, replay3, replay4, replay5,
               replay6, replay7, replay8, replay9, replay10]
        attachments = [a for a in raw if a is not None]

        # Validate each attachment
        max_bytes = self.config.max_upload_mb * 1024 * 1024
        valid: list[discord.Attachment] = []
        rejected: list[tuple[str, str]] = []
        for a in attachments:
            if not a.filename.endswith(".wowsreplay"):
                rejected.append((a.filename, "not a .wowsreplay file"))
            elif a.size > max_bytes:
                rejected.append((a.filename, f"{a.size / 1024 / 1024:.1f} MB > {self.config.max_upload_mb} MB"))
            else:
                valid.append(a)

        if not valid:
            details = "\n".join(f"• `{n}`: {r}" for n, r in rejected) or "(no attachments)"
            await interaction.response.send_message(
                f"No valid replays to render:\n{details}", ephemeral=True,
            )
            return

        log.info(
            "/render_batch start: user=%s guild=%s valid=%d rejected=%d preset=%s",
            interaction.user.id, interaction.guild_id,
            len(valid), len(rejected), preset_value,
        )
        await interaction.response.defer()

        batch_tmp = tempfile.mkdtemp(prefix="wows_batch_")
        cfg = self.config
        try:
            # Prepare per-item paths — both input and output prefixed with idx to avoid collisions
            items = [
                _BatchItem(
                    index=idx,
                    filename=Path(a.filename).name,
                    replay_path=Path(batch_tmp) / f"r{idx}_{Path(a.filename).name}",
                    output_path=Path(batch_tmp) / f"r{idx}_{Path(a.filename).stem}.mp4",
                )
                for idx, a in enumerate(valid)
            ]

            # Download all in parallel
            await interaction.edit_original_response(
                content=f"Downloading {len(items)} replay{'s' if len(items) > 1 else ''}...",
            )
            await asyncio.gather(
                *[a.save(item.replay_path) for a, item in zip(valid, items, strict=True)],
            )

            batch_start = time.monotonic()
            # Per-replay timeout accounts for queue-wait when len(items) > max_workers
            per_replay_timeout = float(cfg.render_timeout)
            # Semaphore caps in-flight submissions at max_workers; later items wait here,
            # NOT in the pool's internal queue (so a mid-batch pool rebuild doesn't cancel them).
            semaphore = asyncio.Semaphore(max(1, cfg.max_workers))
            tasks = [
                asyncio.create_task(
                    self._render_one_for_batch(item, preset_value, per_replay_timeout, semaphore),
                )
                for item in items
            ]

            await interaction.edit_original_response(
                content=f"Rendering batch (0/{len(items)})...",
            )

            # Stream results as they land
            results: list[_BatchResult] = []
            pool_died_seen = False
            for i, coro in enumerate(asyncio.as_completed(tasks)):
                result = await coro
                completed = i + 1
                results.append(result)
                pool_died_seen = pool_died_seen or result.pool_died

                # Stream the finished video (if it fits Discord's limit)
                if result.ok:
                    try:
                        size_bytes = result.item.output_path.stat().st_size
                        size_mb = size_bytes / 1024 / 1024
                        if size_mb > DISCORD_ATTACHMENT_LIMIT_MB:
                            result.ok = False
                            result.error = f"video too large ({size_mb:.1f} MB > {DISCORD_ATTACHMENT_LIMIT_MB} MB)"
                        else:
                            mins, secs = divmod(int(result.replay_duration), 60)
                            caption = (
                                f"**#{result.item.index + 1}** · {result.game_type} · "
                                f"{mins}:{secs:02d} · v{result.game_version} · "
                                f"Worker time {result.render_time:.1f}s · "
                                f"{size_mb:.1f} MB"
                            )
                            await interaction.followup.send(
                                content=caption,
                                file=discord.File(
                                    str(result.item.output_path),
                                    filename=f"{Path(result.item.filename).stem}.mp4",
                                ),
                            )
                    except Exception:  # noqa: BLE001
                        log.exception("Failed to upload batch result #%d", result.item.index + 1)
                        result.ok = False
                        result.error = "upload to Discord failed"

                await interaction.edit_original_response(
                    content=f"Rendering batch ({completed}/{len(items)})...",
                )

            # If any render surfaced a dead pool, rebuild for the next batch.
            # (Self-healing also happens on the next /render submit, but doing it eagerly
            # shrinks the window where an in-flight /render could see the dead pool.)
            if pool_died_seen:
                await self._replace_broken_pool(self._pool)

            batch_elapsed = time.monotonic() - batch_start
            ok_count = sum(1 for r in results if r.ok)
            embed = self._build_batch_summary_embed(
                results, rejected, ok_count, batch_elapsed, preset_value,
            )
            await interaction.edit_original_response(content=None, embed=embed)

            log.info(
                "[BATCH] user=%s guild=%s total=%d ok=%d skipped=%d time=%.1fs preset=%s",
                interaction.user.id, interaction.guild_id,
                len(items), ok_count, len(rejected), batch_elapsed, preset_value,
            )
        except Exception:  # noqa: BLE001
            log.exception("Batch render failed (user=%s guild=%s)", interaction.user.id, interaction.guild_id)
            try:
                await interaction.edit_original_response(content="Batch render failed unexpectedly.", embed=None)
            except discord.HTTPException:
                pass
        finally:
            shutil.rmtree(batch_tmp, ignore_errors=True)

    def _build_batch_summary_embed(
        self,
        results: list[_BatchResult],
        rejected: list[tuple[str, str]],
        ok_count: int,
        batch_elapsed: float,
        preset_value: str,
    ) -> discord.Embed:
        """Build the final summary embed, defensively capped at DISCORD_EMBED_TOTAL_LIMIT chars
        so Discord doesn't 400 the edit for long filenames. If we overflow, truncate
        field contents and note the drop."""
        color = 0x2ecc71 if ok_count == len(results) else (0xf39c12 if ok_count > 0 else 0xe74c3c)
        title = f"Batch complete — {ok_count}/{len(results)} succeeded"
        desc = f"Total time: {batch_elapsed:.1f}s · preset: `{preset_value}`"
        embed = discord.Embed(title=title, description=desc, color=color)
        running = len(title) + len(desc)

        for r in sorted(results, key=lambda r: r.item.index):
            icon = "✅" if r.ok else "❌"
            header = f"{icon} #{r.item.index + 1} {r.item.filename}"[:256]
            if r.ok:
                mins, secs = divmod(int(r.replay_duration), 60)
                body = f"{r.game_type} · {mins}:{secs:02d} · worker {r.render_time:.1f}s"
            else:
                body = f"Failed: {r.error}"
            body = body[:1024]
            if running + len(header) + len(body) > DISCORD_EMBED_TOTAL_LIMIT:
                embed.add_field(
                    name="…",
                    value=f"(output truncated; {len(results) - len(embed.fields)} more items)",
                    inline=False,
                )
                break
            embed.add_field(name=header, value=body, inline=False)
            running += len(header) + len(body)

        if rejected and running + 40 < DISCORD_EMBED_TOTAL_LIMIT:
            rej_body = "\n".join(f"`{n}`: {r}" for n, r in rejected)[:1024]
            if running + 20 + len(rej_body) <= DISCORD_EMBED_TOTAL_LIMIT:
                embed.add_field(
                    name=f"⚠️ Skipped ({len(rejected)})", value=rej_body, inline=False,
                )
        return embed

    @render_batch.error
    async def render_batch_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            retry_min = error.retry_after / 60
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"Batch render is on cooldown — try again in {retry_min:.1f} min.",
                    ephemeral=True,
                )
        else:
            log.exception("Unhandled error in /render_batch", exc_info=error)
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong.", ephemeral=True)
