"""Renders consumable icons near ships + detection radius circles."""

from __future__ import annotations

import math

import cairo

from renderer.assets import CONSUMABLE_TYPE_ID_MAP, CONSUMABLE_TYPE_TO_ICONS
from renderer.layers.base import Layer, BaseRenderContext


# Consumable types that show detection radius circles
_CIRCLE_TYPES = {"sonar", "rls", "hydrophone", "submarineLocator"}

# Circle colors per consumable type: (r, g, b)
_CIRCLE_COLORS: dict[str, tuple[float, float, float]] = {
    "sonar": (0.2, 0.8, 0.9),       # teal
    "rls": (0.9, 0.3, 0.3),         # red
    "hydrophone": (0.4, 0.5, 0.9),  # blue
    "submarineLocator": (0.5, 0.3, 0.8),  # purple
}


class ConsumableLayer(Layer):
    """Draws consumable icons below ships when activated.

    Also draws detection radius circles for radar, hydro, and hydrophone
    using per-ship range data from GameParams.

    The cons_id from onConsumableUsed is a global consumableTypeId
    (from server-side ConsumableIDsMap). This maps directly to a
    consumableType string, which maps to an icon file.
    """

    ICON_SIZE = 16
    ICON_GAP = 2
    OFFSET_Y = 30
    ACTIVE_ALPHA = 1.0
    CIRCLE_FILL_ALPHA = 0.03
    CIRCLE_STROKE_ALPHA = 0.25
    CIRCLE_STROKE_WIDTH = 1.0

    # cons_id → cairo.ImageSurface (global, not per-entity)
    _type_icons: dict[int, cairo.ImageSurface]
    # entity_id → {consumableType: range_meters}
    _entity_ranges: dict[int, dict[str, float]]

    def initialize(self, ctx: BaseRenderContext) -> None:
        super(ConsumableLayer, self).initialize(ctx)
        from renderer.assets import load_consumable_icons, load_ship_consumables
        gp = ctx.config.effective_gamedata_path
        all_icons = load_consumable_icons(gp)
        ship_consumables = load_ship_consumables(gp)

        # Build global cons_id → icon mapping
        self._type_icons = {}
        for type_id, type_name in CONSUMABLE_TYPE_ID_MAP.items():
            candidates = CONSUMABLE_TYPE_TO_ICONS.get(type_name, [])
            for icon_name in candidates:
                if icon_name in all_icons:
                    self._type_icons[type_id] = all_icons[icon_name]
                    break

        # Build per-entity range data from ship_consumables
        self._entity_ranges = {}
        for entity_id, player in ctx.player_lookup.items():
            if player.ship_id:
                cons = ship_consumables.get(player.ship_id, {})
                ranges = cons.get("ranges", {})
                if ranges:
                    self._entity_ranges[entity_id] = ranges

        # Build activation index from ConsumableEvents
        # entity_id → list of (timestamp, cons_type_id, duration)
        self._activations: dict[int, list[tuple[float, int, float]]] = {}
        for event in ctx.replay.events:
            if type(event).__name__ != "ConsumableEvent":
                continue
            if not event.is_used:
                continue
            eid = event.entity_id
            params = event.raw_data.get("consumableUsageParams", {})
            cons_id = params.get("consumable_id", 0)
            duration = event.work_time_left
            if duration > 0:
                self._activations.setdefault(eid, []).append(
                    (event.timestamp, cons_id, duration)
                )

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        for entity_id, ship in state.ships.items():
            if not ship.is_alive:
                continue
            if not self.ctx.is_visible(entity_id, timestamp):
                continue

            # Detection check
            player = self.ctx.player_lookup.get(entity_id)
            relation = player.relation if player else 2
            if relation == 2:
                is_det = getattr(ship, "is_detected", ship.visibility_flags > 0)
                if not is_det:
                    continue

            # Get active consumables
            activations = self._activations.get(entity_id, [])
            active_icons: list[cairo.ImageSurface] = []
            active_type_names: list[str] = []
            for activated_at, cons_id, duration in activations:
                if activated_at <= timestamp < activated_at + duration:
                    icon = self._type_icons.get(cons_id)
                    if icon is not None:
                        active_icons.append(icon)
                    type_name = CONSUMABLE_TYPE_ID_MAP.get(cons_id, "")
                    if type_name:
                        active_type_names.append(type_name)

            if not active_icons and not active_type_names:
                continue

            wx, _, wz = ship.position
            px, py = self.ctx.world_to_pixel(wx, wz)

            # Draw detection radius circles for radar/hydro
            entity_ranges = self._entity_ranges.get(entity_id, {})
            for type_name in active_type_names:
                if type_name not in _CIRCLE_TYPES:
                    continue
                range_meters = entity_ranges.get(type_name, 0)
                if range_meters <= 0:
                    continue
                self._draw_range_circle(
                    cr, px, py, range_meters, type_name, relation,
                )

            # Draw icon row
            if active_icons:
                s = self.ctx.scale
                icon_size = self.ICON_SIZE * s
                icon_gap = self.ICON_GAP * s
                n = len(active_icons)
                row_width = n * icon_size + (n - 1) * icon_gap
                start_x = px - row_width / 2
                icon_y = py + self.OFFSET_Y * s

                for i, icon_surface in enumerate(active_icons):
                    icon_x = start_x + i * (icon_size + icon_gap)
                    self._draw_icon(cr, icon_x, icon_y, icon_surface)

    def _draw_range_circle(
        self, cr: cairo.Context, px: float, py: float,
        range_meters: float, type_name: str, relation: int = 2,
    ) -> None:
        """Draw a detection radius circle centered on the ship."""
        # range_meters is in game meters. Convert to pixels:
        # Trap 3: meters → space_units = ÷30, then space_units → pixels
        map_size = self.ctx.map_size  # in space_units
        mm = self.ctx.config.minimap_size
        radius_px = range_meters / 30.0 / map_size * mm

        # Team-colored: ally/self = blue, enemy = red
        if relation <= 1:  # self or ally
            r, g, b = (0.2, 0.5, 1.0)
        else:
            r, g, b = (1.0, 0.25, 0.25)

        is_radar = type_name == "rls"

        # Fill (radar only)
        if is_radar:
            cr.new_sub_path()
            cr.arc(px, py, radius_px, 0, 2 * math.pi)
            cr.set_source_rgba(r, g, b, self.CIRCLE_FILL_ALPHA)
            cr.fill()

        # Stroke
        cr.new_sub_path()
        cr.arc(px, py, radius_px, 0, 2 * math.pi)
        cr.set_source_rgba(r, g, b, self.CIRCLE_STROKE_ALPHA)
        cr.set_line_width(self.CIRCLE_STROKE_WIDTH)
        if not is_radar:
            cr.set_dash([6, 4])
        cr.stroke()
        if not is_radar:
            cr.set_dash([])  # reset

    def _draw_icon(
        self, cr: cairo.Context, x: float, y: float, surface: cairo.ImageSurface,
    ) -> None:
        w = surface.get_width()
        h = surface.get_height()
        scale = self.ICON_SIZE * self.ctx.scale / max(w, h)

        cr.save()
        cr.translate(x, y)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, 0, 0)
        cr.paint_with_alpha(self.ACTIVE_ALPHA)
        cr.restore()
