from __future__ import annotations

import json
import math
from pathlib import Path

import cairo

from renderer.gamedata_resolver import resolve_json_cache
from renderer.layers.base import Layer, RenderContext, FONT_FAMILY


def _build_buff_drops(source_dir: Path) -> dict:
    """Scan split/Drop/*.json for paramsId → markerNameActive mapping."""
    result = {}
    for f in source_dir.iterdir():
        if f.suffix != ".json":
            continue
        try:
            data = json.loads(f.read_text())
            params_id = data.get("id")
            marker = data.get("markerNameActive", "")
            if params_id is not None and marker:
                result[str(params_id)] = marker
        except (json.JSONDecodeError, ValueError):
            continue
    return result


class CapturePointLayer(Layer):
    """Draws capture point circles with team colors, progress arcs, and labels.

    Supports standard domination, Arms Race (late-spawning shrinking zone +
    buff pickups), and PvE/operation zones that spawn mid-match.

    All zone data is queried live per frame from the game state — no init-time
    caching of positions or radii. InteractiveZone entities that haven't been
    created yet or have been destroyed are simply absent from the state.

    Zone types (from InteractiveZone.type property):
      9  = Main capture zone (Arms Race central point, shrinks over time)
      6  = Buff pickup zone (Arms Race collectibles, radius=48)
      12 = ASW/ward zone (carrier consumable, short-lived, skip rendering)
      0  = Standard capture point (domination/epicenter)
    """

    NEUTRAL_COLOR = (0.7, 0.7, 0.7)
    CONTESTED_COLOR = (1.0, 0.85, 0.0)
    BUFF_ALPHA = 0.25
    BUFF_BORDER_ALPHA = 0.5
    BUFF_RADIUS_PX = 8.0  # small fixed-size marker for buffs at 760px ref
    CAP_LABELS = "ABCDEFGH"
    LABEL_FONT_SIZE = 22
    DEFAULT_RADIUS = 75.0
    # Zone types to skip rendering (wards/ASW zones from carrier consumables)
    _SKIP_TYPES = {12}

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        # Pre-build a sorted cap order from early game state for label assignment.
        # This only determines the A/B/C/D letter mapping, not visibility.
        self._cap_label_order: list[int] = []
        self._build_label_order(ctx)
        # Buff icons and zone→paramsId mapping for Arms Race
        self._buff_icons: dict[str, cairo.ImageSurface] = {}
        self._buff_drops: dict[int, str] = {}  # paramsId -> marker name
        self._zone_buff_type: dict[int, str] = {}  # zone_eid -> marker name
        self._load_buff_data(ctx)

    def _build_label_order(self, ctx: RenderContext) -> None:
        """Assign cap letters by scanning states at a few timestamps."""
        tracker = ctx.replay.tracker
        seen: dict[int, tuple[float, int]] = {}  # eid -> (x_pos, point_index)

        for t in (10.0, 30.0, 60.0, 300.0, 600.0):
            state = ctx.replay.state_at(t)
            for cap in state.battle.capture_points:
                eid = cap.entity_id
                if eid in seen:
                    continue
                # Skip non-capture zones (buffs, wards)
                zone_type = self._get_zone_type(tracker, eid)
                if zone_type in self._SKIP_TYPES or zone_type == 6:
                    continue
                pos = tracker.position_at(eid, t)
                x = pos[0] if pos else 0.0
                idx = cap.point_index if cap.point_index >= 0 else 999
                seen[eid] = (x, idx)

        # Sort by point_index first, then x position
        self._cap_label_order = sorted(seen, key=lambda e: (seen[e][1], seen[e][0]))

    def _load_buff_data(self, ctx: RenderContext) -> None:
        """Load buff drop icons and build zone → buff type mapping."""
        gamedata = Path(ctx.config.gamedata_path)

        # Load paramsId → marker name via resolver (buff_drops.json cache, split/Drop/ source)
        drops_data = resolve_json_cache(
            gamedata / "buff_drops.json",
            gamedata / "split" / "Drop",
            _build_buff_drops,
        )
        self._buff_drops = {int(k): v for k, v in drops_data.items()}

        # Load buff marker icons from gui/powerups/drops/
        icon_dir = gamedata / "gui" / "powerups" / "drops"
        if icon_dir.exists():
            for png in icon_dir.glob("icon_marker_*_small.png"):
                # icon_marker_health_active_small.png → health_active
                name = png.stem.removeprefix("icon_marker_").removesuffix("_small")
                try:
                    self._buff_icons[name] = cairo.ImageSurface.create_from_png(str(png))
                except Exception:
                    pass

        # Build zone_eid → marker name from BattleLogic state property history.
        # Each change to the BattleLogic 'state' property includes drop.data
        # with active buff zones and their paramsIds.
        tracker = ctx.replay.tracker
        bl_eids = tracker.get_entities_by_type("BattleLogic")
        bl_eid = bl_eids[0] if bl_eids else None
        if bl_eid is None or not self._buff_drops:
            return

        for change in tracker.property_history(bl_eid, "state"):
            # Check both old_value (catches initial state) and new_value
            for val in (change.old_value, change.new_value):
                if not isinstance(val, dict):
                    continue
                drop = val.get("drop")
                if not isinstance(drop, dict):
                    continue
                data = drop.get("data", [])
                if not isinstance(data, list):
                    continue
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    zid = item.get("zoneId", 0)
                    pid = item.get("paramsId", 0)
                    if zid and pid and zid not in self._zone_buff_type:
                        marker = self._buff_drops.get(pid, "")
                        if marker:
                            self._zone_buff_type[zid] = marker

    def _get_zone_type(self, tracker, eid: int) -> int:
        """Get InteractiveZone type property (0 if unknown)."""
        props = tracker.get_entity_props(eid)
        return int(props.get("type", 0))

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        team_colors = config.team_colors
        tracker = self.ctx.replay.tracker
        map_size = self.ctx.map_size
        mm = config.minimap_size

        rendered_caps: list[int] = []

        for cap in state.battle.capture_points:
            eid = cap.entity_id
            zone_type = self._get_zone_type(tracker, eid)

            # Skip ward/ASW zones
            if zone_type in self._SKIP_TYPES:
                continue

            # Skip entities that have left the game (collected buffs, expired zones).
            # The tracker doesn't remove InteractiveZones from state after EntityLeave.
            leave_time = tracker.get_entity_leave_time(eid)
            if leave_time is not None and timestamp >= leave_time:
                continue

            # Get live position
            pos = tracker.position_at(eid, timestamp)
            if pos is None:
                continue
            wx, _, wz = pos
            px, py = self.ctx.world_to_pixel(wx, wz)

            # cap.radius is live from iter_states (supports shrinking)
            radius_world = cap.radius if cap.radius > 0 else self.DEFAULT_RADIUS

            if zone_type == 6:
                # Buff pickup — icon or diamond marker
                self._render_buff(cr, px, py, eid, cap, team_colors)
                continue

            # Don't render capture zones that aren't active yet (e.g. Arms Race
            # main zone before the timer expires). is_enabled is set by the server
            # when the zone becomes capturable.
            if not cap.is_enabled and cap.progress < 0.01 and cap.team_id < 0:
                continue

            pixel_radius = radius_world / map_size * mm
            rendered_caps.append(eid)

            # Determine owner color
            owner_r, owner_g, owner_b = self.NEUTRAL_COLOR
            if cap.team_id >= 0:
                display_team = self.ctx.raw_to_display_team(cap.team_id)
                if display_team in team_colors:
                    owner_r, owner_g, owner_b, _ = team_colors[display_team]

            # Fill circle with owner color
            cr.new_sub_path()
            cr.arc(px, py, pixel_radius, 0, 2 * math.pi)
            cr.set_source_rgba(owner_r, owner_g, owner_b, 0.15)
            cr.fill()

            # Border
            cr.new_sub_path()
            cr.arc(px, py, pixel_radius, 0, 2 * math.pi)
            cr.set_source_rgba(owner_r, owner_g, owner_b, 0.5)
            cr.set_line_width(2.0)
            cr.stroke()

            # Progress arc
            battle_active = state.battle.battle_stage == 0
            cap_active = cap.is_enabled
            being_captured = (cap_active and cap.progress > 0.01 and cap.has_invaders
                              and cap.invader_team != cap.team_id)
            if battle_active and being_captured:
                inv_r, inv_g, inv_b = self.NEUTRAL_COLOR
                if cap.invader_team >= 0:
                    inv_display = self.ctx.raw_to_display_team(cap.invader_team)
                    if inv_display in team_colors:
                        inv_r, inv_g, inv_b, _ = team_colors[inv_display]

                start_angle = -math.pi / 2
                end_angle = start_angle + 2 * math.pi * cap.progress

                cr.new_sub_path()
                cr.arc(px, py, pixel_radius, start_angle, end_angle)
                cr.set_source_rgba(inv_r, inv_g, inv_b, 0.9)
                cr.set_line_width(4.0)
                cr.stroke()

                if cap.progress > 0.05:
                    cr.new_sub_path()
                    cr.move_to(px, py)
                    cr.arc(px, py, pixel_radius * 0.9, start_angle, end_angle)
                    cr.close_path()
                    cr.set_source_rgba(inv_r, inv_g, inv_b, 0.12)
                    cr.fill()

            # Contested indicator
            if cap_active and cap.both_inside:
                cr.new_sub_path()
                cr.arc(px, py, pixel_radius + 3, 0, 2 * math.pi)
                yr, yg, yb = self.CONTESTED_COLOR
                cr.set_source_rgba(yr, yg, yb, 0.7)
                cr.set_line_width(2.0)
                cr.set_dash([6, 4])
                cr.stroke()
                cr.set_dash([])

            # Cap letter label
            if eid in self._cap_label_order:
                label_idx = self._cap_label_order.index(eid)
            else:
                label_idx = len(rendered_caps) - 1
            label = self.CAP_LABELS[label_idx % len(self.CAP_LABELS)]

            s = self.ctx.scale
            self.draw_text_halo(
                cr, px - self.LABEL_FONT_SIZE * s * 0.3, py + self.LABEL_FONT_SIZE * s * 0.35,
                label,
                owner_r, owner_g, owner_b, alpha=0.9,
                font_size=self.LABEL_FONT_SIZE * s, bold=True, outline_width=3.5 * s,
            )

    def _render_buff(
        self, cr: cairo.Context, px: float, py: float,
        eid: int, cap, team_colors: dict,
    ) -> None:
        """Render a buff pickup zone with icon or diamond fallback."""
        s = self.ctx.scale

        # Try to render buff icon
        marker = self._zone_buff_type.get(eid, "")
        icon = self._buff_icons.get(marker) if marker else None

        if icon:
            w = icon.get_width()
            h = icon.get_height()
            icon_scale = s * 0.9
            cr.save()
            cr.translate(px, py)
            cr.scale(icon_scale, icon_scale)
            cr.set_source_surface(icon, -w / 2, -h / 2)
            cr.paint()
            cr.restore()
        else:
            # Diamond fallback
            size = self.BUFF_RADIUS_PX * s
            r, g, b = self.NEUTRAL_COLOR
            if cap.team_id >= 0:
                display_team = self.ctx.raw_to_display_team(cap.team_id)
                if display_team in team_colors:
                    r, g, b, _ = team_colors[display_team]
            cr.save()
            cr.translate(px, py)
            cr.rotate(math.pi / 4)
            cr.rectangle(-size, -size, size * 2, size * 2)
            cr.set_source_rgba(r, g, b, self.BUFF_ALPHA)
            cr.fill_preserve()
            cr.set_source_rgba(r, g, b, self.BUFF_BORDER_ALPHA)
            cr.set_line_width(1.5 * s)
            cr.stroke()
            cr.restore()
