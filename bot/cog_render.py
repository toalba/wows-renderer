"""Render cog — /render slash command with async worker dispatch."""
from __future__ import annotations

import asyncio
import functools
import io
import logging
import queue
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from multiprocessing import Manager
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot import metrics
from bot.config import BotConfig
from bot.worker import render_dual_replay, render_replay

log = logging.getLogger(__name__)

BATCH_MAX_REPLAYS = 10
BATCH_COOLDOWN_SECONDS = 600
DISCORD_ATTACHMENT_LIMIT_MB = 25
DISCORD_EMBED_TOTAL_LIMIT = 5500  # conservative; discord's hard limit is 6000
RESULT_VIEW_TIMEOUT_S = 600  # 10 min — covers slow clickers, bounds memory


_FIELD_VALUE_LIMIT = 1024  # Discord hard limit
_MD_ESCAPE_TABLE = str.maketrans({
    # `_` inside `[link text]()` can't be backslash-escaped (Discord renders
    # the literal `\`), but underscores there still leak into italic parsing
    # for surrounding text. Swap for the visually-near-identical FULLWIDTH
    # LOW LINE (U+FF3F), which doesn't participate in markdown at all.
    "_": "＿",
    "*": "\\*", "~": "\\~", "|": "\\|",
    "`": "\\`", "[": "\\[", "]": "\\]",
})


def _md_escape(text: str) -> str:
    """Defuse Discord markdown in user-provided text. Without this, an
    underscore in one player name (`_c0ssack`) opens an italic span that
    bleeds through every following player until the next `_` closes it."""
    return text.translate(_MD_ESCAPE_TABLE)


def _chunk_lines(lines: list[str], limit: int = _FIELD_VALUE_LIMIT) -> list[str]:
    """Pack ``lines`` into ``limit``-char chunks joined by newlines.

    Each chunk fits inside a single Discord embed field value. A line that
    by itself exceeds ``limit`` is truncated so it still ships."""
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        add = len(line) + (1 if cur else 0)  # +1 for joining "\n"
        if cur_len + add > limit:
            chunks.append("\n".join(cur))
            cur, cur_len = [line], len(line)
        else:
            cur.append(line)
            cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


_EMBED_DESCRIPTION_LIMIT = 4096  # Discord hard limit


def _make_team_embed(label: str, lines: list[str], color: int) -> discord.Embed | None:
    """Build a single-team embed. Prefers the description (one contiguous
    block, no inter-field padding) and falls back to chunked fields only
    if joined lines exceed Discord's 4096-char description cap. Returns
    None for empty input or when even the fallback overflows the per-embed
    budget."""
    if not lines:
        return None
    embed = discord.Embed(title=label, color=color)
    embed.set_footer(text="Click a name to view their build on WoWs ShipBuilder")
    joined = "\n".join(lines)
    if len(joined) <= _EMBED_DESCRIPTION_LIMIT:
        embed.description = joined
    else:
        # Title already names the team — fields don't need a label, so use a
        # zero-width space to satisfy Discord's non-empty-name requirement.
        for chunk in _chunk_lines(lines):
            embed.add_field(name="​", value=chunk, inline=False)
    if len(embed) > DISCORD_EMBED_TOTAL_LIMIT or len(embed.fields) > 25:
        return None
    return embed


