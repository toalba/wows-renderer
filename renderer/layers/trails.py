from __future__ import annotations

from bisect import bisect_left, bisect_right

import cairo

from renderer.layers.base import Layer, RenderContext


class TrailLayer(Layer):
    """Draws fading movement trails behind ships.

    Pre-samples positions at init time using iter_states (fast incremental),
    then renders using binary search + batched Cairo paths.
    """

    SAMPLE_INTERVAL = 0.5  # Game-seconds between position samples
    MAX_ALPHA = 0.6
    LINE_WIDTH = 1.5
    # Number of alpha buckets for batching (fewer = faster, more = smoother fade)
    ALPHA_BUCKETS = 4
    # If gap between consecutive trail points exceeds this, break the trail.
    # Prevents straight-line jumps when ships go unspotted and reappear elsewhere.
    GAP_THRESHOLD = 2.0  # seconds
    # If distance between consecutive points exceeds this, break the trail.
    # Catches cases where the time gap is small but the ship teleported.
    DISTANCE_THRESHOLD = 40.0  # pixels

    # Per-entity pre-computed data
    _trail_times: dict[int, list[float]]
    _trail_pixels: dict[int, list[tuple[float, float]]]
    _trail_colors: dict[int, tuple[float, float, float]]
    _trail_gaps: dict[int, set[int]]  # entity_id → set of indices where a gap starts

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        self._trail_times = {}
        self._trail_pixels = {}
        self._trail_colors = {}
        self._trail_gaps = {}

        replay = ctx.replay
        w2p = ctx.world_to_pixel

        # Pre-compute team color per entity (doesn't change)
        for entity_id, player in ctx.player_lookup.items():
            if player.relation == 0:
                r, g, b, _ = ctx.config.self_color
            else:
                c = ctx.config.team_colors.get(player.team_id, (0.5, 0.5, 0.5, 1.0))
                r, g, b = c[0], c[1], c[2]
            self._trail_colors[entity_id] = (r, g, b)

        # Sample positions using iter_states (O(n) total, not O(n²))
        duration = replay.duration
        timestamps = []
        t = 0.0
        while t <= duration:
            timestamps.append(t)
            t += self.SAMPLE_INTERVAL

        for t, state in zip(timestamps, replay.iter_states(timestamps)):
            for entity_id, ship in state.ships.items():
                if not ship.is_alive:
                    continue
                if not ctx.is_visible(entity_id, t):
                    continue
                wx, _, wz = ship.position
                px, py = w2p(wx, wz)

                if entity_id not in self._trail_times:
                    self._trail_times[entity_id] = []
                    self._trail_pixels[entity_id] = []
                    self._trail_gaps[entity_id] = set()

                times = self._trail_times[entity_id]
                pixels = self._trail_pixels[entity_id]
                # Detect gap → mark this index as a trail break
                if times:
                    dt = t - times[-1]
                    if dt > self.GAP_THRESHOLD:
                        self._trail_gaps[entity_id].add(len(times))
                    elif pixels:
                        # Distance check: large jumps even with small time gaps
                        prev_px, prev_py = pixels[-1]
                        dist = ((px - prev_px) ** 2 + (py - prev_py) ** 2) ** 0.5
                        if dist > self.DISTANCE_THRESHOLD:
                            self._trail_gaps[entity_id].add(len(times))

                times.append(t)
                pixels.append((px, py))

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        trail_length = self.ctx.config.trail_length
        trail_start = max(0.0, timestamp - trail_length)
        max_alpha = self.MAX_ALPHA
        n_buckets = self.ALPHA_BUCKETS

        cr.set_line_width(self.LINE_WIDTH * self.ctx.scale)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        for entity_id, times in self._trail_times.items():
            # Skip entities not in current state
            if entity_id not in state.ships:
                continue

            # Binary search for the relevant time window
            lo = bisect_left(times, trail_start)
            hi = bisect_right(times, timestamp)
            if hi - lo < 2:
                continue

            pixels = self._trail_pixels[entity_id]
            color = self._trail_colors.get(entity_id, (0.5, 0.5, 0.5))
            r, g, b = color

            # Batch segments into alpha buckets for fewer stroke() calls
            # Bucket 0 = oldest (lowest alpha), bucket N-1 = newest (highest alpha)
            bucket_size = (hi - lo) / n_buckets if n_buckets > 0 else hi - lo

            for bucket in range(n_buckets):
                # Segments in this bucket
                seg_lo = lo + int(bucket * bucket_size)
                seg_hi = lo + int((bucket + 1) * bucket_size)
                if seg_hi <= seg_lo + 1:
                    seg_hi = seg_lo + 2
                if seg_hi > hi:
                    seg_hi = hi
                if seg_hi - seg_lo < 2:
                    continue

                # Alpha for this bucket (based on midpoint age)
                mid_idx = (seg_lo + seg_hi) // 2
                if mid_idx >= len(times):
                    continue
                age = timestamp - times[mid_idx]
                alpha = max_alpha * (1.0 - age / trail_length) if trail_length > 0 else max_alpha
                alpha = max(0.0, min(max_alpha, alpha))
                if alpha < 0.01:
                    continue

                cr.set_source_rgba(r, g, b, alpha)

                # Build path for all segments in this bucket,
                # breaking at gaps (unspotted → re-spotted jumps)
                gaps = self._trail_gaps.get(entity_id, set())
                x0, y0 = pixels[seg_lo]
                cr.move_to(x0, y0)
                for i in range(seg_lo + 1, seg_hi):
                    if i in gaps:
                        # Break: stroke what we have, start new sub-path
                        cr.stroke()
                        cr.move_to(*pixels[i])
                    else:
                        cr.line_to(*pixels[i])
                cr.stroke()
