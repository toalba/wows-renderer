"""Renders smoke screens on the minimap as semi-transparent gray circles."""

from __future__ import annotations

import math

import cairo

from renderer.layers.base import Layer, BaseRenderContext


class SmokeLayer(Layer):
    """Draws smoke screen clouds on the minimap.

    SmokeScreen state (from ``replay.state_at(t).smoke_screens``) carries:
    - radius: float (in space_units)
    - points: list of (x, y, z) puff world positions

    Each point is rendered as a semi-transparent gray circle.
    Smoke is drawn below ships but above the map background.
    """

    SMOKE_COLOR = (0.85, 0.85, 0.85)  # light gray
    FILL_ALPHA = 0.35
    RADIUS_MULTIPLIER = 1.0  # exact game radius

    def initialize(self, ctx: BaseRenderContext) -> None:
        super().initialize(ctx)
        lifetimes = getattr(ctx.replay, "smoke_screen_lifetimes", {}) or {}
        self._leave_times: dict[int, float] = {}
        for eid, interval in lifetimes.items():
            try:
                _, leave_t = interval
            except (TypeError, ValueError):
                continue
            self._leave_times[eid] = float(leave_t)

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        map_size = self.ctx.map_size
        mm = self.ctx.config.minimap_size
        r, g, b = self.SMOKE_COLOR

        smoke_screens = getattr(state, "smoke_screens", None) or {}
        if not smoke_screens:
            return

        for entity_id, smoke in smoke_screens.items():
            radius = getattr(smoke, "radius", 0) or 0
            if not radius:
                continue

            # Check if smoke has fully expired (EntityLeave)
            leave_time = self._leave_times.get(entity_id)
            if leave_time is not None and leave_time <= timestamp:
                continue

            puffs = getattr(smoke, "points", None) or []
            if not puffs:
                continue

            # Trap 3: smoke radius is in space_units
            px_radius = radius * self.RADIUS_MULTIPLIER / map_size * mm

            for puff in puffs:
                wx = float(puff[0])
                wz = float(puff[2]) if len(puff) >= 3 else float(puff[1])
                px, py = self.ctx.world_to_pixel(wx, wz)

                cr.new_sub_path()
                cr.arc(px, py, px_radius, 0, 2 * math.pi)
                cr.set_source_rgba(r, g, b, self.FILL_ALPHA)
                cr.fill()
