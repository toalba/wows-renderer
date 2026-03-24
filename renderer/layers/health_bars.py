from __future__ import annotations

import cairo

from renderer.layers.base import Layer, RenderContext


class HealthBarLayer(Layer):
    """Draws per-ship HP bars near ship icons.

    Color changes by HP fraction (Trap 12):
      > 66% → green
      > 33% → yellow
      ≤ 33% → red

    Background is dark gray at 70% alpha.
    A lighter segment shows regeneration_health (repair party recoverable HP).
    """

    BAR_WIDTH = 28
    BAR_HEIGHT = 3
    BAR_OFFSET_Y = 12  # Pixels below ship center
    BG_ALPHA = 0.7

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        for entity_id, ship in state.ships.items():
            if not ship.is_alive:
                continue
            if not self.ctx.is_visible(entity_id, timestamp):
                continue
            if ship.max_health <= 0:
                continue

            # Detected check: only show HP for detected enemies
            player = self.ctx.player_lookup.get(entity_id)
            relation = player.relation if player else 2
            if relation == 2:
                is_det = getattr(ship, "is_detected", ship.visibility_flags > 0)
                if not is_det:
                    continue  # Undetected enemy — no HP bar

            wx, _, wz = ship.position
            px, py = self.ctx.world_to_pixel(wx, wz)

            fraction = ship.health / ship.max_health
            fraction = max(0.0, min(1.0, fraction))

            self._draw_bar(cr, px, py, fraction, ship)

    def _draw_bar(
        self, cr: cairo.Context, px: float, py: float,
        fraction: float, ship: object,
    ) -> None:
        w = self.BAR_WIDTH
        h = self.BAR_HEIGHT
        x = px - w / 2
        y = py + self.BAR_OFFSET_Y

        # Background
        cr.set_source_rgba(0.2, 0.2, 0.2, self.BG_ALPHA)
        cr.rectangle(x, y, w, h)
        cr.fill()

        # Regeneration health segment (lighter, behind current HP)
        if ship.max_health > 0:
            regen_frac = (ship.health + ship.regeneration_health) / ship.max_health
            regen_frac = max(0.0, min(1.0, regen_frac))
            if regen_frac > fraction:
                rg, gg, bg = self._hp_color(fraction)
                cr.set_source_rgba(rg, gg, bg, 0.3)
                cr.rectangle(x, y, w * regen_frac, h)
                cr.fill()

        # Current HP bar
        r, g, b = self._hp_color(fraction)
        cr.set_source_rgba(r, g, b, 0.9)
        cr.rectangle(x, y, w * fraction, h)
        cr.fill()

    @staticmethod
    def _hp_color(fraction: float) -> tuple[float, float, float]:
        """HP bar color based on remaining fraction (Trap 12)."""
        if fraction > 0.66:
            return (0.0, 1.0, 0.0)      # Green
        elif fraction > 0.33:
            return (1.0, 1.0, 0.0)      # Yellow
        else:
            return (1.0, 0.0, 0.0)      # Red
