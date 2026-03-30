from __future__ import annotations

import math

import cairo

from renderer.layers.base import Layer, RenderContext, FONT_FAMILY, _font_for_text


# Species name from ships.json → icon key
_SPECIES_TO_ICON: dict[str, str] = {
    "Destroyer": "destroyer",
    "Cruiser": "cruiser",
    "Battleship": "battleship",
    "AirCarrier": "aircarrier",
    "Submarine": "submarine",
    "Auxiliary": "auxiliary",
}


class ShipLayer(Layer):
    """Draws ship class icons on the minimap with player names.

    Alive ships: team-colored class icons (from game assets) rotated by yaw.
    Dead ships: sunk variant icon or faded X mark.
    Player name shown above each ship.
    """

    ICON_SCALE = 0.85  # Scale factor for 28x28 icons (at 760px reference)
    DEAD_SIZE = 6.0    # X mark half-size
    DETECTED_ALPHA = 1.0
    UNDETECTED_ALPHA = 0.4
    NAME_OFFSET_Y = -14  # Pixels above ship center (at 760px)
    NAME_FONT_SIZE = 11.0  # At 760px reference (scaled for Warhelios)
    # Off-white for primary labels (easier on the eyes than pure white)
    LABEL_COLOR = (0.91, 0.89, 0.85)  # #E8E4D9

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        # Build entity_id → species icon key lookup
        self._entity_species: dict[int, str] = {}
        ship_db = ctx.ship_db or {}
        for entity_id, player in ctx.player_lookup.items():
            if player.ship_id and player.ship_id in ship_db:
                species = ship_db[player.ship_id].get("species", "")
                icon_key = _SPECIES_TO_ICON.get(species)
                if icon_key:
                    self._entity_species[entity_id] = icon_key

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        icons = self.ctx.ship_icons or {}

        for entity_id, ship in state.ships.items():
            if not self.ctx.is_visible(entity_id, timestamp):
                continue
            wx, _, wz = ship.position
            px, py = self.ctx.world_to_pixel(wx, wz)

            player = self.ctx.player_lookup.get(entity_id)
            relation = player.relation if player else 2

            # Team color for fallback / name coloring
            if relation == 0:
                team_color = config.self_color
                icon_variant = "white"
            elif relation == 1:
                team_color = config.team_colors.get(0, (0.33, 0.85, 0.33, 1.0))
                icon_variant = "ally"
            else:
                team_color = config.team_colors.get(1, (0.90, 0.25, 0.25, 1.0))
                icon_variant = "enemy"

            # Detection check
            is_detected = True
            if relation == 2 and ship.is_alive:
                if hasattr(ship, "is_detected"):
                    is_detected = ship.is_detected
                else:
                    is_detected = ship.visibility_flags > 0
            alpha_mult = self.DETECTED_ALPHA if is_detected else self.UNDETECTED_ALPHA

            # Get icon surface
            species_key = self._entity_species.get(entity_id)
            icon_set = icons.get(species_key) if species_key else None

            # Position packets carry yaw for all visible ships.
            # Enemy icons from game assets point the same direction as ally
            # icons, but the game renders enemies facing the opposite way
            # on the minimap — add π for enemy ships.
            heading = ship.yaw
            if relation == 2:
                heading += math.pi

            if ship.is_alive:
                if icon_set:
                    icon_surface = icon_set.get(icon_variant)
                    if icon_surface:
                        self._draw_icon(cr, px, py, heading, icon_surface, alpha_mult)
                    else:
                        self._draw_triangle(cr, px, py, heading, team_color, alpha_mult)
                else:
                    self._draw_triangle(cr, px, py, heading, team_color, alpha_mult)

                # Player name
                if player and is_detected:
                    self._draw_name(cr, px, py, player.name, team_color, alpha_mult)
            else:
                # Dead ship
                if icon_set and "sunk" in icon_set:
                    self._draw_icon(cr, px, py, 0.0, icon_set["sunk"], 0.5)
                else:
                    self._draw_dead_marker(cr, px, py, team_color)

    def _draw_icon(
        self, cr: cairo.Context, px: float, py: float, yaw: float,
        surface: cairo.ImageSurface, alpha: float = 1.0,
    ) -> None:
        """Draw a ship class icon centered at (px, py), rotated by yaw."""
        w = surface.get_width()
        h = surface.get_height()
        scale = self.ICON_SCALE * self.ctx.scale

        cr.save()
        cr.translate(px, py)
        # Ship heading: 0=north, positive=CW (compass convention).
        # Cairo: positive rotation = CW. Icon default = pointing RIGHT (east).
        # Offset by -π/2 to align icon "east" with heading "north".
        cr.rotate(yaw - math.pi / 2)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, -w / 2, -h / 2)
        cr.paint_with_alpha(alpha)
        cr.restore()

    def _draw_name(
        self, cr: cairo.Context, px: float, py: float,
        name: str, color: tuple[float, float, float, float],
        alpha_mult: float = 1.0,
    ) -> None:
        """Draw player name above the ship with dark halo."""
        if not name:
            return
        cr.save()
        s = self.ctx.scale
        font_size = self.NAME_FONT_SIZE * s
        cr.select_font_face(_font_for_text(name), cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font_size)
        extents = cr.text_extents(name)
        tx = px - extents.width / 2
        ty = py + self.NAME_OFFSET_Y * s

        r, g, b, a = color
        self.draw_text_halo(
            cr, tx, ty, name,
            r, g, b, alpha=a * alpha_mult,
            font_size=font_size, bold=True, outline_width=2.5 * s,
        )
        cr.restore()

    def _draw_triangle(
        self, cr: cairo.Context, px: float, py: float, yaw: float,
        color: tuple[float, float, float, float], alpha_mult: float = 1.0,
    ) -> None:
        """Fallback: team-colored triangle pointing in yaw direction."""
        size = 10.0
        r, g, b, a = color

        cr.save()
        cr.translate(px, py)
        cr.rotate(yaw)

        cr.move_to(0, -size)
        cr.line_to(-size * 0.5, size * 0.4)
        cr.line_to(size * 0.5, size * 0.4)
        cr.close_path()

        cr.set_source_rgba(r, g, b, a * alpha_mult)
        cr.fill_preserve()

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
