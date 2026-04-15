"""Renders weather zone overlays on the minimap as semi-transparent circles."""

from __future__ import annotations

import math

import cairo

from renderer.layers.base import Layer


class WeatherLayer(Layer):
    """Draws weather zone circles (cyclones, squalls) on the minimap.

    Weather zones are InteractiveZone entities with type==5. Their
    position and radius come from the parser's GameState.weather_zones.
    """

    FILL_COLOR = (1.0, 1.0, 1.0)  # white
    FILL_ALPHA = 0.15

    def render(
        self, cr: cairo.Context, state: object, timestamp: float,
    ) -> None:
        zones = getattr(state, "weather_zones", None)
        if not zones:
            return

        map_size = self.ctx.map_size
        mm = self.ctx.config.minimap_size
        cr.set_source_rgba(*self.FILL_COLOR, self.FILL_ALPHA)

        for zone in zones.values():
            if zone.radius <= 0:
                continue
            wx, _wy, wz = zone.position
            if wx == 0.0 and wz == 0.0:
                continue

            px, py = self.ctx.world_to_pixel(wx, wz)
            px_radius = zone.radius / map_size * mm

            cr.new_sub_path()
            cr.arc(px, py, px_radius, 0, 2 * math.pi)
            cr.fill()
