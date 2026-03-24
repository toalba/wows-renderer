from __future__ import annotations
import cairo
from renderer.layers.base import Layer, RenderContext


class HudLayer(Layer):
    """Draws the score bar, timer, and ship counts overlay."""

    SCORE_BAR_HEIGHT = 24
    TIMER_FONT_SIZE = 16
    SCORE_FONT_SIZE = 14
    COUNT_FONT_SIZE = 12
    MAX_SCORE = 1000

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        panel_w = config.panel_width
        mm_size = config.minimap_size

        scores = state.battle.team_scores
        time_left = state.battle.time_left

        # Count alive ships per team (count all players, not just visible)
        alive = {0: 0, 1: 0}
        player_lookup = self.ctx.player_lookup
        for entity_id, ship in state.ships.items():
            if ship.is_alive:
                player = player_lookup.get(entity_id)
                team = player.team_id if player else 0
                if team in alive:
                    alive[team] += 1

        self._draw_score_bar(cr, panel_w, mm_size, scores, config.team_colors)
        self._draw_ship_counts(cr, panel_w, mm_size, alive, config.team_colors)
        self._draw_timer(cr, panel_w, mm_size, time_left)

    def _draw_score_bar(self, cr, panel_w, mm_size, scores, team_colors):
        h = self.SCORE_BAR_HEIGHT
        y = 0

        score_0 = scores.get(0, 0)
        score_1 = scores.get(1, 0)
        total = max(score_0 + score_1, 1)

        # Team 0 bar (left portion)
        r0, g0, b0, _ = team_colors.get(0, (0.33, 0.85, 0.33, 1.0))
        frac_0 = score_0 / self.MAX_SCORE
        w0 = frac_0 * mm_size
        cr.set_source_rgba(r0, g0, b0, 0.75)
        cr.rectangle(panel_w, y, w0, h)
        cr.fill()

        # Team 1 bar (right portion, from right edge)
        r1, g1, b1, _ = team_colors.get(1, (0.90, 0.25, 0.25, 1.0))
        frac_1 = score_1 / self.MAX_SCORE
        w1 = frac_1 * mm_size
        cr.set_source_rgba(r1, g1, b1, 0.75)
        cr.rectangle(panel_w + mm_size - w1, y, w1, h)
        cr.fill()

        # Dark background for remaining center gap
        cr.set_source_rgba(0.1, 0.1, 0.1, 0.6)
        cr.rectangle(panel_w + w0, y, mm_size - w0 - w1, h)
        cr.fill()

        # Score text
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(self.SCORE_FONT_SIZE)

        # Team 0 score (left quarter)
        text_0 = str(score_0)
        ext_0 = cr.text_extents(text_0)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(panel_w + mm_size * 0.15 - ext_0.width / 2, y + h / 2 + ext_0.height / 2)
        cr.show_text(text_0)

        # Team 1 score (right quarter)
        text_1 = str(score_1)
        ext_1 = cr.text_extents(text_1)
        cr.move_to(panel_w + mm_size * 0.85 - ext_1.width / 2, y + h / 2 + ext_1.height / 2)
        cr.show_text(text_1)

    def _draw_ship_counts(self, cr, panel_w, mm_size, alive, team_colors):
        h = self.SCORE_BAR_HEIGHT
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(self.COUNT_FONT_SIZE)

        # Team 0 count (left edge of score bar)
        text_0 = str(alive.get(0, 0))
        ext = cr.text_extents(text_0)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(panel_w + 6, h / 2 + ext.height / 2)
        cr.show_text(text_0)

        # Team 1 count (right edge of score bar)
        text_1 = str(alive.get(1, 0))
        ext = cr.text_extents(text_1)
        cr.move_to(panel_w + mm_size - ext.width - 6, h / 2 + ext.height / 2)
        cr.show_text(text_1)

    def _draw_timer(self, cr, panel_w, mm_size, time_left):
        minutes = int(time_left) // 60
        seconds = int(time_left) % 60
        timer_text = f"{minutes:02d}:{seconds:02d}"

        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(self.TIMER_FONT_SIZE)
        ext = cr.text_extents(timer_text)

        # Position below score bar, centered
        tx = panel_w + mm_size / 2 - ext.width / 2
        ty = self.SCORE_BAR_HEIGHT + 20

        # Dark background pill
        pad_x, pad_y = 8, 4
        pill_x = tx - pad_x
        pill_y = ty - ext.height - pad_y
        pill_w = ext.width + pad_x * 2
        pill_h = ext.height + pad_y * 2

        cr.set_source_rgba(0, 0, 0, 0.55)
        # Rounded rectangle
        radius = pill_h / 2
        cr.new_sub_path()
        cr.arc(pill_x + pill_w - radius, pill_y + radius, radius, -1.5708, 0)
        cr.arc(pill_x + pill_w - radius, pill_y + pill_h - radius, radius, 0, 1.5708)
        cr.arc(pill_x + radius, pill_y + pill_h - radius, radius, 1.5708, 3.14159)
        cr.arc(pill_x + radius, pill_y + radius, radius, 3.14159, 4.71239)
        cr.close_path()
        cr.fill()

        # Timer text
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(tx, ty)
        cr.show_text(timer_text)
