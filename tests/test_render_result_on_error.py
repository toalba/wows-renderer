# tests/test_render_result_on_error.py
"""_RenderResultView.on_error must surface button failures to the user.

`show_statistics` defers the interaction before rendering. On a component
interaction that maps to `deferred_message_update` (type 6), not a
"thinking" state — so if the render raises, discord.py's default
`View.on_error` (log-only, no user-visible effect) leaves the user with
*no* feedback at all: not even Discord's own "This interaction failed".
These tests hold the override that fixes that for all three buttons.

Driven through asyncio.run rather than pytest-asyncio, matching
test_metrics.py, so the suite needs no extra plugin.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("discord")

import discord

from bot.cog_render import _RenderResultView


def _view() -> _RenderResultView:
    return _RenderResultView(
        build_urls=[], chat_text="", chat_filename="c.txt",
        stats=None, theme="default",
    )


def _fake_item():
    return SimpleNamespace(label="Statistics")


def _fake_interaction(*, response_done: bool) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = response_done
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def test_on_error_sends_message_when_response_not_yet_answered() -> None:
    """`show_statistics` fails before its `defer()` (or a button that never
    defers fails outright) — the interaction is still open, so on_error
    must answer it directly."""
    interaction = _fake_interaction(response_done=False)

    asyncio.run(_view().on_error(interaction, RuntimeError("boom"), _fake_item()))

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "failed" in args[0].lower()
    assert kwargs.get("ephemeral") is True
    interaction.followup.send.assert_not_awaited()


def test_on_error_uses_followup_when_already_deferred() -> None:
    """This is the exact failure mode in the finding: `show_statistics`
    calls `interaction.response.defer()` before `render_stats_board` can
    raise, so by the time on_error runs, `is_done()` is True and a second
    `response.send_message` would itself raise InteractionResponded."""
    interaction = _fake_interaction(response_done=True)

    asyncio.run(_view().on_error(interaction, RuntimeError("boom"), _fake_item()))

    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "failed" in args[0].lower()
    assert kwargs.get("ephemeral") is True
    interaction.response.send_message.assert_not_awaited()


def test_on_error_swallows_a_failed_apology() -> None:
    """If even the apology can't be sent (interaction already expired),
    on_error must not raise — it is already handling a failure and must
    not itself become an unhandled exception inside discord.py's dispatch."""
    interaction = _fake_interaction(response_done=False)
    fake_response = SimpleNamespace(status=404, reason="Not Found")
    interaction.response.send_message.side_effect = discord.HTTPException(
        fake_response, "Unknown interaction",
    )

    # Must not raise.
    asyncio.run(_view().on_error(interaction, RuntimeError("boom"), _fake_item()))
