from __future__ import annotations

import math

import cairo

from renderer.layers.base import Layer, RenderContext


class CapturePointLayer(Layer):
    """Draws capture point circles with progress and team colors.

    Each capture point is rendered as a translucent circle whose fill
    color and opacity reflect the owning team and capture progress.
    A letter label (A, B, C, ...) is drawn at the center.
    """

    NEUTRAL_COLOR = (0.7, 0.7, 0.7, 0.3)
    CAP_LABELS = "ABCDEFGH"
    LABEL_FONT_SIZE = 14
    MAX_CAPTURE_POINTS = 1000

    _cap_positions: dict[int, tuple[float, float]]  # entity_id -> (px, py)
    _cap_radii: dict[int, float]  # entity_id -> pixel_radius
    _cap_order: list[int]  # entity_ids sorted left-to-right for label assignment

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)

        replay = ctx.replay
        map_size = ctx.map_size
        mm = ctx.config.minimap_size

        self._cap_positions = {}
        self._cap_radii = {}

        # Discover cap points from early game state (t=10s, after spawn)
        state = replay.state_at(10.0)
        entity_ids: list[int] = []

        for cap in state.battle.capture_points:
            eid = cap.entity_id
            entity_ids.append(eid)

            # Get position from tracker (caps don't move, so cache once)
            pos = None
            if hasattr(replay, "_tracker"):
                pos = replay._tracker.position_at(eid, 10.0)

            if pos is not None:
                wx, _, wz = pos
                px, py = ctx.world_to_pixel(wx, wz)
                self._cap_positions[eid] = (px, py)

            # Convert world radius to pixel radius
            pixel_radius = cap.radius / map_size * mm
            self._cap_radii[eid] = pixel_radius

        # Sort by x-position for consistent A/B/C labeling (left to right)
        entity_ids.sort(
            key=lambda eid: self._cap_positions.get(eid, (0, 0))[0]
        )
        self._cap_order = entity_ids

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        team_colors = config.team_colors

        for cap in state.battle.capture_points:
            eid = cap.entity_id
            pos = self._cap_positions.get(eid)
            if pos is None:
                continue

            px, py = pos
            radius = self._cap_radii.get(eid, 20.0)

            # Determine color based on controlling team and capture progress
            # Trap 5: map raw team ID to display team for correct colors
            progress = cap.capture_points / self.MAX_CAPTURE_POINTS
            control_team = self.ctx.raw_to_display_team(cap.control_team_id)

            if control_team in team_colors:
                r, g, b, _ = team_colors[control_team]
                fill_alpha = 0.15 + 0.25 * progress
                stroke_alpha = 0.4 + 0.4 * progress
            else:
                r, g, b = 0.7, 0.7, 0.7
                fill_alpha = 0.1
                stroke_alpha = 0.3

            # Fill circle
            cr.new_sub_path()
            cr.arc(px, py, radius, 0, 2 * math.pi)
            cr.set_source_rgba(r, g, b, fill_alpha)
            cr.fill_preserve()

            # Stroke circle
            cr.set_source_rgba(r, g, b, stroke_alpha)
            cr.set_line_width(2.0)
            cr.stroke()

            # Draw cap letter label
            label_idx = (
                self._cap_order.index(eid) if eid in self._cap_order else 0
            )
            label = self.CAP_LABELS[label_idx % len(self.CAP_LABELS)]

            cr.select_font_face(
                "sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD
            )
            cr.set_font_size(self.LABEL_FONT_SIZE)
            ext = cr.text_extents(label)

            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.move_to(px - ext.width / 2, py + ext.height / 2)
            cr.show_text(label)
