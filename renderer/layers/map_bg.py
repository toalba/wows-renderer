from __future__ import annotations
import cairo
from renderer.layers.base import Layer, RenderContext
from renderer.assets import load_minimap


class MapBackgroundLayer(Layer):
    """Draws the minimap background image and dark side panels."""

    _minimap_surface: cairo.ImageSurface | None = None
    _scale_x: float = 1.0
    _scale_y: float = 1.0

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        config = ctx.config

        # Load minimap
        self._minimap_surface = load_minimap(config.gamedata_path, ctx.replay.map_name)

        # Compute scale to fit minimap_size
        src_w = self._minimap_surface.get_width()
        src_h = self._minimap_surface.get_height()
        self._scale_x = config.minimap_size / src_w
        self._scale_y = config.minimap_size / src_h

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config

        # Dark background for side panels
        cr.set_source_rgb(0.12, 0.12, 0.15)
        # Left panel
        cr.rectangle(0, 0, config.panel_width, config.total_height)
        cr.fill()
        # Right panel
        cr.rectangle(config.panel_width + config.minimap_size, 0, config.panel_width, config.total_height)
        cr.fill()

        # Draw minimap (scaled + positioned)
        if self._minimap_surface:
            cr.save()
            cr.translate(config.panel_width, 0)
            cr.scale(self._scale_x, self._scale_y)
            cr.set_source_surface(self._minimap_surface, 0, 0)
            cr.paint()
            cr.restore()
