from __future__ import annotations
import cairo
from renderer.layers.base import Layer, RenderContext


class TrailLayer(Layer):
    """Draws fading movement trails behind ships."""

    SAMPLE_INTERVAL = 0.5  # Game-seconds between position samples
    MAX_ALPHA = 0.6
    LINE_WIDTH = 1.5

    _trails: dict[int, list[tuple[float, float, float]]]  # entity_id -> [(t, px, py), ...]

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        self._trails = {}

        replay = ctx.replay

        # Pre-compute position samples for all ships
        t = 0.0
        duration = replay.duration

        while t <= duration:
            state = replay.state_at(t)
            for entity_id, ship in state.ships.items():
                if not ship.is_alive:
                    continue
                if not ctx.is_visible(entity_id, t):
                    continue
                wx, _, wz = ship.position
                px, py = ctx.world_to_pixel(wx, wz)

                if entity_id not in self._trails:
                    self._trails[entity_id] = []
                self._trails[entity_id].append((t, px, py))

            t += self.SAMPLE_INTERVAL

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        trail_length = config.trail_length
        trail_start = max(0.0, timestamp - trail_length)

        for entity_id, samples in self._trails.items():
            # Get team color
            ship = state.ships.get(entity_id)
            if ship is None:
                continue
            player = self.ctx.player_lookup.get(entity_id)
            if player and player.relation == 0:
                team_color = config.self_color
            elif player:
                team_color = config.team_colors.get(player.team_id, (0.5, 0.5, 0.5, 1.0))
            else:
                team_color = (0.5, 0.5, 0.5, 1.0)
            r, g, b, _ = team_color

            # Find relevant samples (trail_start <= t <= timestamp)
            # Samples are sorted by time, use binary search bounds
            relevant = [
                (t, px, py) for t, px, py in samples
                if trail_start <= t <= timestamp
            ]

            if len(relevant) < 2:
                continue

            # Draw trail segments with fading alpha
            cr.set_line_width(self.LINE_WIDTH)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)

            for i in range(1, len(relevant)):
                t0, x0, y0 = relevant[i - 1]
                t1, x1, y1 = relevant[i]

                # Alpha based on how recent the segment is
                age = timestamp - t1  # 0 = newest, trail_length = oldest
                alpha = self.MAX_ALPHA * (1.0 - age / trail_length) if trail_length > 0 else self.MAX_ALPHA
                alpha = max(0.0, min(self.MAX_ALPHA, alpha))

                cr.set_source_rgba(r, g, b, alpha)
                cr.move_to(x0, y0)
                cr.line_to(x1, y1)
                cr.stroke()
