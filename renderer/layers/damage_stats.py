"""Self-player damage breakdown panel on the right side, above ribbons.

Shows DMG (dealt), SPOT (spotting), POT (potential) sections with
per-weapon-category breakdowns accumulated from DamageReceivedStatEvent.
All rows that will ever appear are shown from the start (with 0 values).
"""
from __future__ import annotations

import cairo

from renderer.layers.base import Layer, RenderContext, FONT_FAMILY

# damage_param → short display label
_CATEGORY_MAP: dict[str, str] = {
    param: cat
    for cat, params in {
        "HE":    ["MAIN_HE", "MAIN_CS"],
        "AP":    ["MAIN_AP"],
        "SEC":   ["ATBA_AP", "ATBA_HE", "ATBA_CS", "MAIN_AI_AP", "MAIN_AI_HE"],
        "TORP":  ["TORPEDO", "TORPEDO_DEEP", "TORPEDO_ALTER"],
        "FIRE":  ["BURN"],
        "FLOOD": ["FLOOD"],
        "ROCKET": ["ROCKET_HE", "ROCKET_AP", "ROCKET_HE_ASUP", "ROCKET_HE_ALTER",
                   "ROCKET_AP_ASUP", "ROCKET_AP_ALTER", "ROCKET_HE_TC", "ROCKET_AP_TC"],
        "BOMB":  ["BOMBER_AP", "BOMBER_HE", "TBOMBER",
                  "BOMBER_AP_ASUP", "BOMBER_HE_ASUP", "TBOMBER_ASUP",
                  "BOMBER_AP_ALTER", "BOMBER_HE_ALTER", "TBOMBER_ALTER",
                  "BOMBER_AP_TC", "BOMBER_HE_TC", "TBOMBER_TC"],
        "DC":    ["DEPTH_CHARGE", "DEPTH_CHARGE_ASUP", "DEPTH_CHARGE_ALTER",
                  "DEPTH_CHARGE_TC"],
        "RAM":   ["RAM"],
    }.items()
    for param in params
}

# Display order for subcategories
_CATEGORY_ORDER: dict[str, int] = {
    name: i for i, name in enumerate([
        "HE", "AP", "SEC", "TORP", "FIRE", "FLOOD",
        "ROCKET", "BOMB", "DC", "RAM", "OTHER",
    ])
}

# Section colors
_SECTION_COLORS: dict[str, tuple[float, float, float]] = {
    "ENEMY": (1.0, 0.75, 0.25),   # gold
    "SPOT":  (0.5, 0.85, 1.0),    # light blue
    "AGRO":  (0.85, 0.45, 0.45),  # soft red
}

_SECTION_LABELS: dict[str, str] = {
    "ENEMY": "Damage Dealt",
    "SPOT":  "Spotting Damage",
    "AGRO":  "Potential Damage",
}


