"""Renders aircraft (CV squadrons + airstrikes) on the minimap."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import cairo

from renderer.layers.base import Layer, RenderContext

log = logging.getLogger(__name__)


def _build_aircraft_icon_map(gamedata_path: Path) -> dict[int, str]:
    """Build params_id -> icon_base mapping from split Aircraft + Projectile JSONs.

    Icon base is the filename prefix before _{variant}.png, e.g. "fighter_he",
    "torpedo_regular", "bomber_ap", "scout", etc.
    """
    aircraft_dir = gamedata_path / "split" / "Aircraft"
    projectile_dir = gamedata_path / "split" / "Projectile"

    if not aircraft_dir.exists():
        return {}

    # Load projectile lookup: name -> (species, ammoType, isDeepWater)
    proj_db: dict[str, tuple[str, str, bool]] = {}
    if projectile_dir.exists():
        for f in projectile_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                name = data.get("name", "")
                if name:
                    proj_db[name] = (
                        data.get("typeinfo", {}).get("species", ""),
                        data.get("ammoType", ""),
                        bool(data.get("isDeepWater", False)),
                    )
            except Exception:
                continue

    # Build aircraft params_id -> icon_base
    result: dict[int, str] = {}
    for f in aircraft_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        params_id = data.get("id")
        if params_id is None:
            continue
        species = data.get("typeinfo", {}).get("species", "")
        bomb_name = data.get("bombName", "")
        proj_species, ammo_type, is_deep = proj_db.get(bomb_name, ("", "", False))

        ammo = ammo_type.lower() if ammo_type in ("HE", "AP") else "he"

        if proj_species == "DepthCharge":
            icon_base = "bomber_depth_charge"
        elif proj_species == "Torpedo":
            icon_base = "torpedo_deepwater" if is_deep else "torpedo_regular"
        elif species == "Bomber":  # torpedo bombers
            icon_base = "torpedo_deepwater" if is_deep else "torpedo_regular"
        elif species == "Dive":  # dive bombers
            icon_base = f"bomber_{ammo}"
        elif species == "Fighter":  # attack aircraft (rockets)
            icon_base = f"fighter_{ammo}"
        elif species == "Skip":
            icon_base = f"skip_{ammo}"
        elif species == "Scout":
            icon_base = "scout"
        elif species == "Auxiliary":
            icon_base = "auxiliary"
        elif species == "Smoke":
            icon_base = "smoke"
        else:
            icon_base = "fighter_he"

        result[params_id] = icon_base

    log.debug("Aircraft icon map: %d entries", len(result))
    return result


class AircraftLayer(Layer):
    """Draws aircraft icons on the minimap.

    Controllable squadrons (CV-controlled) use icons from markers_minimap/plane/controllable/.
    Airstrikes use icons from markers_minimap/plane/airsupport/.
    Consumable aircraft (scout, smoke, fighter) use markers_minimap/plane/consumables/.
    """

    ICON_SCALE = 1.0  # relative to icon native size, at 760px reference

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)

        gamedata = Path(ctx.config.gamedata_path)
        plane_dir = gamedata / "gui" / "battle_hud" / "markers_minimap" / "plane"

        # Build params_id -> icon_base mapping
        self._icon_map = _build_aircraft_icon_map(gamedata)

        # Load all icons from all three directories
        # Key: (dir_name, icon_base, variant) -> surface
        self._icons: dict[tuple[str, str, str], cairo.ImageSurface] = {}

        variants = ["ally", "enemy", "own"]
        dirs = {
            "controllable": plane_dir / "controllable",
            "airsupport": plane_dir / "airsupport",
            "consumables": plane_dir / "consumables",
        }

        for dir_name, dir_path in dirs.items():
            if not dir_path.exists():
                continue
            for variant in variants:
                for png in dir_path.glob(f"*_{variant}.png"):
                    icon_base = png.stem.removesuffix(f"_{variant}")
                    try:
                        self._icons[(dir_name, icon_base, variant)] = (
                            cairo.ImageSurface.create_from_png(str(png))
                        )
                    except Exception:
                        pass

        log.debug("Loaded %d aircraft icon variants", len(self._icons))

        # Map icon_base -> preferred directory (consumable types go to consumables/)
        self._consumable_bases = {"scout", "smoke", "fighter", "fighter_upgrade"}

    def _get_icon(
        self, params_id: int, squadron_type: str, variant: str,
    ) -> cairo.ImageSurface | None:
        """Look up the correct icon for an aircraft."""
        icon_base = self._icon_map.get(params_id)

        if icon_base:
            # Pick directory based on squadron type and icon base
            if squadron_type == "airstrike":
                icon_dir = "airsupport"
            elif icon_base in self._consumable_bases:
                icon_dir = "consumables"
            else:
                icon_dir = "controllable"
            icon = self._icons.get((icon_dir, icon_base, variant))
            if icon:
                return icon
            # Try other directories as fallback
            for d in ("controllable", "airsupport", "consumables"):
                icon = self._icons.get((d, icon_base, variant))
                if icon:
                    return icon

        # Fallback by squadron type (most airstrikes are ASW depth charges)
        if squadron_type == "airstrike":
            return self._icons.get(("airsupport", "bomber_depth_charge", variant))
        return self._icons.get(("controllable", "fighter_he", variant))

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        if not hasattr(state, "aircraft") or not state.aircraft:
            return

        for plane_id, ac in state.aircraft.items():
            if not ac.is_active:
                continue

            px, py = self.ctx.world_to_pixel(ac.x, ac.z)

            # Determine team variant
            display_team = self.ctx.raw_to_display_team(ac.team_id)
            variant = "ally" if display_team == 0 else "enemy"

            icon = self._get_icon(ac.params_id, ac.squadron_type or "controllable", variant)

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
        team_colors = self.ctx.config.team_colors
        display_team = 1 if variant == "enemy" else 0
        r, g, b, _ = team_colors.get(display_team, (0.5, 0.5, 0.5, 0.8))
        cr.set_source_rgba(r, g, b, 0.8)

        cr.save()
        cr.translate(px, py)
        cr.rotate(0.7854)
        cr.rectangle(-s, -s, s * 2, s * 2)
        cr.fill()
        cr.restore()
