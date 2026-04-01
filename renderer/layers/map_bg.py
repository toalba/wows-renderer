from __future__ import annotations
import cairo
from renderer.layers.base import Layer, RenderContext, FONT_FAMILY
from renderer.assets import load_minimap, load_minimap_water


class MapBackgroundLayer(Layer):
    """Draws the minimap background image with water texture.

    The entire background (water, minimap, grid, labels) is static and
    rendered once into a cached surface during initialize().
    """

    _bg_cache: cairo.ImageSurface | None = None

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        config = ctx.config

        minimap_surface = load_minimap(config.gamedata_path, ctx.replay.map_name)
        water_surface = load_minimap_water(config.gamedata_path, ctx.replay.map_name)

        scale_x = config.minimap_size / minimap_surface.get_width()
        scale_y = config.minimap_size / minimap_surface.get_height()

        # Pre-render the entire static background once
        width = config.total_width
        height = config.total_height
        self._bg_cache = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(self._bg_cache)

        # Water texture as full canvas background
        if water_surface:
            water_w = water_surface.get_width()
            water_h = water_surface.get_height()
            sx = width / water_w
            sy = height / water_h
            scale = max(sx, sy)
            cr.save()
            cr.scale(scale, scale)
            cr.set_source_surface(water_surface, 0, 0)

            cr.paint()
            cr.restore()
        else:
            cr.set_source_rgb(0.05, 0.08, 0.15)
            cr.paint()

        # Minimap image
        if minimap_surface:
            cr.save()
            cr.translate(config.left_panel, config.hud_height)
            cr.scale(scale_x, scale_y)
            cr.set_source_surface(minimap_surface, 0, 0)
            # Use BEST filter (lanczos) for sharp upscaling from 760→1080+

            cr.paint()
            cr.restore()

        # Grid + border
        mx = config.left_panel
        my = config.hud_height
        ms = config.minimap_size
        cell = ms / 10.0

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.15)
        cr.set_line_width(1.0)
        for i in range(1, 10):
            x = mx + i * cell
            cr.move_to(x, my)
            cr.line_to(x, my + ms)
            y = my + i * cell
            cr.move_to(mx, y)
            cr.line_to(mx + ms, y)
        cr.stroke()

        cr.set_source_rgba(0.8, 0.8, 0.8, 0.6)
        cr.set_line_width(2.0)
        cr.rectangle(mx, my, ms, ms)
        cr.stroke()

        # Grid labels
        font_size = max(10.0, cell * 0.12)
        cr.set_font_size(font_size)
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.45)
        labels_h = "1234567890"
        labels_v = "ABCDEFGHIJ"
        for i in range(10):
            ext = cr.text_extents(labels_h[i])
            lx = mx + i * cell + (cell - ext.width) / 2
            cr.move_to(lx, my + ext.height + 3)
            cr.show_text(labels_h[i])
            ext = cr.text_extents(labels_v[i])
            ly = my + i * cell + (cell + ext.height) / 2
            cr.move_to(mx + 3, ly)
            cr.show_text(labels_v[i])

        self._bg_cache.flush()

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        cr.set_source_surface(self._bg_cache, 0, 0)
        cr.paint()
