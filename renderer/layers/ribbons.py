"""Ribbon counter display on the right panel for the recording player.

Shows accumulated ribbon counts as a persistent grouped layout matching the
in-game ribbon UI. Main ribbons display at full size with sub-ribbons
(pen types) shown smaller underneath their parent.
"""
from __future__ import annotations

from pathlib import Path

import cairo

from renderer.layers.base import Layer, RenderContext, FONT_FAMILY


# Ribbon groups: (parent_id, [sub_ids])
# Parent count = sum of children when children exist.
# Groups with no subs just show as a standalone ribbon.
_RIBBON_GROUPS: list[tuple[int, list[int]]] = [
    (5,  []),                        # Destroyed
    (8,  []),                        # Citadel
    (4,  []),                        # Critical Hit
    (6,  []),                        # Set on Fire
    (7,  []),                        # Caused Flooding
    (1,  []),                        # Torpedo Hit
    (19, []),                        # Detected
    (54, []),                        # Assist
    (0,  [15, 14, 16, 17]),          # Main Battery Hit → Pen, Overpen, Shatter, Ricochet
    (13, []),                        # Secondary Hit
    (2,  [21, 20, 22, 23]),          # Bomb Hit → Pen, Overpen, Shatter, Ricochet
    (24, [25, 35, 26, 34]),          # Rocket Hit → Pen, Overpen, Shatter, Ricochet
    (3,  []),                        # Plane Shot Down
    (10, []),                        # Base Capture
    (11, []),                        # Capture Assist
    (9,  []),                        # Base Defense
    (12, []),                        # Suppressed
    (18, []),                        # Building Destroyed
]

_SUB_RIBBON_IDS = {14, 15, 16, 17, 20, 21, 22, 23, 25, 26, 34, 35}

# Sub-ribbon → parent ribbon
_SUB_TO_PARENT: dict[int, int] = {
    14: 0, 15: 0, 16: 0, 17: 0,
    20: 2, 21: 2, 22: 2, 23: 2,
    25: 24, 26: 24, 34: 24, 35: 24,
}

# Parent → ordered list of sub-ribbon IDs
_PARENT_SUBS: dict[int, list[int]] = {
    0:  [15, 14, 16, 17],
    2:  [21, 20, 22, 23],
    24: [25, 35, 26, 34],
}

# Icon file paths relative to gui/ dir
_ICON_PATHS: dict[int, str] = {
    0:  "ribbons/ribbon_main_caliber.png",
    1:  "ribbons/ribbon_torpedo.png",
    2:  "ribbons/ribbon_bomb.png",
    3:  "ribbons/ribbon_plane.png",
    4:  "ribbons/ribbon_crit.png",
    5:  "ribbons/ribbon_frag.png",
    6:  "ribbons/ribbon_burn.png",
    7:  "ribbons/ribbon_flood.png",
    8:  "ribbons/ribbon_citadel.png",
    9:  "ribbons/ribbon_base_defense.png",
    10: "ribbons/ribbon_base_capture.png",
    11: "ribbons/ribbon_base_capture_assist.png",
    12: "ribbons/ribbon_suppressed.png",
    13: "ribbons/ribbon_secondary_caliber.png",
    14: "ribbons/subribbons/subribbon_main_caliber_over_penetration.png",
    15: "ribbons/subribbons/subribbon_main_caliber_penetration.png",
    16: "ribbons/subribbons/subribbon_main_caliber_no_penetration.png",
    17: "ribbons/subribbons/subribbon_main_caliber_ricochet.png",
    18: "ribbons/ribbon_building_kill.png",
    19: "ribbons/ribbon_detected.png",
    54: "ribbons/ribbon_assist.png",
    # Bomb sub-ribbons
    20: "ribbons/subribbons/subribbon_bomb_over_penetration.png",
    21: "ribbons/subribbons/subribbon_bomb_penetration.png",
    22: "ribbons/subribbons/subribbon_bomb_no_penetration.png",
    23: "ribbons/subribbons/subribbon_bomb_ricochet.png",
    # Rocket sub-ribbons
    25: "ribbons/subribbons/subribbon_rocket_penetration.png",
    26: "ribbons/subribbons/subribbon_rocket_no_penetration.png",
    34: "ribbons/subribbons/subribbon_rocket_ricochet.png",
    35: "ribbons/subribbons/subribbon_rocket_over_penetration.png",
}

# Fallback labels
_RIBBON_LABELS: dict[int, str] = {
    0: "HIT", 1: "TORP", 2: "BOMB", 3: "PLANE", 4: "CRIT",
    5: "FRAG", 6: "FIRE", 7: "FLOOD", 8: "CITADEL", 9: "DEF",
    10: "CAP", 11: "ASSIST", 12: "SUPP", 13: "SEC",
    14: "OVERPEN", 15: "PEN", 16: "SHATTER", 17: "RICOCHET",
    18: "BUILDING", 19: "SPOTTED", 54: "ASSIST",
}


