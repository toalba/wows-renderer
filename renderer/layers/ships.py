from __future__ import annotations

import math

import cairo

from renderer.layers.base import Layer, RenderContext


class ShipLayer(Layer):
    """Draws ship icons on the minimap.

    Alive ships: team-colored triangles pointing in yaw direction.
    Dead ships: faded X marks at last known position.
    Undetected enemies: reduced opacity, no name/HP overlay.
    """

    SHIP_SIZE = 10.0  # Triangle size in pixels
    DEAD_SIZE = 6.0   # X mark half-size
    DETECTED_ALPHA = 1.0
    UNDETECTED_ALPHA = 0.4

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config

        for entity_id, ship in state.ships.items():
            if not self.ctx.is_visible(entity_id, timestamp):
                continue
            # Convert world position to pixel
            wx, _, wz = ship.position
            px, py = self.ctx.world_to_pixel(wx, wz)

            # Determine player info and relation
            player = self.ctx.player_lookup.get(entity_id)
            relation = player.relation if player else 2

            # Get color based on relation (Trap 11)
            if relation == 0:
                # Self — white
                team_color = config.self_color
            elif relation == 1:
                # Ally — green (display team 0)
                team_color = config.team_colors.get(0, (0.33, 0.85, 0.33, 1.0))
            else:
                # Enemy — red (display team 1)
                team_color = config.team_colors.get(1, (0.90, 0.25, 0.25, 1.0))

            # Detected vs undetected (Trap 6)
            # Allies/self are always "detected" from our perspective.
            # Enemies: use is_detected from MinimapVisionInfo (authoritative),
            # fall back to visibility_flags > 0.
            is_detected = True
            if relation == 2 and ship.is_alive:
                if hasattr(ship, "is_detected"):
                    is_detected = ship.is_detected
                else:
                    is_detected = ship.visibility_flags > 0

            alpha_mult = self.DETECTED_ALPHA if is_detected else self.UNDETECTED_ALPHA

            if ship.is_alive:
                self._draw_triangle(cr, px, py, ship.yaw, team_color, alpha_mult)
            else:
                self._draw_dead_marker(cr, px, py, team_color)

    def _draw_triangle(
        self, cr: cairo.Context, px: float, py: float, yaw: float,
        color: tuple[float, float, float, float], alpha_mult: float = 1.0,
    ) -> None:
        """Draw a team-colored triangle pointing in yaw direction."""
        size = self.SHIP_SIZE
        r, g, b, a = color

        cr.save()
        cr.translate(px, py)
        # Trap 4: Yaw convention fix.
        # Game yaw: 0=north, positive=CW (east). From atan2(dx, dz).
        # Cairo: positive rotation is CW (Y-down).
        # Triangle tip starts at (0, -size) = north.
        # To rotate to yaw direction: rotate CW by yaw.
        cr.rotate(yaw)

        # Triangle: tip at top (forward), base at bottom
        cr.move_to(0, -size)                    # Tip (forward)
        cr.line_to(-size * 0.5, size * 0.4)     # Bottom-left
        cr.line_to(size * 0.5, size * 0.4)      # Bottom-right
        cr.close_path()

        # Fill
        cr.set_source_rgba(r, g, b, a * alpha_mult)
        cr.fill_preserve()

        # Outline for visibility
        cr.set_source_rgba(0, 0, 0, 0.6 * alpha_mult)
        cr.set_line_width(1.0)
        cr.stroke()

        cr.restore()

    def _draw_dead_marker(
        self, cr: cairo.Context, px: float, py: float,
        color: tuple[float, float, float, float],
    ) -> None:
        """Draw a faded X mark for a dead ship."""
        s = self.DEAD_SIZE
        r, g, b, _ = color

        cr.save()
        cr.translate(px, py)

        cr.set_source_rgba(r, g, b, 0.35)
        cr.set_line_width(2.0)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        cr.move_to(-s, -s)
        cr.line_to(s, s)
        cr.stroke()

        cr.move_to(s, -s)
        cr.line_to(-s, s)
        cr.stroke()

        cr.restore()
