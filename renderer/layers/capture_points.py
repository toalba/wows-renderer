from __future__ import annotations

import math

import cairo

from renderer.layers.base import Layer, RenderContext, FONT_FAMILY


class CapturePointLayer(Layer):
    """Draws capture point circles with team colors, progress arcs, and labels.

    Visual states:
    - Neutral (gray): no team owns it, no one inside
    - Owned (team color fill): a team controls the point
    - Being captured (progress arc): invader team color arc shows progress
    - Contested (both_inside): flashing/dashed border indicates blocked capture
    """

    NEUTRAL_COLOR = (0.7, 0.7, 0.7)
    CONTESTED_COLOR = (1.0, 0.85, 0.0)  # Yellow for contested
    CAP_LABELS = "ABCDEFGH"
    LABEL_FONT_SIZE = 22
    DEFAULT_RADIUS = 75.0

    _cap_positions: dict[int, tuple[float, float]]
    _cap_radii: dict[int, float]
    _cap_order: list[int]

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)

        replay = ctx.replay
        map_size = ctx.map_size
        mm = ctx.config.minimap_size
        tracker = replay._tracker if hasattr(replay, "_tracker") else None

        self._cap_positions = {}
        self._cap_radii = {}
        entity_ids: list[int] = []

        state = replay.state_at(10.0)
        for cap in state.battle.capture_points:
            eid = cap.entity_id
            entity_ids.append(eid)
            if tracker:
                pos = tracker.position_at(eid, 10.0)
                if pos is not None:
                    wx, _, wz = pos
                    self._cap_positions[eid] = ctx.world_to_pixel(wx, wz)
            pixel_radius = cap.radius / map_size * mm if cap.radius > 0 else self.DEFAULT_RADIUS / map_size * mm
            self._cap_radii[eid] = pixel_radius

        if not entity_ids and tracker:
            for eid, etype in tracker._entity_types.items():
                if etype in ("InteractiveObject", "InteractiveZone"):
                    pos = tracker.position_at(eid, 0.1)
                    if pos is not None:
                        wx, _, wz = pos
                        self._cap_positions[eid] = ctx.world_to_pixel(wx, wz)
                        self._cap_radii[eid] = self.DEFAULT_RADIUS / map_size * mm
                        entity_ids.append(eid)

        def sort_key(eid):
            for cap in state.battle.capture_points:
                if cap.entity_id == eid and cap.point_index >= 0:
                    return cap.point_index
            return self._cap_positions.get(eid, (0, 0))[0]

        entity_ids.sort(key=sort_key)
        self._cap_order = entity_ids

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        team_colors = config.team_colors

        cap_states = {}
        for cap in state.battle.capture_points:
            cap_states[cap.entity_id] = cap

        for eid in self._cap_order:
            pos = self._cap_positions.get(eid)
            if pos is None:
                continue

            px, py = pos
            radius = self._cap_radii.get(eid, 20.0)
            cap = cap_states.get(eid)

            # Determine owner color
            owner_r, owner_g, owner_b = self.NEUTRAL_COLOR
            if cap and cap.team_id >= 0:
                display_team = self.ctx.raw_to_display_team(cap.team_id)
                if display_team in team_colors:
                    owner_r, owner_g, owner_b, _ = team_colors[display_team]

            # Fill circle with owner color
            cr.new_sub_path()
            cr.arc(px, py, radius, 0, 2 * math.pi)
            cr.set_source_rgba(owner_r, owner_g, owner_b, 0.15)
            cr.fill()

            # Border: solid owner color
            cr.new_sub_path()
            cr.arc(px, py, radius, 0, 2 * math.pi)
            cr.set_source_rgba(owner_r, owner_g, owner_b, 0.5)
            cr.set_line_width(2.0)
            cr.stroke()

            # Progress arc (if being captured by the opposing team)
            # Skip during pre-battle (battleStage != 0) — initial state can be stale
            # Skip when invader == owner (own team inside their pre-owned zone)
            battle_active = state.battle.battle_stage == 0
            being_captured = (cap and cap.progress > 0.01 and cap.has_invaders
                              and cap.invader_team != cap.team_id)
            if battle_active and being_captured:
                inv_r, inv_g, inv_b = self.NEUTRAL_COLOR
                if cap.invader_team >= 0:
                    inv_display = self.ctx.raw_to_display_team(cap.invader_team)
                    if inv_display in team_colors:
                        inv_r, inv_g, inv_b, _ = team_colors[inv_display]

                # Draw progress arc (clockwise from top)
                start_angle = -math.pi / 2
                end_angle = start_angle + 2 * math.pi * cap.progress

                # Thick progress arc on the border
                cr.new_sub_path()
                cr.arc(px, py, radius, start_angle, end_angle)
                cr.set_source_rgba(inv_r, inv_g, inv_b, 0.9)
                cr.set_line_width(4.0)
                cr.stroke()

                # Inner progress fill (subtle)
                if cap.progress > 0.05:
                    cr.new_sub_path()
                    cr.move_to(px, py)
                    cr.arc(px, py, radius * 0.9, start_angle, end_angle)
                    cr.close_path()
                    cr.set_source_rgba(inv_r, inv_g, inv_b, 0.12)
                    cr.fill()

            # Contested indicator (both teams inside)
            if cap and cap.both_inside:
                cr.new_sub_path()
                cr.arc(px, py, radius + 3, 0, 2 * math.pi)
                yr, yg, yb = self.CONTESTED_COLOR
                cr.set_source_rgba(yr, yg, yb, 0.7)
                cr.set_line_width(2.0)
                cr.set_dash([6, 4])
                cr.stroke()
                cr.set_dash([])  # Reset dash

            # Cap letter label with halo
            label_idx = self._cap_order.index(eid) if eid in self._cap_order else 0
            label = self.CAP_LABELS[label_idx % len(self.CAP_LABELS)]

            cr.select_font_face(
                FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD
            )
            cr.set_font_size(self.LABEL_FONT_SIZE)
            ext = cr.text_extents(label)

            s = self.ctx.scale
            self.draw_text_halo(
                cr, px - ext.width / 2, py + ext.height / 2, label,
                owner_r, owner_g, owner_b, alpha=0.9,
                font_size=self.LABEL_FONT_SIZE * s, bold=True, outline_width=3.5 * s,
            )
