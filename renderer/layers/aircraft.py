"""Renders aircraft (CV squadrons + airstrikes) on the minimap."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import cairo

from renderer.gamedata_resolver import resolve_json_cache
from renderer.layers.base import Layer, BaseRenderContext

log = logging.getLogger(__name__)


def _build_aircraft_icons(source_dir: Path) -> dict[str, Any]:
    """Scan split/Aircraft/*.json for params_id -> icon mapping.

    Cross-references Projectile split files for bomb species/ammoType.
    """
    # source_dir is split/Aircraft/
    projectile_dir = source_dir.parent / "Projectile"

    # Pre-load projectile data for cross-reference
    projectile_ammo: dict[str, str] = {}  # projectile_name -> ammoType
    if projectile_dir.exists():
        for f in projectile_dir.iterdir():
            if f.suffix != ".json":
                continue
            try:
                data = json.loads(f.read_text())
                name = data.get("name", "")
                ammo = data.get("ammoType", "")
                if name and ammo:
                    projectile_ammo[name] = ammo
            except (json.JSONDecodeError, ValueError):
                continue

    result = {}
    for f in source_dir.iterdir():
        if f.suffix != ".json":
            continue
        try:
            data = json.loads(f.read_text())
            pid = data.get("id")
            if pid is None:
                continue

            species = ""
            typeinfo = data.get("typeinfo", {})
            if isinstance(typeinfo, dict):
                species = typeinfo.get("species", "")

            # Determine icon base from species and bomb type
            bomb_name = data.get("bombName", "")
            plane_subtype = data.get("planeSubtype", "")

            # Icon mapping logic (matches generate_from_gameparams.py)
            icon_base = "bomber_depth_charge"  # default

            if species == "Fighter":
                icon_base = "fighter"
            elif species == "Scout":
                icon_base = "scout"
            elif species in ("Dive", "DiveBomber"):
                ammo = projectile_ammo.get(bomb_name, "")
                if ammo == "CS_SKIP_BOMB":
                    icon_base = "bomber_skip"
                else:
                    icon_base = "bomber"
            elif species in ("Torpedo", "TorpedoBomber"):
                icon_base = "torpedo"
            elif plane_subtype == "DepthCharge" or species == "DepthCharge":
                icon_base = "bomber_depth_charge"

            result[str(pid)] = icon_base
        except (json.JSONDecodeError, ValueError):
            continue

    return result


def _load_aircraft_icon_map(gamedata_path: Path) -> dict[int, str]:
    """Load params_id -> icon_base mapping from aircraft_icons.json.

    Uses the dynamic resolver: loads from JSON cache if fresh, otherwise
    rebuilds from split/Aircraft/ split files.
    """
    data = resolve_json_cache(
        gamedata_path / "aircraft_icons.json",
        gamedata_path / "split" / "Aircraft",
        _build_aircraft_icons,
    )
    return {int(k): v for k, v in data.items()}


class AircraftLayer(Layer):
    """Draws aircraft icons on the minimap.

    Controllable squadrons (CV-controlled) use icons from markers_minimap/plane/controllable/.
    Airstrikes use icons from markers_minimap/plane/airsupport/.
    Consumable aircraft (scout, smoke, fighter) use markers_minimap/plane/consumables/.
    """

    ICON_SCALE = 1.0  # relative to icon native size, at 760px reference

    def initialize(self, ctx: BaseRenderContext) -> None:
        super().initialize(ctx)

        gamedata = ctx.config.effective_gamedata_path
        plane_dir = gamedata / "gui" / "battle_hud" / "markers_minimap" / "plane"

        # Load params_id -> icon_base mapping
        self._icon_map = _load_aircraft_icon_map(gamedata)

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
