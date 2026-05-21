"""Verify RibbonLayer exposes panel_bottom for downstream layers."""
from __future__ import annotations

from renderer.layers.ribbons import RibbonLayer


def test_ribbon_layer_has_panel_bottom_attribute():
    layer = RibbonLayer()
    assert hasattr(layer, "panel_bottom")
    assert layer.panel_bottom == 0.0
