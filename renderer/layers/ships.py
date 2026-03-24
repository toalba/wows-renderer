from __future__ import annotations
import cairo
from renderer.layers.base import Layer, RenderContext


class ShipLayer(Layer):
    """Draws ship icons on the minimap.

    Alive ships: team-colored triangles pointing in yaw direction.
    Dead ships: faded X marks.
    """

    SHIP_SIZE = 10.0  # Triangle size in pixels
    DEAD_SIZE = 6.0   # X mark half-size

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config

        for entity_id, ship in state.ships.items():
            if not self.ctx.is_visible(entity_id, timestamp):
                continue
            # Convert world position to pixel
            wx, _, wz = ship.position
            px, py = self.ctx.world_to_pixel(wx, wz)

            # Get color: white for own ship, green for ally, red for enemy
            player = self.ctx.player_lookup.get(entity_id)
            if player and player.relation == 0:
                team_color = config.self_color
            elif player:
                team_color = config.team_colors.get(player.team_id, (0.5, 0.5, 0.5, 1.0))
            else:
                team_color = (0.5, 0.5, 0.5, 1.0)

            if ship.is_alive:
                self._draw_triangle(cr, px, py, ship.yaw, team_color)
            else:
                self._draw_dead_marker(cr, px, py, team_color)

    def _draw_triangle(
        self, cr: cairo.Context, px: float, py: float, yaw: float,
        color: tuple[float, float, float, float],
    ) -> None:
        """Draw a team-colored triangle pointing in yaw direction."""
        size = self.SHIP_SIZE
        r, g, b, a = color

        cr.save()
        cr.translate(px, py)
        # Cairo: positive rotation is clockwise (Y-down), yaw: 0=north, positive=clockwise
        # In game: yaw=0 is north (up on minimap), so we rotate by -yaw + pi/2 offset
        # Actually: game yaw 0 = north = up on screen = -Y in cairo
        # We want the triangle tip to point in the yaw direction
        cr.rotate(-yaw)

        # Triangle: tip at top (north/yaw direction), base at bottom
        cr.move_to(0, -size)           # Tip (forward)
        cr.line_to(-size * 0.5, size * 0.4)   # Bottom-left
        cr.line_to(size * 0.5, size * 0.4)    # Bottom-right
        cr.close_path()

        # Fill
        cr.set_source_rgba(r, g, b, a)
        cr.fill_preserve()

        # Outline for visibility
        cr.set_source_rgba(0, 0, 0, 0.6)
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
