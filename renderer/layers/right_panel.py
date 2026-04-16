"""Composite right-panel layer.

Owns and orchestrates: PlayerHeader → DamageStats → Ribbons → Killfeed.
Draws a single shared background behind all sub-layers.
Toggle this one layer to show/hide the entire right panel.
"""
from __future__ import annotations

import cairo

from renderer.layers.base import Layer, SingleRenderContext
from renderer.layers.damage_stats import DamageStatsLayer
from renderer.layers.killfeed import KillfeedLayer
from renderer.layers.player_header import PlayerHeaderLayer
from renderer.layers.ribbons import RibbonLayer


class RightPanelLayer(Layer):
    """Composite layer for the entire right panel."""

    BG_ALPHA = 0.5
    BG_CORNER_RADIUS = 3
    PADDING = 8
    Y_START = 6

    def __init__(
        self,
        *,
        show_header: bool = True,
        show_damage: bool = True,
        show_ribbons: bool = True,
        show_killfeed: bool = True,
    ) -> None:
        self._show_header = show_header
        self._show_damage = show_damage
        self._show_ribbons = show_ribbons
        self._show_killfeed = show_killfeed

        self._header = PlayerHeaderLayer() if show_header else None
        self._damage = DamageStatsLayer() if show_damage else None
        self._ribbons = RibbonLayer() if show_ribbons else None
        self._killfeed = KillfeedLayer() if show_killfeed else None

        # Wire cross-references
        if self._header and self._damage:
            self._header._dmg_stats_ref = self._damage
            self._damage._header_ref = self._header
        if self._ribbons and self._damage:
            self._ribbons._dmg_stats_ref = self._damage

    def initialize(self, ctx: SingleRenderContext) -> None:
        super().initialize(ctx)
        for sub in self._sub_layers():
            sub.initialize(ctx)

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        if config.right_panel <= 0:
            return

        s = self.ctx.scale
        _pad = self.PADDING * s  # noqa: F841 — sub-layers compute their own padding; kept for future composite header
        panel_x = config.left_panel + config.minimap_size

        # Clip to right panel
        cr.save()
        cr.rectangle(panel_x, 0, config.right_panel, config.total_height)
        cr.clip()

        # Render sub-layers (header and damage draw the shared background)
        if self._header:
            self._header.render(cr, state, timestamp)
        if self._damage:
            self._damage.render(cr, state, timestamp)
        if self._ribbons:
            self._ribbons.render(cr, state, timestamp)
        if self._killfeed:
            self._killfeed.render(cr, state, timestamp)

        cr.restore()

    def _sub_layers(self):
        return [layer for layer in (self._header, self._damage, self._ribbons, self._killfeed) if layer]