def _fmt(value: float) -> str:
    """Format damage as compact string: 1234 → '1.2k', 46692 → '46.7k'."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))


class DamageStatsLayer(Layer):
    """Draws self-player damage breakdown at the top of the right panel."""

    HEADER_FONT_SIZE = 11
    SUB_FONT_SIZE = 9
    ROW_HEIGHT = 13
    SECTION_GAP = 4
    INDENT = 16
    PADDING = 8
    Y_START = 10  # offset from hud_height (at 760px reference)

    # Exposed so ribbons can position below
    panel_bottom: float = 0.0

    def measure_height(self, s: float) -> float:
        """Pre-compute total damage panel height (callable after initialize)."""
        row_h = self.ROW_HEIGHT * s
        section_gap = self.SECTION_GAP * s
        h = 0.0
        for stat_type in self._active_sections:
            cats = self._fixed_cats[stat_type]
            h += row_h  # header row
            if stat_type == "ENEMY" and len(cats) > 1:
                h += len(cats) * row_h
            h += section_gap
        if h > 0:
            h -= section_gap
        return h

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)

        from wows_replay_parser.events.models import DamageReceivedStatEvent

        self._events: list[DamageReceivedStatEvent] = sorted(
            (e for e in ctx.replay.events if isinstance(e, DamageReceivedStatEvent)),
            key=lambda e: e.timestamp,
        )
        self._ev_idx: int = 0
        self._accum: dict[str, dict[str, float]] = {
            "ENEMY": {}, "SPOT": {}, "AGRO": {},
        }

        # Pre-scan all events to discover which categories will appear per section
        self._fixed_cats: dict[str, list[str]] = {"ENEMY": [], "SPOT": [], "AGRO": []}
        seen: dict[str, set[str]] = {"ENEMY": set(), "SPOT": set(), "AGRO": set()}
        for ev in self._events:
            if ev.stat_type in seen:
                cat = _CATEGORY_MAP.get(ev.damage_param, "OTHER")
                seen[ev.stat_type].add(cat)
        for st in ("ENEMY", "SPOT", "AGRO"):
            self._fixed_cats[st] = sorted(
                seen[st],
                key=lambda c: _CATEGORY_ORDER.get(c, 99),
            )

        # Which sections have data at all
        self._active_sections = [st for st in ("ENEMY", "SPOT", "AGRO") if self._fixed_cats[st]]

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        if not self._active_sections:
            return

        # Accumulate deltas up to current timestamp
        while self._ev_idx < len(self._events):
            ev = self._events[self._ev_idx]
            if ev.timestamp > timestamp:
                break
            bucket = self._accum.get(ev.stat_type)
            if bucket is not None:
                bucket[ev.damage_param] = bucket.get(ev.damage_param, 0) + ev.delta_total
            self._ev_idx += 1

        config = self.ctx.config
        s = self.ctx.scale
        pad = self.PADDING * s
        panel_x = config.left_panel + config.minimap_size
        x_left = panel_x + pad
        # Position below PlayerHeaderLayer if present
        header_ref = getattr(self, "_header_ref", None)
        if header_ref is not None and header_ref.panel_bottom > 0:
            y = header_ref.panel_bottom
        else:
            y = config.hud_height + self.Y_START * s
        max_w = config.right_panel - 2 * pad
        x_right = x_left + max_w

        header_font = self.HEADER_FONT_SIZE * s
        sub_font = self.SUB_FONT_SIZE * s
        row_h = self.ROW_HEIGHT * s
        indent = self.INDENT * s
        section_gap = self.SECTION_GAP * s

        # Clip to right panel
        cr.save()
        cr.rectangle(panel_x, 0, config.right_panel, config.total_height)
        cr.clip()

        for stat_type in self._active_sections:
            cats = self._fixed_cats[stat_type]
            sr, sg, sb = _SECTION_COLORS[stat_type]

            # Compute current totals
            raw = self._accum[stat_type]
            cat_values: dict[str, float] = {}
            for cat in cats:
                cat_values[cat] = 0.0
            for param, dmg in raw.items():
                cat = _CATEGORY_MAP.get(param, "OTHER")
                cat_values[cat] = cat_values.get(cat, 0.0) + dmg
            total = sum(cat_values.values())

            # Header row: "DMG" left, total right
            cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(header_font)

            # Shadow
            cr.set_source_rgba(0, 0, 0, 0.9)
            cr.move_to(x_left + 1, y + header_font + 1)
            cr.show_text(_SECTION_LABELS[stat_type])
            # Label
            cr.set_source_rgb(sr, sg, sb)
            cr.move_to(x_left, y + header_font)
            cr.show_text(_SECTION_LABELS[stat_type])

            # Total value (right-aligned)
            total_text = _fmt(total)
            ext = cr.text_extents(total_text)
            cr.set_source_rgba(0, 0, 0, 0.9)
            cr.move_to(x_right - ext.width + 1, y + header_font + 1)
            cr.show_text(total_text)
            cr.set_source_rgb(sr, sg, sb)
            cr.move_to(x_right - ext.width, y + header_font)
            cr.show_text(total_text)
            y += row_h

            # Subcategory rows (only for damage dealt)
            if stat_type == "ENEMY" and len(cats) > 1:
                cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
                cr.set_font_size(sub_font)

                for cat_label in cats:
                    cat_dmg = cat_values.get(cat_label, 0.0)

                    # Category name (indented, dimmed)
                    cr.set_source_rgba(0.65, 0.65, 0.65, 0.9)
                    cr.move_to(x_left + indent, y + sub_font)
                    cr.show_text(cat_label)

                    # Value (right-aligned, section color)
                    val_text = _fmt(cat_dmg)
                    ext = cr.text_extents(val_text)
                    cr.set_source_rgb(sr, sg, sb)
                    cr.move_to(x_right - ext.width, y + sub_font)
                    cr.show_text(val_text)
                    y += row_h

            y += section_gap

        cr.restore()

        # Expose bottom y so ribbons can position below
        self.panel_bottom = y
