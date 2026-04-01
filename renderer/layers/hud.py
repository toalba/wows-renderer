from __future__ import annotations
import cairo
from renderer.layers.base import Layer, RenderContext, FONT_FAMILY


class HudLayer(Layer):
    """Draws the score bar, timer, ship counts, TTW, and match result overlay."""

    SCORE_BAR_HEIGHT = 24
    TIMER_FONT_SIZE = 19
    SCORE_FONT_SIZE = 17
    COUNT_FONT_SIZE = 14

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        # Scoring config (populated from first frame's BattleState)
        self._win_score: int = 1000
        self._kill_swing: int = 100  # default, updated from actual data
        self._hold_rate: float = 0.6  # pts/sec per cap held (reward/period)
        self._scoring_loaded: bool = False

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        panel_w = config.left_panel
        mm_size = config.minimap_size

        # Load scoring config once from BattleState
        if not self._scoring_loaded:
            battle = state.battle
            if battle.team_win_score > 0:
                self._win_score = battle.team_win_score
            if battle.kill_scoring:
                # Use most common kill swing (reward + penalty)
                swings = [ks.reward + ks.penalty for ks in battle.kill_scoring]
                self._kill_swing = max(swings)  # use worst case (CV swing)
            if battle.hold_scoring:
                hs = battle.hold_scoring[0]
                self._hold_rate = hs.reward / hs.period if hs.period > 0 else 0.6
            self._scoring_loaded = True

        # Remap raw team scores to display teams (Trap 5)
        raw_scores = state.battle.team_scores
        display_scores: dict[int, int] = {}
        for raw_tid, score in raw_scores.items():
            display_tid = self.ctx.raw_to_display_team(raw_tid)
            display_scores[display_tid] = score

        # Count caps held per display team → score rates
        # Contested caps (has_invaders=True) don't tick for the owner
        caps_held: dict[int, int] = {0: 0, 1: 0}
        for cp in state.battle.capture_points:
            if not cp.is_enabled:
                continue
            if cp.has_invaders or cp.both_inside:
                continue  # Enemy inside or contested — cap tick paused
            raw_tid = cp.team_id
            if raw_tid in (0, 1):
                display_tid = self.ctx.raw_to_display_team(raw_tid)
                if display_tid in caps_held:
                    caps_held[display_tid] += 1
        score_rates = {
            tid: caps_held[tid] * self._hold_rate for tid in (0, 1)
        }

        # Timer handling (Trap 10: BattleStage is inverted)
        # raw 1 = pre-battle countdown, raw 0 = battle active
        battle_stage = state.battle.battle_stage
        time_left = state.battle.time_left

        # Count alive ships per display team
        alive = {0: 0, 1: 0}
        player_lookup = self.ctx.player_lookup
        for entity_id, ship in state.ships.items():
            if ship.is_alive:
                player = player_lookup.get(entity_id)
                if player:
                    team = player.team_id
                else:
                    team = 0
                if team in alive:
                    alive[team] += 1

        # Check for match result
        result_winner = state.battle.battle_result_winner

        self._draw_score_bar(cr, panel_w, mm_size, display_scores, config.team_colors, score_rates, time_left)
        self._draw_timer(cr, panel_w, mm_size, time_left, battle_stage)
        self._draw_ttw(cr, panel_w, mm_size, display_scores, score_rates, time_left)
        self._draw_kill_swing(cr, panel_w, mm_size, display_scores, score_rates, time_left)

        if result_winner >= 0:
            self._draw_match_result(cr, panel_w, mm_size, result_winner)

    def _draw_score_bar(self, cr, panel_w, mm_size, scores, team_colors, score_rates, time_left):
        h = self.SCORE_BAR_HEIGHT
        y = 0

        score_0 = scores.get(0, 0)
        score_1 = scores.get(1, 0)
        rate_0 = score_rates[0]
        rate_1 = score_rates[1]

        half = mm_size / 2.0

        # Dark background — only over minimap
        cr.set_source_rgba(0.07, 0.07, 0.12, 0.9)
        cr.rectangle(panel_w, y, mm_size, h)
        cr.fill()

        # Team 0 bar (ally, left→center)
        r0, g0, b0, _ = team_colors.get(0, (0.33, 0.85, 0.33, 1.0))
        frac_0 = min(score_0 / self._win_score, 1.0)
        w0 = frac_0 * half
        cr.set_source_rgba(r0, g0, b0, 0.75)
        cr.rectangle(panel_w, y, w0, h)
        cr.fill()

        # Team 1 bar (enemy, right→center)
        r1, g1, b1, _ = team_colors.get(1, (0.90, 0.25, 0.25, 1.0))
        frac_1 = min(score_1 / self._win_score, 1.0)
        w1 = frac_1 * half
        cr.set_source_rgba(r1, g1, b1, 0.75)
        cr.rectangle(panel_w + mm_size - w1, y, w1, h)
        cr.fill()

        # Projected winner highlight
        winner = self._projected_winner(score_0, score_1, rate_0, rate_1, time_left)
        if winner == 0:
            cr.set_source_rgba(r0, g0, b0, 1.0)
            cr.set_line_width(2.0)
            cr.rectangle(panel_w, y, w0, h)
            cr.stroke()
        elif winner == 1:
            cr.set_source_rgba(r1, g1, b1, 1.0)
            cr.set_line_width(2.0)
            cr.rectangle(panel_w + mm_size - w1, y, w1, h)
            cr.stroke()

        # Score text + pts/sec
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(self.SCORE_FONT_SIZE)

        # Team 0 score (left quarter of minimap)
        rate_text_0 = f" +{rate_0:.1f}/s" if rate_0 > 0.01 else ""
        text_0 = f"{score_0}{rate_text_0}"
        ext_0 = cr.text_extents(text_0)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(panel_w + mm_size * 0.15 - ext_0.width / 2, y + h / 2 + ext_0.height / 2)
        cr.show_text(text_0)

        # Team 1 score (right quarter of minimap)
        rate_text_1 = f" +{rate_1:.1f}/s" if rate_1 > 0.01 else ""
        text_1 = f"{score_1}{rate_text_1}"
        ext_1 = cr.text_extents(text_1)
        cr.move_to(panel_w + mm_size * 0.85 - ext_1.width / 2, y + h / 2 + ext_1.height / 2)
        cr.show_text(text_1)

    def _draw_ship_counts(self, cr, total_w, mm_size, alive, team_colors):
        h = self.SCORE_BAR_HEIGHT
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(self.COUNT_FONT_SIZE)

        # Team 0 count (far left)
        text_0 = str(alive.get(0, 0))
        ext = cr.text_extents(text_0)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(8, h / 2 + ext.height / 2)
        cr.show_text(text_0)

        # Team 1 count (far right)
        text_1 = str(alive.get(1, 0))
        ext = cr.text_extents(text_1)
        cr.move_to(total_w - ext.width - 8, h / 2 + ext.height / 2)
        cr.show_text(text_1)

    def _draw_timer(self, cr, panel_w, mm_size, time_left, battle_stage):
        # Trap 10: BattleStage is inverted
        # raw 1 = pre-battle countdown → show countdown
        # raw 0 = battle active → show remaining time
        if battle_stage == 1:
            # Pre-battle countdown — show "WAITING" or countdown
            timer_text = f"--:-- ({time_left})"
        else:
            # Active battle — show time_left as MM:SS
            minutes = int(time_left) // 60
            seconds = int(time_left) % 60
            timer_text = f"{minutes:02d}:{seconds:02d}"

        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
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

    def _draw_ttw(self, cr, panel_w, mm_size, scores, score_rates, time_left):
        """Draw Time-to-Win estimates as pills beside the timer.

        Each pill shows a diamond icon + MM:SS. The projected actual winner's
        pill gets a brighter fill to indicate who would really win considering
        both cap-tick-to-1000 and timeout scenarios.
        """
        score_0 = scores.get(0, 0)
        score_1 = scores.get(1, 0)
        rate_0 = score_rates[0]
        rate_1 = score_rates[1]

        ttw_0 = (self._win_score - score_0) / rate_0 if rate_0 > 0.01 else float("inf")
        ttw_1 = (self._win_score - score_1) / rate_1 if rate_1 > 0.01 else float("inf")

        winner = self._projected_winner(score_0, score_1, rate_0, rate_1, time_left)

        font_size = self.TIMER_FONT_SIZE
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font_size)

        timer_y = self.SCORE_BAR_HEIGHT + 20
        cx = panel_w + mm_size / 2
        pad_x, pad_y = 8, 4
        gap = 10
        diamond_size = font_size * 0.45

        def _pill(text, r, g, b, anchor_x, ty, is_winner, align_right=False):
            """Draw a TTW pill: ◆ MM:SS, highlighted if projected winner."""
            cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(font_size)
            ext = cr.text_extents(text)
            diamond_space = diamond_size + 5
            content_w = diamond_space + ext.width
            pill_w = content_w + pad_x * 2
            pill_h = ext.height + pad_y * 2
            pill_r = pill_h / 2

            # Position pill
            if align_right:
                pill_x = anchor_x - pill_w
            else:
                pill_x = anchor_x
            pill_y = ty - ext.height - pad_y

            def _rounded_rect():
                cr.new_sub_path()
                cr.arc(pill_x + pill_w - pill_r, pill_y + pill_r, pill_r, -1.5708, 0)
                cr.arc(pill_x + pill_w - pill_r, pill_y + pill_h - pill_r, pill_r, 0, 1.5708)
                cr.arc(pill_x + pill_r, pill_y + pill_h - pill_r, pill_r, 1.5708, 3.14159)
                cr.arc(pill_x + pill_r, pill_y + pill_r, pill_r, 3.14159, 4.71239)
                cr.close_path()

            # Background
            if is_winner:
                cr.set_source_rgba(r, g, b, 0.25)
            else:
                cr.set_source_rgba(0.05, 0.08, 0.18, 0.85)
            _rounded_rect()
            cr.fill()

            # Border
            cr.set_source_rgba(r, g, b, 1.0 if is_winner else 0.5)
            cr.set_line_width(2.0 if is_winner else 1.0)
            _rounded_rect()
            cr.stroke()

            # Diamond icon (centered vertically in pill)
            alpha = 1.0
            dcx = pill_x + pad_x + diamond_size / 2
            dcy = pill_y + pill_h / 2
            s = diamond_size
            cr.save()
            cr.translate(dcx, dcy)
            cr.rotate(0.7854)
            cr.rectangle(-s / 2, -s / 2, s, s)
            cr.set_source_rgba(r, g, b, alpha)
            cr.fill()
            cr.restore()

            # Time text
            cr.set_source_rgba(r, g, b, alpha)
            cr.move_to(pill_x + pad_x + diamond_space, ty)
            cr.show_text(text)

        # Timer pill width for gap calculation
        cr.set_font_size(self.TIMER_FONT_SIZE)
        timer_ext = cr.text_extents("00:00")
        timer_half_w = timer_ext.width / 2 + 8 + 4

        r0, g0, b0, _ = self.ctx.config.team_colors.get(0, (0.33, 0.85, 0.33, 1.0))
        r1, g1, b1, _ = self.ctx.config.team_colors.get(1, (0.90, 0.25, 0.25, 1.0))

        # Team 0 TTW pill (left of timer, right-aligned)
        if ttw_0 < float("inf"):
            secs = max(0, int(ttw_0))
            text_0 = f"{secs // 60}:{secs % 60:02d}"
        else:
            text_0 = "--:--"
        _pill(text_0, r0, g0, b0,
              cx - timer_half_w - gap, timer_y,
              winner == 0, align_right=True)

        # Team 1 TTW pill (right of timer, left-aligned)
        if ttw_1 < float("inf"):
            secs = max(0, int(ttw_1))
            text_1 = f"{secs // 60}:{secs % 60:02d}"
        else:
            text_1 = "--:--"
        _pill(text_1, r1, g1, b1,
              cx + timer_half_w + gap, timer_y,
              winner == 1)

    def _projected_winner(self, score_0, score_1, rate_0, rate_1, time_left):
        """Project who wins: 0=ally, 1=enemy, -1=draw/unclear."""
        win = self._win_score
        # Time to reach win score for each team
        ttw_0 = (win - score_0) / rate_0 if rate_0 > 0.01 else float("inf")
        ttw_1 = (win - score_1) / rate_1 if rate_1 > 0.01 else float("inf")

        # Who hits win score first?
        if ttw_0 < ttw_1 and ttw_0 < time_left:
            return 0
        if ttw_1 < ttw_0 and ttw_1 < time_left:
            return 1
        # Neither reaches win score before timeout — higher score wins
        final_0 = score_0 + rate_0 * time_left
        final_1 = score_1 + rate_1 * time_left
        if final_0 > final_1:
            return 0
        if final_1 > final_0:
            return 1
        return -1

    def _draw_kill_swing(self, cr, panel_w, mm_size, scores, score_rates, time_left):
        """Show '1 KILL DECIDES' when a single kill would flip the projected winner.

        Glows in the color of the team that holds the swing condition
        (i.e., whose kill would flip the outcome).
        """
        score_0 = scores.get(0, 0)
        score_1 = scores.get(1, 0)

        if score_0 < 500 and score_1 < 500:
            return

        rate_0 = score_rates[0]
        rate_1 = score_rates[1]
        swing = self._kill_swing

        current = self._projected_winner(score_0, score_1, rate_0, rate_1, time_left)

        # Check if a kill by either team flips the result
        ally_flips = self._projected_winner(
            score_0 + swing, score_1, rate_0, rate_1, time_left
        ) != current
        enemy_flips = self._projected_winner(
            score_0, score_1 + swing, rate_0, rate_1, time_left
        ) != current

        if not ally_flips and not enemy_flips:
            return

        # Determine glow color: team that benefits from the swing
        r0, g0, b0, _ = self.ctx.config.team_colors.get(0, (0.33, 0.85, 0.33, 1.0))
        r1, g1, b1, _ = self.ctx.config.team_colors.get(1, (0.90, 0.25, 0.25, 1.0))
        if ally_flips and not enemy_flips:
            r, g, b = r0, g0, b0  # Only ally kill flips — glow ally color
        elif enemy_flips and not ally_flips:
            r, g, b = r1, g1, b1  # Only enemy kill flips — glow enemy color
        else:
            r, g, b = 1.0, 0.85, 0.0  # Both can flip — neutral gold

        font_size = self.COUNT_FONT_SIZE
        text = "1 KILL DECIDES"
        cx = panel_w + mm_size / 2
        y = self.SCORE_BAR_HEIGHT + 52
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font_size)
        ext = cr.text_extents(text)
        self.draw_text_halo(
            cr, cx - ext.width / 2, y, text,
            r, g, b, alpha=0.9, font_size=font_size, bold=True,
        )

    def _draw_match_result(self, cr, panel_w, mm_size, result_winner):
        """Draw Victory/Defeat/Draw overlay at match end."""
        # Map raw winner to display team
        display_winner = self.ctx.raw_to_display_team(result_winner)

        if display_winner == 0:
            text = "VICTORY"
            r, g, b = 0.33, 0.85, 0.33
        elif display_winner == 1:
            text = "DEFEAT"
            r, g, b = 0.90, 0.25, 0.25
        else:
            text = "DRAW"
            r, g, b = 1.0, 0.85, 0.0

        font_size = mm_size * 0.07
        cx = panel_w + mm_size / 2
        cy = mm_size * 0.4

        # Dark backdrop
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.rectangle(panel_w, cy - font_size * 1.2, mm_size, font_size * 2.4)
        cr.fill()

        # Result text centered
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font_size)
        ext = cr.text_extents(text)
        tx = cx - ext.width / 2
        ty = cy + ext.height / 2
        self.draw_text_halo(cr, tx, ty, text, r, g, b, font_size=font_size, bold=True, outline_width=4.0)
