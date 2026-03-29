"""Renders consumable icons near ships — only shown when active."""

from __future__ import annotations

import cairo

from renderer.assets import CONSUMABLE_TYPE_ID_MAP, CONSUMABLE_TYPE_TO_ICONS
from renderer.layers.base import Layer, RenderContext


class ConsumableLayer(Layer):
    """Draws consumable icons below ships when activated.

    The cons_id from onConsumableUsed is a global consumableTypeId
    (from server-side ConsumableIDsMap). This maps directly to a
    consumableType string, which maps to an icon file.

    No per-ship pairing needed — the mapping is global.
    """

    ICON_SIZE = 16
    ICON_GAP = 2
    OFFSET_Y = 30
    ACTIVE_ALPHA = 1.0

    # cons_id → cairo.ImageSurface (global, not per-entity)
    _type_icons: dict[int, cairo.ImageSurface]

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        from renderer.assets import load_consumable_icons
        all_icons = load_consumable_icons(ctx.config.gamedata_path)

        # Build global cons_id → icon mapping
        self._type_icons = {}
        for type_id, type_name in CONSUMABLE_TYPE_ID_MAP.items():
            candidates = CONSUMABLE_TYPE_TO_ICONS.get(type_name, [])
            for icon_name in candidates:
                if icon_name in all_icons:
                    self._type_icons[type_id] = all_icons[icon_name]
                    break

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        tracker = getattr(self.ctx.replay, "_tracker", None)
        if tracker is None:
            return

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
            activations = getattr(tracker, "_consumable_activations", {}).get(entity_id, [])
            active_icons: list[cairo.ImageSurface] = []
            for activated_at, cons_id, duration in activations:
                if activated_at <= timestamp < activated_at + duration:
                    icon = self._type_icons.get(cons_id)
                    if icon is not None:
                        active_icons.append(icon)

            if not active_icons:
                continue

            wx, _, wz = ship.position
            px, py = self.ctx.world_to_pixel(wx, wz)

            # Draw row
            n = len(active_icons)
            row_width = n * self.ICON_SIZE + (n - 1) * self.ICON_GAP
            start_x = px - row_width / 2
            icon_y = py + self.OFFSET_Y

            for i, icon_surface in enumerate(active_icons):
                icon_x = start_x + i * (self.ICON_SIZE + self.ICON_GAP)
                self._draw_icon(cr, icon_x, icon_y, icon_surface)

    def _draw_icon(
        self, cr: cairo.Context, x: float, y: float, surface: cairo.ImageSurface,
    ) -> None:
        w = surface.get_width()
        h = surface.get_height()
        scale = self.ICON_SIZE / max(w, h)

        cr.save()
        cr.translate(x, y)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, 0, 0)
        cr.paint_with_alpha(self.ACTIVE_ALPHA)
        cr.restore()
