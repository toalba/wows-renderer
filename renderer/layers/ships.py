from __future__ import annotations

import bisect
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
    HEADING_LINE_LENGTH = 18.0  # Pixels at 760px reference
    HEADING_LINE_WIDTH = 1.5
    NAME_OFFSET_Y = -14  # Pixels above ship center (at 760px)
    NAME_FONT_SIZE = 10.25  # At 760px reference (scaled for Warhelios)
    # Off-white for primary labels (easier on the eyes than pure white)
    LABEL_COLOR = (0.91, 0.89, 0.85)  # #E8E4D9
    # Gold glow for spotted allies/self (visibility_flags != 0)
    SPOTTED_GLOW_COLOR = (1.0, 0.84, 0.0)  # #FFD700
    SPOTTED_GLOW_RADIUS = 1.1  # outline offset in pixels at 760px reference
    SPOTTED_GLOW_ALPHA = 0.45

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

        # Build camera yaw timeline from CAMERA packets (recording player only)
        self._camera_times: list[float] = []
        self._camera_yaws: list[float] = []
        self._self_entity_id: int | None = None
        for eid, player in ctx.player_lookup.items():
            if player.relation == 0:
                self._self_entity_id = eid
                break
        self._build_camera_yaw_timeline(ctx)

        # Build per-entity targetLocalPos (gun aim) timeline from property history
        self._target_times: dict[int, list[float]] = {}
        self._target_yaws: dict[int, list[float]] = {}
        self._build_target_yaw_timeline(ctx)

    def _build_camera_yaw_timeline(self, ctx: RenderContext) -> None:
        """Extract camera yaw from CAMERA packet quaternions."""
        from wows_replay_parser.packets.types import PacketType
        for packet in ctx.replay.packets:
            if packet.type != PacketType.CAMERA:
                continue
            rot = getattr(packet, "camera_rotation", None)
            if rot is None:
                continue
            qx, qy, qz, qw = rot
            siny_cosp = 2.0 * (qw * qy + qx * qz)
            cosy_cosp = 1.0 - 2.0 * (qy * qy + qx * qx)
            yaw = math.atan2(siny_cosp, cosy_cosp)
            self._camera_times.append(packet.timestamp)
            self._camera_yaws.append(yaw)

    def _build_target_yaw_timeline(self, ctx: RenderContext) -> None:
        """Build gun aim yaw timeline from targetLocalPos property changes."""
        TWO_PI = 2.0 * math.pi
        tracker = ctx.replay._tracker
        for change in tracker._history:
            if change.property_name != "targetLocalPos":
                continue
            val = change.new_value
            if val is None or val == 65535:
                continue
            lo = int(val) & 0xFF
            yaw = (lo / 256.0) * TWO_PI - math.pi
            eid = change.entity_id
            if eid not in self._target_times:
                self._target_times[eid] = []
                self._target_yaws[eid] = []
            self._target_times[eid].append(change.timestamp)
            self._target_yaws[eid].append(yaw)

    def _get_camera_yaw(self, timestamp: float) -> float | None:
        """Look up camera yaw at a given timestamp (nearest earlier sample)."""
        if not self._camera_times:
            return None
        idx = bisect.bisect_right(self._camera_times, timestamp) - 1
        if idx < 0:
            return None
        return self._camera_yaws[idx]

    def _get_target_yaw(self, entity_id: int, timestamp: float) -> float | None:
        """Look up gun aim yaw for entity at timestamp."""
        times = self._target_times.get(entity_id)
        if not times:
            return None
        idx = bisect.bisect_right(times, timestamp) - 1
        if idx < 0:
            return None
        return self._target_yaws[entity_id][idx]

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

            heading = ship.yaw

            if ship.is_alive:
                # Spotted glow for allies/self when enemy can see them
                is_spotted = relation in (0, 1) and ship.visibility_flags > 0
                if is_spotted and icon_set:
                    self._draw_spotted_glow(cr, px, py, heading, icon_set.get(icon_variant))

                if icon_set:
                    icon_surface = icon_set.get(icon_variant)
                    if icon_surface:
                        self._draw_icon(cr, px, py, heading, icon_surface, alpha_mult)
                    else:
                        self._draw_triangle(cr, px, py, heading, team_color, alpha_mult)
                else:
                    self._draw_triangle(cr, px, py, heading, team_color, alpha_mult)

                # Look direction line (skip for undetected enemies — stale data)
                if relation == 0:
                    look_yaw = self._get_camera_yaw(timestamp)
                    if look_yaw is not None:
                        self._draw_heading_line(cr, px, py, look_yaw, team_color, alpha_mult)
                elif is_detected:
                    target_yaw = self._get_target_yaw(entity_id, timestamp)
                    if target_yaw is not None:
                        self._draw_heading_line(cr, px, py, target_yaw, team_color, alpha_mult)

                # Player name
                if player and is_detected:
                    self._draw_name(cr, px, py, player.name, team_color, alpha_mult)
            else:
                # Dead ship
                if icon_set and "sunk" in icon_set:
                    self._draw_icon(cr, px, py, 0.0, icon_set["sunk"], 0.5)
                else:
                    self._draw_dead_marker(cr, px, py, team_color)

    def _draw_spotted_glow(
        self, cr: cairo.Context, px: float, py: float, yaw: float,
        surface: cairo.ImageSurface | None,
    ) -> None:
        """Draw a gold glow outline around a spotted ally/self ship icon."""
        if surface is None:
            return
        w = surface.get_width()
        h = surface.get_height()
        scale = self.ICON_SCALE * self.ctx.scale
        r, g, b = self.SPOTTED_GLOW_COLOR
        offset = self.SPOTTED_GLOW_RADIUS * self.ctx.scale

        cr.save()
        cr.translate(px, py)
        cr.rotate(yaw)
        cr.scale(scale, scale)
        # Draw the icon shifted in 8 directions to create an outline glow
        off = offset / scale  # offset in icon-space pixels
        for dx, dy in ((-off, 0), (off, 0), (0, -off), (0, off),
                       (-off, -off), (off, -off), (-off, off), (off, off)):
            cr.set_source_rgba(r, g, b, self.SPOTTED_GLOW_ALPHA)
            cr.mask_surface(surface, -w / 2 + dx, -h / 2 + dy)
        cr.restore()

    def _draw_heading_line(
        self, cr: cairo.Context, px: float, py: float, yaw: float,
        color: tuple[float, float, float, float], alpha_mult: float = 1.0,
    ) -> None:
        """Draw a line from the ship center outward in the heading direction."""
        s = self.ctx.scale
        length = self.HEADING_LINE_LENGTH * s
        r, g, b, a = color

        # yaw: 0=north, positive=CW. Cairo: 0=east, positive=CW.
        # North in Cairo is -π/2, so dx/dy use sin/cos directly (compass convention).
        dx = math.sin(yaw) * length
        dy = -math.cos(yaw) * length

        cr.save()
        cr.set_source_rgba(r, g, b, a * alpha_mult * 0.8)
        cr.set_line_width(self.HEADING_LINE_WIDTH * s)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(px, py)
        cr.line_to(px + dx, py + dy)
        cr.stroke()
        cr.restore()

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
        # Cairo: positive rotation = CW. SVG icons point UP (north).
        cr.rotate(yaw)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, -w / 2, -h / 2)

        cr.paint_with_alpha(alpha)
        cr.restore()

    def _draw_name(
        self, cr: cairo.Context, px: float, py: float,
        name: str, color: tuple[float, float, float, float],
        alpha_mult: float = 1.0,
    ) -> None:
        """Draw player name above the ship using cached text surface."""
        if not name:
            return
        s = self.ctx.scale
        font_size = self.NAME_FONT_SIZE * s
        r, g, b, a = color

        surf, text_w, text_h = self.get_cached_text(cr, name, font_size, True, r, g, b)
        if surf.get_width() <= 1:
            return

        tx = px - text_w / 2
        ty = py + self.NAME_OFFSET_Y * s
        self.draw_cached_text(
            cr, tx, ty, name,
            r, g, b, alpha=a * alpha_mult,
            font_size=font_size, bold=True,
        )

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
