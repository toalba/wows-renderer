from __future__ import annotations
import cairo
from renderer.layers.base import Layer, RenderContext, FONT_FAMILY
from renderer.assets import load_minimap, load_minimap_water


class MapBackgroundLayer(Layer):
    """Draws the minimap background image with water texture."""

    _minimap_surface: cairo.ImageSurface | None = None
    _water_surface: cairo.ImageSurface | None = None
    _scale_x: float = 1.0
    _scale_y: float = 1.0

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        config = ctx.config

        # Load minimap and water layer
        self._minimap_surface = load_minimap(config.gamedata_path, ctx.replay.map_name)
        self._water_surface = load_minimap_water(config.gamedata_path, ctx.replay.map_name)

        # Compute scale to fit minimap_size
        src_w = self._minimap_surface.get_width()
        src_h = self._minimap_surface.get_height()
        self._scale_x = config.minimap_size / src_w
        self._scale_y = config.minimap_size / src_h

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config

        # Water texture as full canvas background (replaces navy blue)
        if self._water_surface:
            water_w = self._water_surface.get_width()
            water_h = self._water_surface.get_height()
            # Scale water to fill entire canvas
            sx = config.total_width / water_w
            sy = config.total_height / water_h
            scale = max(sx, sy)
            cr.save()
            cr.scale(scale, scale)
            cr.set_source_surface(self._water_surface, 0, 0)
            cr.paint()
            cr.restore()
        else:
            # Fallback: dark background
            cr.set_source_rgb(0.05, 0.08, 0.15)
            cr.paint()

        # Draw minimap (scaled + positioned, offset by hud_height)
        if self._minimap_surface:
            cr.save()
            cr.translate(config.panel_width, config.hud_height)
            cr.scale(self._scale_x, self._scale_y)
            cr.set_source_surface(self._minimap_surface, 0, 0)
            cr.paint()
            cr.restore()

        # Grid + border on the minimap
        mx = config.panel_width
        my = config.hud_height
        ms = config.minimap_size
        cell = ms / 10.0

        # Grid lines
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.15)
        cr.set_line_width(1.0)
        for i in range(1, 10):
            # Vertical
            x = mx + i * cell
            cr.move_to(x, my)
            cr.line_to(x, my + ms)
            # Horizontal
            y = my + i * cell
            cr.move_to(mx, y)
            cr.line_to(mx + ms, y)
        cr.stroke()

        # Border
        cr.set_source_rgba(0.8, 0.8, 0.8, 0.6)
        cr.set_line_width(2.0)
        cr.rectangle(mx, my, ms, ms)
        cr.stroke()

        # Grid labels
        font_size = max(10.0, cell * 0.12)
        cr.set_font_size(font_size)
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.45)
        labels_h = "1234567890"  # 1-9 then 0 for 10
        labels_v = "ABCDEFGHIJ"
        for i in range(10):
            # Horizontal labels (numbers) — top edge
            ext = cr.text_extents(labels_h[i])
            lx = mx + i * cell + (cell - ext.width) / 2
            cr.move_to(lx, my + ext.height + 3)
            cr.show_text(labels_h[i])
            # Vertical labels (letters) — left edge
            ext = cr.text_extents(labels_v[i])
            ly = my + i * cell + (cell + ext.height) / 2
            cr.move_to(mx + 3, ly)
            cr.show_text(labels_v[i])
