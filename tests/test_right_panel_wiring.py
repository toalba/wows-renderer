"""Verify RightPanelLayer instantiates and wires the AchievementLayer."""
from __future__ import annotations

from renderer.layers.achievements import AchievementLayer
from renderer.layers.ribbons import RibbonLayer
from renderer.layers.right_panel import RightPanelLayer


def test_right_panel_creates_achievement_layer_by_default():
    panel = RightPanelLayer()
    assert panel._achievements is not None
    assert isinstance(panel._achievements, AchievementLayer)


def test_right_panel_skips_achievements_when_disabled():
    panel = RightPanelLayer(show_achievements=False)
    assert panel._achievements is None


def test_right_panel_wires_ribbons_ref_on_achievement_layer():
    panel = RightPanelLayer()
    assert panel._achievements is not None
    assert panel._achievements._ribbons_ref is panel._ribbons
    assert isinstance(panel._achievements._ribbons_ref, RibbonLayer)


def test_right_panel_no_ribbons_means_no_achievement_wiring():
    """If ribbons are disabled, achievements still exist but have no anchor."""
    panel = RightPanelLayer(show_ribbons=False)
    if panel._achievements is not None:
        # Either the layer exists without a ribbon ref, or wiring was skipped.
        assert getattr(panel._achievements, "_ribbons_ref", None) is None


def test_right_panel_wires_dmg_stats_ref_for_achievement_fallback():
    """When ribbons are disabled, achievements anchor under damage stats."""
    from renderer.layers.damage_stats import DamageStatsLayer
    panel = RightPanelLayer(show_ribbons=False)
    assert panel._achievements is not None
    # Even with no ribbons, damage stats is the fallback anchor.
    assert panel._achievements._dmg_stats_ref is panel._damage
    assert isinstance(panel._achievements._dmg_stats_ref, DamageStatsLayer)


def test_right_panel_sub_layers_includes_achievements():
    panel = RightPanelLayer()
    sub_layers = panel._sub_layers()
    assert any(isinstance(s, AchievementLayer) for s in sub_layers)
