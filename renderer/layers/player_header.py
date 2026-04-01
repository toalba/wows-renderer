"""Self-player header at the top of the right panel.

Shows ship silhouette (HP-colored fill + healable segment) alongside
[ClanTag] PlayerName on one line, with a subtle background box.
"""
from __future__ import annotations

from pathlib import Path

import cairo

from renderer.assets import get_ship_display_name, load_ship_consumables, load_ships_db
from renderer.layers.base import Layer, RenderContext, FONT_FAMILY


def _hp_color(fraction: float) -> tuple[float, float, float]:
    """Green → yellow → red based on HP fraction."""
    if fraction > 0.8:
        return (0.0, 0.9, 0.0)
    elif fraction > 0.5:
        # Green → yellow
        t = (0.8 - fraction) / 0.3
        return (t, 0.9, 0.0)
    elif fraction > 0.25:
        # Yellow → orange
        t = (0.5 - fraction) / 0.25
        return (1.0, 0.9 - t * 0.5, 0.0)
    else:
        # Orange → red
        t = (0.25 - fraction) / 0.25
        return (1.0, 0.4 - t * 0.4, 0.0)


class PlayerHeaderLayer(Layer):
    """Draws self-player ship silhouette + name at the top of the right panel.

    Exposes `panel_bottom` so downstream layers (DamageStatsLayer) can
    position themselves below.
    """

    PADDING = 8
    Y_START = 0       # offset from hud_height (at 760px reference)
    SIL_HEIGHT = 48   # silhouette display height — big, spans panel width
    NAME_FONT_SIZE = 10
    CLAN_FONT_SIZE = 9
    HP_FONT_SIZE = 10
    SHIP_NAME_FONT_SIZE = 10
    BG_ALPHA = 0.5
    BG_CORNER_RADIUS = 3

    panel_top: float = 0.0
    panel_bottom: float = 0.0

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)

        ship_db = ctx.ship_db or {}
        ship_consumables = load_ship_consumables(ctx.config.gamedata_path)

        # Find self player
        self._self_eid: int | None = None
        self._player_name: str = ""
        self._clan_tag: str = ""
        self._clan_color: tuple[float, float, float] = (0.9, 0.8, 0.4)  # default gold
        self._max_health: int = 0
        self._has_heal: bool = False
        self._ship_index: str = ""
        self._ship_name: str = ""

        for eid, player in ctx.player_lookup.items():
            if player.relation == 0:
                self._self_eid = eid
                self._player_name = player.name
                self._clan_tag = getattr(player, "clan_tag", "") or ""
                raw_color = getattr(player, "clan_color", None)
                if raw_color and isinstance(raw_color, int):
                    self._clan_color = (
                        ((raw_color >> 16) & 0xFF) / 255.0,
                        ((raw_color >> 8) & 0xFF) / 255.0,
                        (raw_color & 0xFF) / 255.0,
                    )
                self._max_health = player.max_health

                # Ship index + display name
                if player.ship_id and player.ship_id in ship_db:
                    self._ship_index = ship_db[player.ship_id].get("index", "")
                    self._ship_name = get_ship_display_name(
                        ctx.config.gamedata_path, self._ship_index,
                    )

                # Repair party check
                cons = ship_consumables.get(player.ship_id)
                if cons is not None:
                    self._has_heal = cons.get("has_repair_party", False)
                break

        # Load silhouette images
        self._sil_fg: cairo.ImageSurface | None = None
        self._sil_bg: cairo.ImageSurface | None = None
        self._sil_dead: cairo.ImageSurface | None = None

        if self._ship_index:
            bar_dir = Path(ctx.config.gamedata_path) / "gui" / "ship_bars"
            for suffix, attr in [
                ("_h.png", "_sil_fg"),
                ("_h_bg.png", "_sil_bg"),
                ("_h_bgdead.png", "_sil_dead"),
            ]:
                path = bar_dir / f"{self._ship_index}{suffix}"
                if path.exists():
                    try:
                        setattr(self, attr, cairo.ImageSurface.create_from_png(str(path)))
                    except Exception:
                        pass

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        if self._self_eid is None:
            return

        config = self.ctx.config
        s = self.ctx.scale
        pad = self.PADDING * s
        panel_x = config.left_panel + config.minimap_size
        x_left = panel_x + pad
        y_top = self.Y_START * s
        max_w = config.right_panel - 2 * pad

        sil_h = self.SIL_HEIGHT * s
        name_font = self.NAME_FONT_SIZE * s
        clan_font = self.CLAN_FONT_SIZE * s
        hp_font = self.HP_FONT_SIZE * s
        ship_name_font = self.SHIP_NAME_FONT_SIZE * s

        # Get ship state
        ship = state.ships.get(self._self_eid)
        is_alive = ship.is_alive if ship else False
        hp = ship.health if ship else 0
        max_hp = self._max_health or (ship.max_health if ship else 1)
        regen_hp = ship.regeneration_health if ship and hasattr(ship, "regeneration_health") else 0
        fraction = max(0.0, min(1.0, hp / max_hp)) if max_hp > 0 else 0.0

        # Layout:
        #   ┌──────────────────────────────────────────┐
        #   │  [======= silhouette HP bar ========]    │
        #   │  SHIP NAME  47 435/59 250  [TTT] Player  │
        #   └──────────────────────────────────────────┘
        text_line_h = name_font + 4 * s

        # Clip to right panel
        cr.save()
        cr.rectangle(panel_x, 0, config.right_panel, config.total_height)
        cr.clip()

        x_right = x_left + max_w

        # Measure ship name + HP text width to size the silhouette
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(ship_name_font)
        name_text = self._ship_name.upper() if self._ship_name else ""
        name_ext = cr.text_extents(name_text)

        hp_str = f"{int(hp):,}".replace(",", " ")
        max_hp_str = f"{int(max_hp):,}".replace(",", " ")
        hp_text = f"{hp_str}/{max_hp_str}"
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(hp_font)
        hp_ext = cr.text_extents(hp_text)

        left_text_w = name_ext.x_advance + 6 * s + hp_ext.x_advance

        # Silhouette — left-aligned, scaled to match the ship name + HP width
        sil_y = y_top + pad * 0.5
        sil_x = x_left
        if self._sil_fg:
            fg_w = self._sil_fg.get_width()
            fg_h = self._sil_fg.get_height()
            # Scale to fit: use the larger of height-based or text-width-based scale
            sil_scale_h = sil_h / fg_h
            sil_scale_w = left_text_w / fg_w
            sil_scale_use = max(sil_scale_h, sil_scale_w)
            actual_sil_h = fg_h * sil_scale_use

        used_sil_h = actual_sil_h if self._sil_fg else sil_h
        total_h = used_sil_h + text_line_h + pad
        self.panel_top = y_top

        # Draw combined background (header + damage stats) BEFORE content
        dmg_ref = getattr(self, "_dmg_stats_ref", None)
        dmg_h = dmg_ref.measure_height(s) if dmg_ref else 0.0
        # Extra spacing between header text and damage stats
        gap = 4 * s
        bg_h = total_h + gap + dmg_h + pad if dmg_h > 0 else total_h
        r = self.BG_CORNER_RADIUS * s
        bx = panel_x + 2 * s
        bw = config.right_panel - 4 * s
        self._rounded_rect(cr, bx, y_top, bw, bg_h, r)
        cr.set_source_rgba(0.15, 0.15, 0.15, self.BG_ALPHA)
        cr.fill()

        if not is_alive and self._sil_dead:
            self._draw_silhouette_dead(cr, sil_x, sil_y, sil_h)
        elif self._sil_fg and self._sil_bg:
            self._draw_silhouette_hp(
                cr, sil_x, sil_y, used_sil_h, fraction, regen_hp, max_hp,
            )

        # Text line: SHIP NAME + HP centered under silhouette, [CLAN] Player right-aligned
        text_y = sil_y + used_sil_h + name_font + 2 * s

        # Left-align "SHIP NAME  HP" under the silhouette
        tx = x_left

        # Ship name
        if self._ship_name:
            cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(ship_name_font)
            cr.set_source_rgba(0, 0, 0, 0.8)
            cr.move_to(tx + 1, text_y + 1)
            cr.show_text(self._ship_name.upper())
            cr.set_source_rgba(0.85, 0.85, 0.85, 1.0)
            cr.move_to(tx, text_y)
            cr.show_text(self._ship_name.upper())
            tx += name_ext.x_advance + 6 * s

        # HP values (HP-colored)
        hp_r, hp_g, hp_b = _hp_color(fraction)
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(hp_font)
        cr.set_source_rgba(0, 0, 0, 0.8)
        cr.move_to(tx + 1, text_y + 1)
        cr.show_text(hp_text)
        cr.set_source_rgb(hp_r, hp_g, hp_b)
        cr.move_to(tx, text_y)
        cr.show_text(hp_text)
        tx += hp_ext.x_advance

        # [CLAN] PlayerName (right-aligned, clamped to not overlap HP text)
        # Build full string to measure
        player_parts = []
        if self._clan_tag:
            player_parts.append(f"[{self._clan_tag}] ")
        player_parts.append(self._player_name)

        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(name_font)
        full_text = "".join(player_parts)
        full_ext = cr.text_extents(full_text)
        px = max(tx + 4 * s, x_right - full_ext.width)

        if self._clan_tag:
            cr.set_font_size(clan_font)
            tag_text = f"[{self._clan_tag}] "
            cr.set_source_rgba(0, 0, 0, 0.8)
            cr.move_to(px + 1, text_y + 1)
            cr.show_text(tag_text)
            cr.set_source_rgba(*self._clan_color, 1.0)
            cr.move_to(px, text_y)
            cr.show_text(tag_text)
            tag_ext = cr.text_extents(tag_text)
            px += tag_ext.x_advance

        cr.set_font_size(name_font)
        cr.set_source_rgba(0, 0, 0, 0.8)
        cr.move_to(px + 1, text_y + 1)
        cr.show_text(self._player_name)
        cr.set_source_rgba(0.95, 0.95, 0.95, 1.0)
        cr.move_to(px, text_y)
        cr.show_text(self._player_name)

        cr.restore()

        self.panel_bottom = y_top + total_h + 4 * s

    def _draw_silhouette_hp(
        self, cr: cairo.Context, x: float, y: float, target_h: float,
        fraction: float, regen_hp: float, max_hp: int,
    ) -> float:
        """Draw ship silhouette with HP fill and healable segment. Returns drawn width.

        Renders to a temp surface so we can use ATOP to tint the silhouette.
        """
        fg = self._sil_fg
        bg = self._sil_bg
        fg_w, fg_h = fg.get_width(), fg.get_height()
        bg_w, bg_h = bg.get_width(), bg.get_height()
        scale = target_h / max(fg_h, bg_h)
        draw_w = max(fg_w, bg_w) * scale
        draw_h = target_h

        # Render silhouette to a temp surface for compositing
        tmp = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(draw_w + 2), int(draw_h + 2))
        tc = cairo.Context(tmp)

        # Full background outline (dim)
        tc.save()
        tc.scale(scale, scale)
        tc.set_source_surface(bg, 0, 0)
        tc.paint_with_alpha(0.25)
        tc.restore()

        # HP-filled portion: draw fg clipped to fraction, then tint with ATOP
        if fraction > 0:
            hp_w = draw_w * fraction
            r, g, b = _hp_color(fraction)

            # Draw fg silhouette clipped to HP width
            tc.save()
            tc.rectangle(0, 0, hp_w, draw_h)
            tc.clip()
            tc.scale(scale, scale)
            tc.set_source_surface(fg, 0, 0)
            tc.paint()
            tc.restore()

            # Tint: paint color ATOP (only where silhouette pixels exist)
            tc.save()
            tc.rectangle(0, 0, hp_w, draw_h)
            tc.clip()
            tc.set_operator(cairo.OPERATOR_ATOP)
            tc.set_source_rgba(r, g, b, 0.6)
            tc.paint()
            tc.restore()

        # Healable segment (lighter tint)
        if self._has_heal and regen_hp > 0 and max_hp > 0:
            heal_end = min(1.0, (fraction * max_hp + regen_hp) / max_hp)
            if heal_end > fraction:
                r, g, b = _hp_color(fraction)
                heal_start_px = draw_w * fraction
                heal_end_px = draw_w * heal_end

                tc.save()
                tc.rectangle(heal_start_px, 0, heal_end_px - heal_start_px, draw_h)
                tc.clip()
                tc.scale(scale, scale)
                tc.set_source_surface(fg, 0, 0)
                tc.paint_with_alpha(0.4)
                tc.restore()

                tc.save()
                tc.rectangle(heal_start_px, 0, heal_end_px - heal_start_px, draw_h)
                tc.clip()
                tc.set_operator(cairo.OPERATOR_ATOP)
                tc.set_source_rgba(r, g, b, 0.3)
                tc.paint()
                tc.restore()

        tmp.flush()

        # Blit temp surface onto main context
        cr.set_source_surface(tmp, x, y)
        cr.paint()

        return draw_w

    def _draw_silhouette_dead(
        self, cr: cairo.Context, x: float, y: float, target_h: float,
    ) -> float:
        """Draw dead ship silhouette. Returns drawn width."""
        dead = self._sil_dead
        dw, dh = dead.get_width(), dead.get_height()
        scale = target_h / dh
        draw_w = dw * scale

        cr.save()
        cr.translate(x, y)
        cr.scale(scale, scale)
        cr.set_source_surface(dead, 0, 0)
        cr.paint_with_alpha(0.7)
        cr.restore()

        return draw_w

    @staticmethod
    def _rounded_rect(
        cr: cairo.Context, x: float, y: float, w: float, h: float, r: float,
    ) -> None:
        import math
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