def _build_ship_builds_payload(
    build_urls: list[tuple[str, str, int, str | None]],
) -> tuple[list[discord.Embed], discord.File | None]:
    """Render the Show Builds payload.

    Returns ``(embeds, file)``. Normally produces one embed per team —
    each ShipBuilder URL is ~250 chars so a 24-player match (~5800 chars)
    doesn't fit in a single embed's 6000-char budget but splits cleanly
    per side. If a single team's embed still overflows (extreme outlier),
    falls back to a `.txt` file attachment and no embeds.
    """
    team0: list[str] = []
    team1: list[str] = []
    for name, ship, team, url in build_urls:
        safe_name = _md_escape(name)
        safe_ship = _md_escape(ship)
        line = f"[{safe_name}]({url}) — {safe_ship}" if url else f"{safe_name} — {safe_ship}"
        (team0 if team == 0 else team1).append(line)

    allies = _make_team_embed("Allies", team0, color=0x2ECC71)
    enemies = _make_team_embed("Enemies", team1, color=0xE74C3C)

    embeds = [e for e in (allies, enemies) if e is not None]
    # If both teams produced embeds OR neither team had data, no fallback needed.
    # Fallback fires only when a team had data but couldn't fit in one embed.
    overflowed = (team0 and allies is None) or (team1 and enemies is None)
    if overflowed:
        plain_lines = ["Allies:", *[f"  {ln}" for ln in team0], "", "Enemies:",
                       *[f"  {ln}" for ln in team1]]
        buf = io.BytesIO("\n".join(plain_lines).encode("utf-8"))
        return [], discord.File(buf, filename="ship_builds.txt")
    return embeds, None


