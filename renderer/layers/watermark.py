from __future__ import annotations

import cairo

from renderer.layers.base import BaseRenderContext, Layer


class WatermarkLayer(Layer):
    """Draws a small attribution watermark in the bottom-right corner."""

    TEXT = "Developed by Rias_prpr"
    FONT_SIZE = 11.0
    MARGIN = 6.0
    ALPHA = 0.7

    def initialize(self, ctx: BaseRenderContext) -> None:
        super().initialize(ctx)
        scale = ctx.scale
        self._font_size = self.FONT_SIZE * scale
        self._margin = self.MARGIN * scale
        self._right = ctx.config.total_width
        self._bottom = ctx.config.total_height
        self._width: float | None = None  # measured lazily once a cairo context exists

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        if self._width is None:
            surf, w, _ascent = Layer.get_cached_text(
                cr, self.TEXT, self._font_size, False, 1.0, 1.0, 1.0,
            )
            self._width = w

        x = self._right - self._width - self._margin
        y = self._bottom - self._margin
        Layer.draw_cached_text(
            cr, x, y, self.TEXT,
            1.0, 1.0, 1.0,
            alpha=self.ALPHA,
            font_size=self._font_size,
            bold=False,
        )
