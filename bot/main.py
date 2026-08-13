"""Discord bot entry point."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from bot import metrics
from bot.cog_render import RenderCog
from bot.config import BotConfig
from bot.render_service import RenderService

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = BotConfig.from_env()

    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    async def setup_hook() -> None:
        # Started before anything else so the first renders are already
        # counted. Serves from a daemon thread; render workers are forked
        # later and never inherit the listening socket.
        if config.metrics_enabled:
            metrics.start_metrics_server(config.metrics_port)

        # One pool per process, shared by the cog and (when enabled) the
        # HTTP API — MAX_WORKERS is sized against the host, so a second
        # pool would oversubscribe CPU and the memory cap.
        service = RenderService(config)

        await bot.add_cog(RenderCog(bot, config, service))

        # HTTP render API, before the tree syncs below: those can take a
        # while, and there is no reason for the API to wait on Discord.
        # No API_TOKEN → no server at all (see BotConfig.api_token).
        if config.api_token:
            await _start_api(config, service)
        else:
            log.info("Render API disabled (no API_TOKEN set)")
        # Per-guild sync for authorized guilds → commands appear instantly
        # (global sync can take up to ~1 hour to propagate). The gated
        # commands (/render_batch, /render_dual) are guild-only anyway.
        for gid in config.authorized_guild_ids:
            guild = discord.Object(id=gid)
            bot.tree.copy_global_to(guild=guild)
            try:
                await bot.tree.sync(guild=guild)
            except discord.Forbidden:
                log.warning(
                    "Sync für Guild %s fehlgeschlagen – Bot nicht eingeladen oder "
                    "applications.commands-Scope fehlt", gid,
                )
        await bot.tree.sync()

        # Populate gamedata caches in the background so the bot starts immediately
        asyncio.create_task(_populate_caches_bg(config))

        # Liveness heartbeat for Docker HEALTHCHECK (touch /tmp/bot_heartbeat every 30s)
        asyncio.create_task(_heartbeat_bg())

        # Event-loop lag sampling — finer-grained than the 30s heartbeat, which
        # only catches a loop that is fully wedged.
        asyncio.create_task(_loop_lag_bg())

    bot.setup_hook = setup_hook

    @bot.event
    async def on_ready() -> None:
        log.info("Logged in as %s", bot.user)

    bot.run(config.discord_token, log_handler=None)


async def _start_api(config: BotConfig, service: RenderService) -> bool:
    """Serve the render API on the bot's event loop.

    Runs in-process on purpose: it shares the render pool with the cog. The
    listening socket is never inherited by render workers — the pool's
    forkserver helper is started from a clean process, the same reason the
    metrics thread is safe (see bot/metrics.py).

    Returns False (and logs) instead of raising if the port is unavailable.
    Same rule as start_metrics_server: this runs inside setup_hook, where an
    exception aborts bot startup entirely, and a busy port must not take
    Discord down with it.

    No graceful shutdown is wired up: job state is in memory and a restart
    drops it by design, and the container stops the process outright.
    """
    from aiohttp import web

    from bot.api import create_app

    try:
        runner = web.AppRunner(create_app(config, service))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", config.api_port).start()
    except OSError:
        log.exception(
            "Render API: could not bind :%d — API disabled, Discord unaffected",
            config.api_port,
        )
        return False
    log.info(
        "Render API listening on :%d (max_pending=%d, result_ttl=%ds)",
        config.api_port, config.api_max_pending, config.api_result_ttl,
    )
    return True


async def _populate_caches_bg(config: BotConfig) -> None:
    """Background task: populate version caches without blocking bot startup."""
    from renderer.gamedata_cache import populate_all_caches

    loop = asyncio.get_running_loop()
    try:
        populated = await loop.run_in_executor(
            None, populate_all_caches, config.gamedata_repo_path, config.cache_root,
        )
        if populated:
            log.info("Background cache population complete: %s", populated)
            metrics.record_cache_populated(len(populated))
        else:
            log.info("All gamedata caches already up to date")
    except Exception:
        log.exception("Background cache population failed")


_HEARTBEAT_PATH = "/tmp/bot_heartbeat"


async def _heartbeat_bg() -> None:
    """Touch ``/tmp/bot_heartbeat`` every 30s so the Docker HEALTHCHECK
    can tell the event loop is alive. If this task dies for any reason
    the file goes stale and the container is marked unhealthy.
    """
    from pathlib import Path

    hb = Path(_HEARTBEAT_PATH)
    while True:
        try:
            hb.touch()
        except OSError:
            log.exception("heartbeat touch failed")
        await asyncio.sleep(30)


_LOOP_LAG_INTERVAL = 1.0


async def _loop_lag_bg() -> None:
    """Sample event-loop responsiveness once a second.

    Sleeps for a fixed interval and records how much longer than that it
    actually took to be rescheduled. Anything blocking the loop — a sync call
    that should have gone to the pool, a large attachment write — shows up
    here as lag. Recorded as a histogram, not a gauge, so spikes between
    Prometheus scrapes are not lost.
    """
    loop = asyncio.get_running_loop()
    while True:
        before = loop.time()
        await asyncio.sleep(_LOOP_LAG_INTERVAL)
        metrics.observe_loop_lag(loop.time() - before - _LOOP_LAG_INTERVAL)


if __name__ == "__main__":
    main()
