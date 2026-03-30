from __future__ import annotations

import cairo

from renderer.layers.base import Layer, RenderContext, FONT_FAMILY


class HealthBarLayer(Layer):
    """Draws per-ship HP bars near ship icons with ship name underneath.

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
    SHIP_NAME_FONT_SIZE = 10.0
    SHIP_NAME_OFFSET_Y = 7  # Pixels below HP bar
    SHIP_NAME_COLOR = (1.0, 1.0, 1.0)  # white

    _entity_ship_names: dict[int, str]
    _has_repair_party: dict[int, bool]

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        self._entity_ship_names = {}
        self._has_repair_party = {}
        ship_db = ctx.ship_db or {}

        # Load consumable data for repair party gating
        from renderer.assets import load_ship_consumables
        ship_consumables = load_ship_consumables(ctx.config.gamedata_path)

        for entity_id, player in ctx.player_lookup.items():
            if not player.ship_id:
                continue

            # Ship name
            if player.ship_id in ship_db:
                entry = ship_db[player.ship_id]
                short = entry.get("short_name", "")
                if short:
                    self._entity_ship_names[entity_id] = short
                else:
                    name = entry.get("name", "")
                    if name:
                        parts = name.split("_", 1)
                        display = parts[1] if len(parts) > 1 else parts[0]
                        self._entity_ship_names[entity_id] = display.replace("_", " ")

            # Repair party check
            cons = ship_consumables.get(player.ship_id)
            if cons is not None:
                self._has_repair_party[entity_id] = cons["has_repair_party"]

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

            has_heal = self._has_repair_party.get(entity_id, False)
            self._draw_bar(cr, px, py, fraction, ship, has_heal)

            # Ship name below HP bar (neutral color, not team-colored)
            ship_name = self._entity_ship_names.get(entity_id)
            if ship_name:
                self._draw_ship_name(cr, px, py, ship_name)

    def _draw_bar(
        self, cr: cairo.Context, px: float, py: float,
        fraction: float, ship: object, has_heal: bool = False,
    ) -> None:
        s = self.ctx.scale
        w = self.BAR_WIDTH * s
        h = self.BAR_HEIGHT * s
        x = px - w / 2
        y = py + self.BAR_OFFSET_Y * s

        # Background
        cr.set_source_rgba(0.2, 0.2, 0.2, self.BG_ALPHA)
        cr.rectangle(x, y, w, h)
        cr.fill()

        # Regeneration health segment — only show for ships with Repair Party
        if has_heal and ship.max_health > 0:
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

    def _draw_ship_name(
        self, cr: cairo.Context, px: float, py: float,
        name: str,
    ) -> None:
        """Draw ship name centered below the HP bar with dark halo."""
        cr.save()
        s = self.ctx.scale
        font_size = self.SHIP_NAME_FONT_SIZE * s
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(font_size)

        extents = cr.text_extents(name)
        tx = px - extents.width / 2
        ty = py + (self.BAR_OFFSET_Y + self.BAR_HEIGHT + self.SHIP_NAME_OFFSET_Y) * s

        r, g, b = self.SHIP_NAME_COLOR
        self.draw_text_halo(
            cr, tx, ty, name,
            r, g, b, alpha=1.0,
            font_size=font_size, bold=False, outline_width=1.5 * s,
        )
        cr.restore()

    @staticmethod
    def _hp_color(fraction: float) -> tuple[float, float, float]:
        """HP bar color based on remaining fraction (Trap 12)."""
        if fraction > 0.66:
            return (0.0, 1.0, 0.0)      # Green
        elif fraction > 0.33:
            return (1.0, 1.0, 0.0)      # Yellow
        else:
            return (1.0, 0.0, 0.0)      # Red
