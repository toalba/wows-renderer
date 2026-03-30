"""Renders smoke screens on the minimap as semi-transparent gray circles."""

from __future__ import annotations

import math

import cairo

from renderer.layers.base import Layer, RenderContext


class SmokeLayer(Layer):
    """Draws smoke screen clouds on the minimap.

    SmokeScreen entities have:
    - radius: float (in space_units)
    - points: list of {x, y, z} positions (smoke puff locations)

    Each point is rendered as a semi-transparent gray circle.
    Smoke is drawn below ships but above the map background.
    """

    SMOKE_COLOR = (0.85, 0.85, 0.85)  # light gray
    FILL_ALPHA = 0.35
    RADIUS_MULTIPLIER = 1.0  # exact game radius

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        tracker = getattr(self.ctx.replay, "_tracker", None)
        if tracker is None:
            return

        map_size = self.ctx.map_size
        mm = self.ctx.config.minimap_size
        r, g, b = self.SMOKE_COLOR

        for entity_id, entity_type in tracker._entity_types.items():
            if entity_type != "SmokeScreen":
                continue

            props = tracker._current.get(entity_id, {})
            radius = props.get("radius", 0)
            if not radius:
                continue

            # Smoke puff positions come from NonVolatilePosition packets
            # stored in tracker._positions, NOT from the points property.
            # Each position entry is (timestamp, (x, y, z), yaw).
            all_positions = tracker._positions.get(entity_id, [])
            if not all_positions:
                continue

            # Only show positions that exist at this timestamp
            # First position = entity creation time
            if all_positions[0][0] > timestamp:
                continue  # not yet created

            # Check if smoke has expired (EntityLeave)
            leave_time = tracker._entity_leave_times.get(entity_id)
            if leave_time is not None and leave_time <= timestamp:
                continue

            # Trap 3: smoke radius is in space_units
            px_radius = radius * self.RADIUS_MULTIPLIER / map_size * mm

            # Draw each smoke puff that exists at this timestamp
            for pos_t, pos_xyz, _ in all_positions:
                if pos_t > timestamp:
                    break  # future puffs not yet laid
                wx, wz = pos_xyz[0], pos_xyz[2]
                px, py = self.ctx.world_to_pixel(wx, wz)

                cr.new_sub_path()
                cr.arc(px, py, px_radius, 0, 2 * math.pi)
                cr.set_source_rgba(r, g, b, self.FILL_ALPHA)
                cr.fill()
