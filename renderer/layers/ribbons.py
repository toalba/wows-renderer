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
    (0,  [15, 14, 16, 17, 28]),      # Main Battery Hit → Pen, Overpen, Shatter, Ricochet, Bulge
    (13, []),                        # Secondary Hit
    (2,  [21, 20, 22, 23, 29]),      # Bomb Hit → Pen, Overpen, Shatter, Ricochet, Bulge
    (24, [25, 35, 26, 34, 30]),      # Rocket Hit → Pen, Overpen, Shatter, Ricochet, Bulge
    (3,  []),                        # Plane Shot Down
    (10, []),                        # Base Capture
    (11, []),                        # Capture Assist
    (9,  []),                        # Base Defense
    (12, []),                        # Suppressed
    (18, []),                        # Building Destroyed
]

_SUB_RIBBON_IDS = {14, 15, 16, 17, 20, 21, 22, 23, 25, 26, 28, 29, 30, 34, 35}

# Sub-ribbon → parent ribbon
_SUB_TO_PARENT: dict[int, int] = {
    14: 0, 15: 0, 16: 0, 17: 0, 28: 0,
    20: 2, 21: 2, 22: 2, 23: 2, 29: 2,
    25: 24, 26: 24, 34: 24, 35: 24, 30: 24,
}

# Parent → ordered list of sub-ribbon IDs
_PARENT_SUBS: dict[int, list[int]] = {
    0:  [15, 14, 16, 17, 28],
    2:  [21, 20, 22, 23, 29],
    24: [25, 35, 26, 34, 30],
}

def _build_icon_paths(gui_dir: Path) -> dict[int, str]:
    """Derive icon paths from parser ribbon names, checking both directories.

    Tries ribbons/ribbon_{name}.png first, then ribbons/subribbons/subribbon_{name}.png.
    """
    from wows_replay_parser.ribbons import RIBBON_WIRE_IDS

    paths: dict[int, str] = {}
    for rid, name in RIBBON_WIRE_IDS.items():
        fname = name.lower()
        candidates = [
            f"ribbons/ribbon_{fname}.png",
            f"ribbons/subribbons/subribbon_{fname}.png",
        ]
        if rid in _SUB_RIBBON_IDS:
            candidates.reverse()
        for rel in candidates:
            if (gui_dir / rel).exists():
                paths[rid] = rel
                break
    return paths

def _build_ribbon_labels() -> dict[int, str]:
    """Derive short fallback labels from parser ribbon names."""
    from wows_replay_parser.ribbons import RIBBON_WIRE_IDS

    return {rid: name.replace("_", " ")[:8] for rid, name in RIBBON_WIRE_IDS.items()}


class RibbonLayer(Layer):
    """Shows accumulated ribbon counts on the right panel."""

    MAIN_HEIGHT = 24     # display height for main ribbons
    SUB_HEIGHT = 18      # display height for sub-ribbons
    GAP = 3              # gap between items
    COUNT_FONT_SIZE = 10

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)

        raw = ctx.replay.recording_player_ribbons()
        self._timeline: list[tuple[float, int]] = [
            (r.timestamp, r.ribbon_id) for r in raw
        ]
        self._counts: dict[int, int] = {}
        self._tl_idx: int = 0
        self._seen_order: list[int] = []  # parent IDs in order of first appearance

        # Load icons
        gui_dir = Path(ctx.config.gamedata_path) / "gui"
        self._icons: dict[int, cairo.ImageSurface] = {}
        icon_paths = _build_icon_paths(gui_dir)
        self._labels = _build_ribbon_labels()
        for rid, rel_path in icon_paths.items():
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
            label = self._labels.get(rid, "?")
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
