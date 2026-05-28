"""Tests for RenderConfig theme resolution."""
from __future__ import annotations

import pytest

from renderer.config import RenderConfig


def test_default_theme_preserves_baseline_team_colors():
    cfg = RenderConfig()  # implicit theme="default"
    assert cfg.theme == "default"
    assert cfg.team_colors[0] == (0.36, 0.90, 0.51, 1.0)
    assert cfg.team_colors[1] == (1.00, 0.42, 0.42, 1.0)


def test_default_theme_contested_color_is_amber():
    cfg = RenderConfig()
    assert cfg.contested_color == (1.0, 0.85, 0.0)


def test_brandon_theme_overrides_team_colors():
    cfg = RenderConfig(theme="brandon")
    assert cfg.theme == "brandon"
    # Hardcoded hex-derived expectations (don't read from THEMES — that would
    # be tautological with the implementation under test).
    assert cfg.team_colors[0] == pytest.approx((0x5B / 255, 0xCA / 255, 0xEC / 255, 1.0))
    assert cfg.team_colors[1] == pytest.approx((0xEB / 255, 0x47 / 255, 0xAB / 255, 1.0))


def test_brandon_keeps_default_self_division_contested():
    cfg = RenderConfig(theme="brandon")
    assert cfg.self_color == (1.0, 1.0, 1.0, 1.0)
    assert cfg.division_color == (1.0, 0.84, 0.0, 1.0)
    assert cfg.contested_color == (1.0, 0.85, 0.0)


def test_unknown_theme_raises_value_error():
    with pytest.raises(ValueError, match="Unknown theme"):
        RenderConfig(theme="neon-purple")


def test_custom_theme_can_override_contested_color(monkeypatch):
    """A new theme entry in THEMES with a non-default contested_color
    must propagate through RenderConfig.__post_init__ — this is the
    extension path future themes will use."""
    from renderer.themes import THEMES, Theme

    monkeypatch.setitem(
        THEMES,
        "test-green-contested",
        Theme(
            team_colors={0: (0.36, 0.90, 0.51, 1.0), 1: (1.00, 0.42, 0.42, 1.0)},
            contested_color=(0.0, 1.0, 0.0),
        ),
    )
    cfg = RenderConfig(theme="test-green-contested")
    assert cfg.contested_color == (0.0, 1.0, 0.0)
