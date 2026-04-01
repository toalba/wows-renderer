"""Discord bot entry point."""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot.config import BotConfig
from bot.cog_render import RenderCog


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = BotConfig.from_env()

    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    async def setup_hook() -> None:
        await bot.add_cog(RenderCog(bot, config))
        await bot.tree.sync()

    bot.setup_hook = setup_hook

    @bot.event
    async def on_ready() -> None:
        logging.getLogger(__name__).info("Logged in as %s", bot.user)

    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
