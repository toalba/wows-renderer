"""Renders aircraft (CV squadrons + airstrikes) on the minimap."""
from __future__ import annotations

from pathlib import Path

import cairo

from renderer.layers.base import Layer, RenderContext


class AircraftLayer(Layer):
    """Draws aircraft icons on the minimap.

    Controllable squadrons (CV-controlled) use icons from markers_minimap/plane/controllable/.
    Airstrikes use icons from markers_minimap/plane/airsupport/.
    """

    ICON_SCALE = 1.0  # relative to icon native size, at 760px reference

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)

        plane_dir = Path(ctx.config.gamedata_path) / "gui" / "battle_hud" / "markers_minimap" / "plane"

        # Load controllable squadron icons: generic fighter for now
        # Keys: (squadron_type, team_variant)
        self._icons: dict[tuple[str, str], cairo.ImageSurface] = {}

        # Controllable: use fighter_he as generic squadron icon
        for variant, filename in [
            ("ally", "fighter_he_ally.png"),
            ("enemy", "fighter_he_enemy.png"),
            ("own", "fighter_he_own.png"),
        ]:
            path = plane_dir / "controllable" / filename
            if path.exists():
                try:
                    self._icons[("controllable", variant)] = cairo.ImageSurface.create_from_png(str(path))
                except Exception:
                    pass

        # Airsupport: use bomber_he
        for variant, filename in [
            ("ally", "bomber_he_ally.png"),
            ("enemy", "bomber_he_enemy.png"),
            ("own", "bomber_he_own.png"),
        ]:
            path = plane_dir / "airsupport" / filename
            if path.exists():
                try:
                    self._icons[("airstrike", variant)] = cairo.ImageSurface.create_from_png(str(path))
                except Exception:
                    pass

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        if not hasattr(state, "aircraft") or not state.aircraft:
            return

        for plane_id, ac in state.aircraft.items():
            if not ac.is_active:
                continue

            px, py = self.ctx.world_to_pixel(ac.x, ac.z)

            # Determine team variant
            player_team = self.ctx._self_team_raw
            if ac.team_id == player_team:
                variant = "ally"
            else:
                variant = "enemy"

            sq_type = ac.squadron_type or "controllable"
            icon = self._icons.get((sq_type, variant))
            if icon is None:
                # Fallback to any available icon for this type
                icon = self._icons.get((sq_type, "ally")) or self._icons.get(("controllable", variant))

            if icon:
                self._draw_icon(cr, px, py, icon)
            else:
                self._draw_fallback(cr, px, py, variant)

    def _draw_icon(self, cr, px, py, surface):
        w = surface.get_width()
        h = surface.get_height()
        scale = self.ICON_SCALE * self.ctx.scale

        cr.save()
        cr.translate(px, py)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, -w / 2, -h / 2)
        cr.paint()
        cr.restore()

    def _draw_fallback(self, cr, px, py, variant):
        """Small diamond marker as fallback."""
        s = 5.0 * self.ctx.scale
        if variant == "enemy":
            cr.set_source_rgba(1.0, 0.3, 0.3, 0.8)
        else:
            cr.set_source_rgba(0.3, 0.8, 0.3, 0.8)

        cr.save()
        cr.translate(px, py)
        cr.rotate(0.7854)
        cr.rectangle(-s, -s, s * 2, s * 2)
        cr.fill()
        cr.restore()
