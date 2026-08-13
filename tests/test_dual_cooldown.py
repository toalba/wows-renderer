# tests/test_dual_cooldown.py
"""/render_dual's cooldown length comes from config, not a constant.

A dual render parses two replays and merges them, so the default is
deliberately long — but an operator with pool headroom can lower it via
DUAL_COOLDOWN_SECONDS without editing code.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("discord")

from bot.cog_render import BATCH_COOLDOWN_SECONDS, _dual_cooldown  # noqa: E402
from bot.config import BotConfig  # noqa: E402


def _interaction(cog):
    return SimpleNamespace(client=SimpleNamespace(get_cog=lambda _name: cog), guild_id=1)


def test_uses_the_configured_length():
    cog = SimpleNamespace(config=SimpleNamespace(dual_cooldown_seconds=60))
    cooldown = _dual_cooldown(_interaction(cog))
    assert cooldown is not None
    assert cooldown.per == 60.0
    assert cooldown.rate == 1


def test_falls_back_to_the_conservative_default_without_a_cog():
    """An unreachable cog must not mean "no cooldown" — that would leave the
    heaviest command unthrottled."""
    cooldown = _dual_cooldown(_interaction(None))
    assert cooldown is not None
    assert cooldown.per == float(BATCH_COOLDOWN_SECONDS)


def _from_env(**env) -> BotConfig:
    """Build a config from exactly ``env``.

    load_dotenv is stubbed out: with a cleared environment it would otherwise
    pull in the developer's own .env and make these assertions depend on it.
    """
    with patch.dict(os.environ, {"DISCORD_TOKEN": "x", **env}, clear=True), \
            patch("bot.config.load_dotenv", lambda *a, **k: False):
        return BotConfig.from_env()


def test_default_config_keeps_the_long_cooldown():
    assert _from_env().dual_cooldown_seconds == 600


def test_env_override_is_read():
    assert _from_env(DUAL_COOLDOWN_SECONDS="60").dual_cooldown_seconds == 60


def test_nonsense_value_is_rejected_at_startup():
    with pytest.raises(RuntimeError, match="DUAL_COOLDOWN_SECONDS"):
        _from_env(DUAL_COOLDOWN_SECONDS="0")
