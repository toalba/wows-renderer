"""Discord bot entry point."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from bot.config import BotConfig
from bot.cog_render import RenderCog

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = BotConfig.from_env()

    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    async def setup_hook() -> None:
        await bot.add_cog(RenderCog(bot, config))
        await bot.tree.sync()

        # Populate gamedata caches in the background so the bot starts immediately
        asyncio.create_task(_populate_caches_bg(config))

    bot.setup_hook = setup_hook

    @bot.event
    async def on_ready() -> None:
        log.info("Logged in as %s", bot.user)

    bot.run(config.discord_token, log_handler=None)


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
        else:
            log.info("All gamedata caches already up to date")
    except Exception:
        log.exception("Background cache population failed")


if __name__ == "__main__":
    main()