class RibbonLayer(Layer):
    """Shows accumulated ribbon counts on the right panel."""

    MAIN_HEIGHT = 24     # display height for main ribbons
    SUB_HEIGHT = 18      # display height for sub-ribbons
    GAP = 3              # gap between items
    COUNT_FONT_SIZE = 10

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)

        from wows_replay_parser.ribbons import extract_recording_player_ribbons

        tracker = getattr(ctx.replay, "_tracker", None)
        if tracker is None:
            self._timeline = []
            return

        avatar_id = None
        for change in tracker._history:
            if change.property_name == "privateVehicleState":
                avatar_id = change.entity_id
                break

        if avatar_id is None:
            self._timeline = []
            return

        raw = extract_recording_player_ribbons(tracker._history, avatar_id)
        self._timeline: list[tuple[float, int]] = [
            (r.timestamp, r.ribbon_id) for r in raw
        ]
        self._counts: dict[int, int] = {}
        self._tl_idx: int = 0
        self._seen_order: list[int] = []  # parent IDs in order of first appearance

        # Load icons
        gui_dir = Path(ctx.config.gamedata_path) / "gui"
        self._icons: dict[int, cairo.ImageSurface] = {}
        for rid, rel_path in _ICON_PATHS.items():
            path = gui_dir / rel_path
            if path.exists():
                try:
                    self._icons[rid] = cairo.ImageSurface.create_from_png(str(path))
                except Exception:
                    pass

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        if not self._timeline:
            return

        # Accumulate and track first-appearance order of parent groups
        while self._tl_idx < len(self._timeline):
            t, rid = self._timeline[self._tl_idx]
            if t > timestamp:
                break
            self._counts[rid] = self._counts.get(rid, 0) + 1
            # Find which parent group this ribbon belongs to
            parent_id = _SUB_TO_PARENT.get(rid, rid)
            if parent_id not in self._seen_order:
                self._seen_order.append(parent_id)
            self._tl_idx += 1

        if not self._counts:
            return

        config = self.ctx.config
        s = self.ctx.scale
        main_h = self.MAIN_HEIGHT * s
        sub_h = self.SUB_HEIGHT * s
        gap = self.GAP * s

        x_start = config.left_panel + config.minimap_size + 8
        # Position below DamageStatsLayer if present
        y_start = getattr(self, "_dmg_stats_ref", None)
        if y_start is not None and y_start.panel_bottom > 0:
            y_start = y_start.panel_bottom + 6 * s
        else:
            y_start = config.hud_height + 10
        max_x = config.total_width - 4

        cr.save()
        clip_x = config.left_panel + config.minimap_size
        cr.rectangle(clip_x, 0, config.right_panel, config.total_height)
        cr.clip()

        # Draw each group as a column: parent on top, subs below
        # Use first-appearance order so new ribbons append to the end
        x = x_start
        y_row = y_start
        row_max_h = main_h  # tallest column in current row

        for parent_id in self._seen_order:
            sub_ids = _PARENT_SUBS.get(parent_id, [])
            active_subs = [(sid, self._counts.get(sid, 0)) for sid in sub_ids if self._counts.get(sid, 0) > 0]
            if active_subs:
                parent_count = sum(cnt for _, cnt in active_subs)
            else:
                parent_count = self._counts.get(parent_id, 0)

            if parent_count == 0:
                continue

            # Measure column width (widest of parent + sub row)
            parent_w = self._ribbon_width(parent_id, main_h)
            sub_row_w = sum(self._ribbon_width(sid, sub_h) + gap for sid, _ in active_subs) - gap if active_subs else 0
            col_w = max(parent_w, sub_row_w)

            # Column height depends on whether it has active subs
            col_h = main_h + (gap + sub_h if active_subs else 0)

            # Wrap to next row if needed
            if x + col_w > max_x and x > x_start:
                x = x_start
                y_row += row_max_h + gap
                row_max_h = main_h

            row_max_h = max(row_max_h, col_h)

            # Draw parent ribbon
            self._draw_ribbon(cr, x, y_row, parent_id, parent_count, main_h, s)

            # Draw sub-ribbons below parent
            if active_subs:
                sx = x
                for sid, scnt in active_subs:
                    sw = self._ribbon_width(sid, sub_h)
                    self._draw_ribbon(cr, sx, y_row + main_h + gap, sid, scnt, sub_h, s)
                    sx += sw + gap

            x += col_w + gap * 2

        cr.restore()

    def _ribbon_width(self, rid: int, height: float) -> float:
        """Estimate rendered width for a ribbon at given height."""
        icon = self._icons.get(rid)
        if icon:
            return icon.get_width() * (height / icon.get_height())
        return height * 2.5

    def _draw_ribbon(self, cr, x, y, rid, count, height, s) -> float:
        """Draw a single ribbon icon with count badge. Returns x + width."""
        icon = self._icons.get(rid)

        if icon:
            iw = icon.get_width()
            ih = icon.get_height()
            scale = height / ih
            draw_w = iw * scale
            cr.save()
            cr.translate(x, y)
            cr.scale(scale, scale)
            cr.set_source_surface(icon, 0, 0)
            cr.paint()
            cr.restore()
        else:
            # Fallback rectangle
            label = _RIBBON_LABELS.get(rid, "?")
            draw_w = height * 2.5
            cr.set_source_rgba(0.3, 0.3, 0.3, 0.6)
            cr.rectangle(x, y, draw_w, height)
            cr.fill()
            cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(8 * s)
            cr.set_source_rgba(0.9, 0.9, 0.9, 1.0)
            ext = cr.text_extents(label)
            cr.move_to(x + (draw_w - ext.width) / 2, y + height / 2 + ext.height / 2)
            cr.show_text(label)

        # Count badge (bottom-right)
        badge_text = f"×{count}"
        font_size = self.COUNT_FONT_SIZE * s
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font_size)
        ext = cr.text_extents(badge_text)
        bx = x + draw_w - ext.width - 2
        by = y + height - 2
        # Text shadow for readability (no background box)
        cr.set_source_rgba(0, 0, 0, 0.9)
        cr.move_to(bx + 1, by + 1)
        cr.show_text(badge_text)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(bx, by)
        cr.show_text(badge_text)

        return x + draw_w
