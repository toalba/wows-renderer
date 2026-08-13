# tests/test_cog_service_wiring.py
"""RenderCog holds the shared RenderService without colliding with its own
slash commands.

The pool used to live on the cog; it now lives on an injected RenderService.
The obvious attribute name for it — `self.render` — silently shadows the
`/render` command object that the app_commands decorator puts on the class,
so the service is stored as `render_service` instead. These tests pin both
halves: the commands still register, and the service is reachable.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("discord")

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from bot.cog_render import RenderCog  # noqa: E402

EXPECTED_COMMANDS = {"render", "render_batch", "render_dual"}


def _cog() -> RenderCog:
    """A cog wired to a stub service. No pool is created and no connection is
    opened — Cog construction is offline, only add_cog would need a client."""
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    config = SimpleNamespace(max_workers=1, render_max_tasks_per_child=None)
    service = SimpleNamespace(config=config)
    return RenderCog(bot, config, service)  # type: ignore[arg-type]


def test_slash_commands_still_register():
    names = {cmd.name for cmd in _cog().get_app_commands()}
    assert names >= EXPECTED_COMMANDS


def test_service_is_reachable_and_does_not_shadow_the_render_command():
    cog = _cog()
    assert cog.render_service is not None
    # `cog.render` must still be the bound /render command, not the service.
    assert isinstance(cog.render, discord.app_commands.Command)