class _RenderResultView(discord.ui.View):
    """Buttons attached to a render reply. Anyone in the channel can click;
    button state is held in process memory and dies on bot restart."""

    def __init__(
        self,
        *,
        build_urls: list[tuple[str, str, int, str | None]],
        chat_text: str,
        chat_filename: str,
    ) -> None:
        super().__init__(timeout=RESULT_VIEW_TIMEOUT_S)
        self._build_urls = build_urls
        self._chat_text = chat_text
        self._chat_filename = chat_filename
        # Set by the cog after the render message lands so on_timeout can
        # grey out the buttons. Without this Discord still shows them as
        # clickable but every click returns "interaction failed".
        self.message: discord.Message | None = None
        if not build_urls:
            self.remove_item(self.show_builds)
        if not chat_text:
            self.remove_item(self.download_chat)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Show Builds", style=discord.ButtonStyle.secondary)
    async def show_builds(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        button.disabled = True
        embeds, file = _build_ship_builds_payload(self._build_urls)
        if embeds:
            # First embed answers the click via the interaction; any extras
            # go via channel.send so Discord doesn't tether them to the
            # original render message with a "replying to" indicator.
            await interaction.response.send_message(embed=embeds[0])
            for extra in embeds[1:]:
                await interaction.channel.send(embed=extra)
        else:
            assert file is not None
            await interaction.response.send_message(file=file)
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Download Chat", style=discord.ButtonStyle.secondary)
    async def download_chat(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        button.disabled = True
        buf = io.BytesIO(self._chat_text.encode("utf-8"))
        file = discord.File(buf, filename=self._chat_filename)
        await interaction.response.send_message(file=file)
        await interaction.message.edit(view=self)

# Render behavior flags exposed via the slash commands' `flags` param.
KNOWN_FLAGS = frozenset({"anonymize"})

THEME_CHOICES = [
    app_commands.Choice(name="Default — green/red", value="default"),
    app_commands.Choice(name="Brandon — cyan/magenta", value="brandon"),
]


def _parse_flags(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated flags string into a frozenset, dropping unknowns."""
    if not raw:
        return frozenset()
    tokens = {t.strip().lower() for t in raw.split(",") if t.strip()}
    return frozenset(tokens & KNOWN_FLAGS)


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


def _dual_cooldown(interaction: discord.Interaction) -> app_commands.Cooldown | None:
    """10-min cooldown for /render_dual, applied on every guild. Unlike
    /render_batch, /render_dual is no longer gated to authorized guilds, so the
    cooldown must always apply to bound the cost of this heavy operation."""
    return app_commands.Cooldown(1, BATCH_COOLDOWN_SECONDS)


def _extract_replays_from_zip(
    zip_path: Path,
    dst_dir: Path,
    max_file_size: int,
    already: int,
    cap: int,
) -> tuple[list[tuple[str, Path]], list[tuple[str, str]]]:
    """Extract .wowsreplay files from a zip into ``dst_dir``.

    Returns (extracted, skipped). ``extracted`` is a list of (display_name,
    extracted_path). ``skipped`` is a list of (entry_name, reason) for zip
    entries that couldn't be included (non-wowsreplay, oversized, cap hit).
    """
    extracted: list[tuple[str, Path]] = []
    skipped: list[tuple[str, str]] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = Path(info.filename).name
                if not base.lower().endswith(".wowsreplay"):
                    continue
                if info.file_size > max_file_size:
                    skipped.append(
                        (base, f"{info.file_size / 1024 / 1024:.1f} MB exceeds limit"),
                    )
                    continue
                if already + len(extracted) >= cap:
                    skipped.append((base, f"over {cap}-replay cap"))
                    continue
                out_path = dst_dir / f"zip_{already + len(extracted)}_{base}"
                with zf.open(info) as src, open(out_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append((base, out_path))
    except zipfile.BadZipFile:
        skipped.append((zip_path.name, "not a valid zip file"))
    return extracted, skipped


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
    # Carried so the caller can set the terminal metrics outcome: an item that
    # rendered fine can still be downgraded there by an oversize or failed
    # upload, which is only known after the follow-up message is attempted.
    tracker: metrics.RenderTracker | None = None


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
                log.warning(
                    "ProcessPool broken, rebuilding (max_workers=%d, max_tasks_per_child=%s)",
                    self.config.max_workers,
                    self.config.render_max_tasks_per_child
                    if self.config.render_max_tasks_per_child is not None
                    else "unlimited",
                )
                broken.shutdown(wait=False, cancel_futures=True)
                self._pool = self._make_pool()
                metrics.record_pool_rebuild()
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
        metrics.track_pool_future(future)
        return pool, future

    async def cog_unload(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._manager.shutdown()

    @app_commands.command(name="render", description="Render a WoWS replay to minimap video")
    @app_commands.describe(
        replay="Upload a .wowsreplay file",
        preset="Render preset (default: full)",
        theme="Color theme (default: Default)",
        flags="Comma-separated flags. Available: anonymize",
    )
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="Full — all layers + both panels", value="full"),
            app_commands.Choice(name="Map — minimap only, no panels", value="map"),
            app_commands.Choice(name="Player data — minimap + killfeed/ribbons", value="playerdata"),
        ],
        theme=THEME_CHOICES,
    )
    @app_commands.checks.cooldown(1, 60)
    async def render(
        self,
        interaction: discord.Interaction,
        replay: discord.Attachment,
        preset: app_commands.Choice[str] | None = None,
        theme: app_commands.Choice[str] | None = None,
        flags: str | None = None,
    ) -> None:
        preset_value = preset.value if preset else "full"
        theme_value = theme.value if theme else "default"
        flag_set = _parse_flags(flags)

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
            "/render start: user=%s guild=%s replay=%s size=%.1fMB preset=%s theme=%s flags=%s",
            interaction.user.id, interaction.guild_id,
            replay.filename, replay.size / 1024 / 1024, preset_value, theme_value,
            sorted(flag_set) or "—",
        )
        await interaction.response.defer()

        # Temp files
        tmp_dir = tempfile.mkdtemp(prefix="wows_render_")
        safe_name = Path(replay.filename).name  # strip directory traversal
        replay_path = Path(tmp_dir) / safe_name
        output_name = Path(replay.filename).stem + ".mp4"
        output_path = Path(tmp_dir) / output_name

        pool = self._pool  # hoisted so the outer BrokenProcessPool handler can always rebuild
        tracked = metrics.RenderTracker("render", preset_value)
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
                flags=flag_set,
                theme=theme_value,
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
            (
                _, replay_duration, timings, game_version, num_players,
                game_type, build_urls, chat_text,
            ) = await future
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
            too_large = file_size > DISCORD_ATTACHMENT_LIMIT_MB * 1024 * 1024

            # Record the render BEFORE attempting delivery. Everything above
            # already happened; a Discord upload that later hangs must not
            # erase it. `finish_render` in the finally still owns the counter,
            # so the outcome can be downgraded below.
            metrics.record_render(
                tracked, timings,
                output_bytes=file_size,
                game_version=game_version,
                game_type=game_type,
            )

            t_upload_start = time.perf_counter()
            try:
                if too_large:
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
                    chat_filename = f"{Path(replay.filename).stem}_chat.txt"
                    result_view = _RenderResultView(
                        build_urls=build_urls, chat_text=chat_text, chat_filename=chat_filename,
                    )
                    edit_kwargs: dict = {
                        "content": (
                            f"Here's your minimap replay!\n"
                            f"{game_type} · {replay_mins}:{replay_secs:02d} · "
                            f"v{game_version} · "
                            f"Rendered in {elapsed:.1f}s · "
                            f"{file_size / 1024 / 1024:.1f} MB"
                        ),
                        "attachments": [discord.File(str(output_path), filename=output_name)],
                    }
                    if result_view.children:
                        edit_kwargs["view"] = result_view
                    msg = await asyncio.wait_for(
                        interaction.edit_original_response(**edit_kwargs),
                        timeout=120,
                    )
                    if result_view.children:
                        result_view.message = msg
            except Exception:
                # A hung upload raises TimeoutError — the same type as the
                # render deadline — so without this it would be counted as a
                # render timeout and blame the renderer for a Discord problem.
                tracked.outcome = metrics.OUTCOME_UPLOAD_FAILED
                log.exception("Discord delivery failed for %s (render itself succeeded)", replay.filename)
                try:
                    await interaction.edit_original_response(
                        content="Render finished, but sending it to Discord failed. Please try again.",
                    )
                except Exception:  # noqa: BLE001 - best effort; the interaction may be dead too
                    pass
                return
            upload_time = time.perf_counter() - t_upload_start
            log.info("Upload complete in %.1fs for %s", upload_time, replay.filename)

            # Measured after the upload so end-to-end genuinely means
            # dispatch-to-delivered. The oversize branch only posted a text
            # error, so it contributes no upload sample.
            metrics.record_delivery(
                tracked,
                elapsed=time.monotonic() - t_start,
                upload_seconds=None if too_large else upload_time,
            )
            if too_large:
                # The render worked; the user still got no video.
                tracked.outcome = metrics.OUTCOME_OVERSIZE

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
            tracked.outcome = metrics.OUTCOME_TIMEOUT
            await interaction.edit_original_response(
                content=f"Render timed out after {self.config.render_timeout}s.",
            )
        except BrokenProcessPool:
            tracked.outcome = metrics.OUTCOME_WORKER_CRASH
            log.exception("Render worker died for %s", replay.filename)
            await self._replace_broken_pool(pool)
            await interaction.edit_original_response(
                content="Render worker crashed. Please try again.",
            )
        except Exception:
            tracked.outcome = metrics.OUTCOME_ERROR
            log.exception("Render failed for %s", replay.filename)
            await interaction.edit_original_response(content="Render failed. Check the replay file and try again.")
        finally:
            metrics.finish_render(tracked)
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
        flag_set: frozenset[str] = frozenset(),
        theme_value: str = "default",
    ) -> _BatchResult:
        """Submit + await a single batch item, bounded by the semaphore so that
        at most ``max_workers`` submissions are in flight at once. This prevents
        a mid-batch pool rebuild from cancelling a queue full of already-submitted
        futures (cancel_futures=True would surface as CancelledError, which
        propagates past the per-item except Exception handler)."""
        async with semaphore:
            cfg = self.config
            tracked = metrics.RenderTracker("render_batch", preset_value)
            t_item_start = time.monotonic()
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
                flags=flag_set,
                theme=theme_value,
            )
            try:
                _, future = await self._submit_render(render_call)
            except BrokenProcessPool:
                # _submit_render already tried one rebuild; if it still fails, give up on this item
                log.warning("Could not submit batch item #%d even after pool rebuild", item.index + 1)
                tracked.outcome = metrics.OUTCOME_WORKER_CRASH
                metrics.finish_render(tracked)
                return _BatchResult(
                    item=item, ok=False,
                    error="worker pool unavailable",
                    pool_died=True,
                    tracker=tracked,
                )

            try:
                (
                    _, replay_duration, timings, game_version, _num_players,
                    game_type, _build_urls, _chat_text,
                ) = await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError:
                future.cancel()
                tracked.outcome = metrics.OUTCOME_TIMEOUT
                metrics.finish_render(tracked)
                return _BatchResult(
                    item=item, ok=False, error=f"timed out after {int(timeout)}s", tracker=tracked,
                )
            except BrokenProcessPool:
                log.warning("Worker died rendering batch item #%d (%s)", item.index + 1, item.filename)
                tracked.outcome = metrics.OUTCOME_WORKER_CRASH
                metrics.finish_render(tracked)
                return _BatchResult(
                    item=item, ok=False,
                    error="worker crashed",
                    pool_died=True,
                    tracker=tracked,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("Batch render failed for item #%d (%s)", item.index + 1, item.filename)
                tracked.outcome = metrics.OUTCOME_ERROR
                metrics.finish_render(tracked)
                msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                return _BatchResult(item=item, ok=False, error=msg, tracker=tracked)

            # Actual worker time (excludes queue-wait inside the pool)
            worker_time = sum(
                float(timings.get(k, 0.0)) for k in ("parse", "setup", "render", "encode")
            )
            # No output size here — batch delivers each video via a separate
            # follow-up message in the caller, which is also where an oversize
            # or failed upload downgrades the outcome, so it is deliberately
            # not finalised at this point.
            metrics.record_render(
                tracked, timings,
                game_version=game_version,
                game_type=game_type,
            )
            # No upload phase: this item's end-to-end ends when the render
            # does. Delivery happens later, in the caller's stream loop.
            metrics.record_delivery(tracked, elapsed=time.monotonic() - t_item_start)
            return _BatchResult(
                item=item,
                ok=True,
                game_type=game_type,
                replay_duration=replay_duration,
                game_version=game_version,
                render_time=worker_time,
                tracker=tracked,
            )

    @app_commands.command(
        name="render_batch",
        description=f"Render up to {BATCH_MAX_REPLAYS} replays in one batch (authorized servers only)",
    )
    @app_commands.describe(
        replay1="Replay or .zip (required)",
        replay2="Replay or .zip",
        replay3="Replay or .zip",
        replay4="Replay or .zip",
        replay5="Replay or .zip",
        replay6="Replay or .zip",
        replay7="Replay or .zip",
        replay8="Replay or .zip",
        replay9="Replay or .zip",
        replay10="Replay or .zip",
        preset="Render preset (default: full)",
        theme="Color theme (default: Default)",
        flags="Comma-separated flags. Available: anonymize",
    )
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="Full — all layers + both panels", value="full"),
            app_commands.Choice(name="Map — minimap only, no panels", value="map"),
            app_commands.Choice(name="Player data — minimap + killfeed/ribbons", value="playerdata"),
        ],
        theme=THEME_CHOICES,
    )
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
        theme: app_commands.Choice[str] | None = None,
        flags: str | None = None,
    ) -> None:
        # Guild authorization gate (cooldown factory already skipped tracking for unauthorized)
        if interaction.guild_id is None or interaction.guild_id not in self.config.authorized_guild_ids:
            await interaction.response.send_message(
                "This command isn't available in this server.", ephemeral=True,
            )
            return

        preset_value = preset.value if preset else "full"
        theme_value = theme.value if theme else "default"
        flag_set = _parse_flags(flags)
        raw = [replay1, replay2, replay3, replay4, replay5,
               replay6, replay7, replay8, replay9, replay10]
        attachments = [a for a in raw if a is not None]

        # Validate each attachment
        max_bytes = self.config.max_upload_mb * 1024 * 1024
        valid: list[discord.Attachment] = []
        rejected: list[tuple[str, str]] = []
        for a in attachments:
            # Accept .wowsreplay directly, or .zip which we'll expand after download.
            lower = a.filename.lower()
            if not (lower.endswith(".wowsreplay") or lower.endswith(".zip")):
                rejected.append((a.filename, "not a .wowsreplay or .zip file"))
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
            "/render_batch start: user=%s guild=%s attachments=%d rejected=%d preset=%s theme=%s flags=%s",
            interaction.user.id, interaction.guild_id,
            len(valid), len(rejected), preset_value, theme_value,
            sorted(flag_set) or "—",
        )
        await interaction.response.defer()

        batch_tmp = tempfile.mkdtemp(prefix="wows_batch_")
        cfg = self.config
        try:
            # Download all attachments first (zip or wowsreplay). Zip
            # expansion happens afterwards so a broken zip doesn't block
            # other replays.
            await interaction.edit_original_response(
                content=f"Downloading {len(valid)} attachment{'s' if len(valid) > 1 else ''}...",
            )
            downloaded: list[tuple[discord.Attachment, Path]] = []
            for i, a in enumerate(valid):
                dst = Path(batch_tmp) / f"att{i}_{Path(a.filename).name}"
                downloaded.append((a, dst))
            await asyncio.gather(
                *[a.save(dst) for a, dst in downloaded],
            )

            # Expand everything into a flat list of replay paths (dedup by
            # cap at BATCH_MAX_REPLAYS). Zips contribute their internal
            # .wowsreplay entries; direct .wowsreplay uploads just pass through.
            replay_sources: list[tuple[str, Path]] = []  # (display_name, replay_path)
            for a, dst in downloaded:
                if dst.suffix.lower() == ".zip":
                    extracted, zip_skipped = _extract_replays_from_zip(
                        dst, Path(batch_tmp), max_bytes,
                        already=len(replay_sources),
                        cap=BATCH_MAX_REPLAYS,
                    )
                    for name, path in extracted:
                        replay_sources.append((name, path))
                    for skip_name, reason in zip_skipped:
                        rejected.append((f"{a.filename}::{skip_name}", reason))
                    if not extracted and not zip_skipped:
                        rejected.append((a.filename, "zip contained no .wowsreplay files"))
                else:
                    if len(replay_sources) >= BATCH_MAX_REPLAYS:
                        rejected.append((a.filename, f"over {BATCH_MAX_REPLAYS}-replay cap"))
                        continue
                    replay_sources.append((Path(a.filename).name, dst))

            if not replay_sources:
                details = "\n".join(f"• `{n}`: {r}" for n, r in rejected) or "(no usable replays)"
                await interaction.edit_original_response(
                    content=f"No valid replays to render:\n{details}",
                )
                return

            # Build items now that we know the full flat replay list.
            items = [
                _BatchItem(
                    index=idx,
                    filename=name,
                    replay_path=path,
                    output_path=Path(batch_tmp) / f"r{idx}_{Path(name).stem}.mp4",
                )
                for idx, (name, path) in enumerate(replay_sources)
            ]

            batch_start = time.monotonic()
            # Per-replay timeout accounts for queue-wait when len(items) > max_workers
            per_replay_timeout = float(cfg.render_timeout)
            # Semaphore caps in-flight submissions at max_workers; later items wait here,
            # NOT in the pool's internal queue (so a mid-batch pool rebuild doesn't cancel them).
            semaphore = asyncio.Semaphore(max(1, cfg.max_workers))
            tasks = [
                asyncio.create_task(
                    self._render_one_for_batch(
                        item, preset_value, per_replay_timeout, semaphore, flag_set, theme_value,
                    ),
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
                completed = i + 1
                try:
                    result = await coro
                except Exception:  # noqa: BLE001
                    # An item that dies outside its own handlers must not stop
                    # the loop: every later item's tracker is finalised here,
                    # so bailing out would silently drop their metrics too.
                    log.exception("Batch item task raised outside its handlers")
                    continue
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
                            if result.tracker is not None:
                                result.tracker.outcome = metrics.OUTCOME_OVERSIZE
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
                        if result.tracker is not None:
                            result.tracker.outcome = metrics.OUTCOME_UPLOAD_FAILED

                # Terminal outcome is only settled once delivery was attempted.
                # No-op for items already finalised on a failure path.
                if result.tracker is not None:
                    metrics.finish_render(result.tracker)

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
                "[BATCH] user=%s guild=%s total=%d ok=%d skipped=%d time=%.1fs preset=%s theme=%s",
                interaction.user.id, interaction.guild_id,
                len(items), ok_count, len(rejected), batch_elapsed, preset_value, theme_value,
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

    @app_commands.command(
        name="render_dual",
        description="Merge two replays from the same match into a single neutral-observer video",
    )
    @app_commands.describe(
        replay1="First replay (.wowsreplay)",
        replay2="Second replay (.wowsreplay, must be from the same match)",
        theme="Color theme (default: Default)",
        flags="Comma-separated flags. Available: anonymize",
    )
    @app_commands.choices(theme=THEME_CHOICES)
    @app_commands.checks.dynamic_cooldown(_dual_cooldown)
    async def render_dual(
        self,
        interaction: discord.Interaction,
        replay1: discord.Attachment,
        replay2: discord.Attachment,
        theme: app_commands.Choice[str] | None = None,
        flags: str | None = None,
    ) -> None:
        flag_set = _parse_flags(flags)
        theme_value = theme.value if theme else "default"

        # Validate both attachments
        max_bytes = self.config.max_upload_mb * 1024 * 1024
        for a in (replay1, replay2):
            if not a.filename.endswith(".wowsreplay"):
                await interaction.response.send_message(
                    f"`{a.filename}`: not a .wowsreplay file.", ephemeral=True,
                )
                return
            if a.size > max_bytes:
                await interaction.response.send_message(
                    f"`{a.filename}`: {a.size / 1024 / 1024:.1f} MB > {self.config.max_upload_mb} MB limit.",
                    ephemeral=True,
                )
                return

        log.info(
            "/render_dual start: user=%s guild=%s replay_a=%s replay_b=%s theme=%s flags=%s",
            interaction.user.id, interaction.guild_id,
            replay1.filename, replay2.filename, theme_value,
            sorted(flag_set) or "—",
        )
        await interaction.response.defer()

        tmp_dir = tempfile.mkdtemp(prefix="wows_dual_")
        cfg = self.config
        # /render_dual has no preset choice — the dual layout is fixed.
        tracked = metrics.RenderTracker("render_dual", "dual")
        try:
            path_a = Path(tmp_dir) / Path(replay1.filename).name
            path_b = Path(tmp_dir) / f"b_{Path(replay2.filename).name}"
            output_path = Path(tmp_dir) / "dual.mp4"

            await interaction.edit_original_response(content="Downloading replays...")
            await asyncio.gather(
                replay1.save(path_a),
                replay2.save(path_b),
            )

            await interaction.edit_original_response(content="Parsing + merging...")
            t_start = time.monotonic()
            progress_queue = self._manager.Queue()
            render_call = functools.partial(
                render_dual_replay,
                str(path_a),
                str(path_b),
                str(output_path),
                str(cfg.gamedata_path),
                progress_queue,
                speed=cfg.render_speed,
                fps=cfg.render_fps,
                minimap_size=cfg.minimap_size,
                panel_width=cfg.panel_width,
                flags=flag_set,
                theme=theme_value,
            )
            pool, future = await self._submit_render(render_call)

            # Poll with the same cadence as /render
            last_msg = "Parsing + merging..."
            deadline = asyncio.get_event_loop().time() + self.config.render_timeout
            while not future.done():
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    future.cancel()
                    raise TimeoutError
                await asyncio.sleep(min(2, remaining))
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

            (
                _, replay_duration, timings, game_version, num_players,
                game_type, _build_urls, chat_text,
            ) = await future
            elapsed = time.monotonic() - t_start

            file_size = output_path.stat().st_size
            log.info(
                "Dual render done (%.1fs); uploading %.1fMB",
                elapsed, file_size / 1024 / 1024,
            )
            mins, secs = divmod(int(replay_duration), 60)
            too_large = file_size > DISCORD_ATTACHMENT_LIMIT_MB * 1024 * 1024

            # Recorded before delivery — see the same comment in /render.
            metrics.record_render(
                tracked, timings,
                output_bytes=file_size,
                game_version=game_version,
                game_type=game_type,
            )

            t_upload_start = time.perf_counter()
            try:
                if too_large:
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
                    chat_filename = f"{Path(replay1.filename).stem}__{Path(replay2.filename).stem}_chat.txt"
                    result_view = _RenderResultView(
                        build_urls=[], chat_text=chat_text, chat_filename=chat_filename,
                    )
                    edit_kwargs: dict = {
                        "content": (
                            f"Dual-perspective render — both teams visible.\n"
                            f"{game_type} · {mins}:{secs:02d} · {num_players} players · "
                            f"v{game_version} · Rendered in {elapsed:.1f}s · "
                            f"{file_size / 1024 / 1024:.1f} MB"
                        ),
                        "attachments": [discord.File(str(output_path), filename="dual_render.mp4")],
                    }
                    if result_view.children:
                        edit_kwargs["view"] = result_view
                    msg = await asyncio.wait_for(
                        interaction.edit_original_response(**edit_kwargs),
                        timeout=120,
                    )
                    if result_view.children:
                        result_view.message = msg
            except Exception:
                tracked.outcome = metrics.OUTCOME_UPLOAD_FAILED
                log.exception("Discord delivery failed for dual render (render itself succeeded)")
                try:
                    await interaction.edit_original_response(
                        content="Render finished, but sending it to Discord failed. Please try again.",
                    )
                except Exception:  # noqa: BLE001 - best effort; the interaction may be dead too
                    pass
                return
            upload_time = time.perf_counter() - t_upload_start

            log.info(
                "[TIMING-DUAL] resolve=%.2fs parse=%.2fs render=%.2fs encode=%.2fs total=%.1fs frames=%d",
                timings.get("resolve", 0), timings.get("parse", 0),
                timings.get("render", 0), timings.get("encode", 0),
                elapsed, int(timings.get("_frames", 0)),
            )
            metrics.record_delivery(
                tracked,
                elapsed=time.monotonic() - t_start,
                upload_seconds=None if too_large else upload_time,
            )
            if too_large:
                # The render worked; the user still got no video.
                tracked.outcome = metrics.OUTCOME_OVERSIZE
        except TimeoutError:
            tracked.outcome = metrics.OUTCOME_TIMEOUT
            await interaction.edit_original_response(
                content=f"Render timed out after {self.config.render_timeout}s.",
            )
        except BrokenProcessPool:
            tracked.outcome = metrics.OUTCOME_WORKER_CRASH
            log.exception("Dual render worker died")
            await self._replace_broken_pool(pool)
            await interaction.edit_original_response(
                content="Render worker crashed (likely out of memory). Please try again.",
            )
        except Exception as e:  # noqa: BLE001
            tracked.outcome = metrics.OUTCOME_ERROR
            log.exception("Dual render failed")
            # merge_replays raises if the two replays aren't from the same match
            msg = str(e) or type(e).__name__
            if "arenaUniqueId" in msg or "map_name" in msg or "merge" in msg.lower():
                await interaction.edit_original_response(
                    content=f"Cannot merge — the two replays don't appear to be from the same match: {msg}",
                )
            else:
                await interaction.edit_original_response(
                    content="Render failed. Check the replay files and try again.",
                )
        finally:
            metrics.finish_render(tracked)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @render_dual.error
    async def render_dual_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            retry_min = error.retry_after / 60
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"Dual render is on cooldown — try again in {retry_min:.1f} min.",
                    ephemeral=True,
                )
        else:
            log.exception("Unhandled error in /render_dual", exc_info=error)
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong.", ephemeral=True)
