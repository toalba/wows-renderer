from __future__ import annotations

import math
from bisect import bisect_right

import cairo

from renderer.assets import load_projectiles_db
from renderer.layers.base import BaseRenderContext, Layer

# Ammo type colors: AP=white, HE=orange, SAP/CS=purple
_AMMO_COLORS: dict[str, tuple[float, float, float]] = {
    "AP": (1.0, 1.0, 1.0),      # white
    "HE": (1.0, 0.6, 0.1),      # orange
    "SAP": (1.0, 0.45, 0.7),     # pink
    "CS": (1.0, 0.45, 0.7),      # pink (CS = Combat Shell = SAP in GameParams)
}
_DEFAULT_AMMO_COLOR = (0.9, 0.9, 0.4)  # yellowish fallback

# Caliber → line width mapping
_MIN_CALIBER = 100   # mm
_MAX_CALIBER = 510   # mm
_MIN_LINE_WIDTH = 1.0
_MAX_LINE_WIDTH = 3.0
_SECONDARY_LINE_WIDTH = 0.6
_SECONDARY_ALPHA = 0.45
_MAIN_ALPHA = 0.85


def _caliber_to_width(caliber_mm: int) -> float:
    """Map caliber to line width (linear interpolation)."""
    if caliber_mm <= _MIN_CALIBER:
        return _MIN_LINE_WIDTH
    if caliber_mm >= _MAX_CALIBER:
        return _MAX_LINE_WIDTH
    frac = (caliber_mm - _MIN_CALIBER) / (_MAX_CALIBER - _MIN_CALIBER)
    return _MIN_LINE_WIDTH + frac * (_MAX_LINE_WIDTH - _MIN_LINE_WIDTH)


class ProjectileLayer(Layer):
    """Draws shell traces and torpedo tracks on the minimap.

    Shells are colored by ammo type (AP=white, HE=orange, SAP=pink),
    scaled by caliber, and secondary guns are rendered thinner/fainter.
    """

    SHELL_TRAIL_FRAC = 0.12  # 12% trail behind head
    TORPEDO_RADIUS = 2.5
    TORPEDO_MAX_LIFETIME = 90.0  # seconds
    TORPEDO_DEFAULT_SPEED = 60.0  # game units/s

    def initialize(self, ctx: BaseRenderContext) -> None:
        super().initialize(ctx)
        replay = ctx.replay

        from wows_replay_parser.events.models import (
            ShotCreatedEvent,
            ShotDestroyedEvent,
            TorpedoCreatedEvent,
        )

        # Load projectile database for ammo type + caliber
        proj_db = load_projectiles_db(ctx.config.effective_gamedata_path)

        # Build shell lifecycle data
        created = replay.events_of_type(ShotCreatedEvent)
        destroyed = replay.events_of_type(ShotDestroyedEvent)

        destroy_map: dict[tuple[int, int], float] = {}
        for evt in destroyed:
            destroy_map[(evt.owner_id, evt.shot_id)] = evt.timestamp

        # Shell data: (start_t, end_t, sx, sz, tx, tz)
        # Shell visual: (color_rgb, line_width, alpha) — precomputed per shell
        shells: list[tuple[float, float, float, float, float, float]] = []
        shell_visuals: list[tuple[tuple[float, float, float], float, float]] = []

        for evt in created:
            dx = evt.target_x - evt.spawn_x
            dz = evt.target_z - evt.spawn_z
            dist = math.sqrt(dx * dx + dz * dz)
            speed = evt.speed if evt.speed > 0 else 800.0
            flight_time = dist / speed

            destroy_t = destroy_map.get((evt.owner_id, evt.shot_id))
            if destroy_t is not None and destroy_t > evt.timestamp:
                end_t = destroy_t
            else:
                end_t = evt.timestamp + flight_time

            shells.append((
                evt.timestamp, end_t,
                evt.spawn_x, evt.spawn_z, evt.target_x, evt.target_z,
            ))

            # Look up projectile info
            proj = proj_db.get(evt.params_id)
            if proj:
                ammo = proj.get("a", "HE")
                caliber = proj.get("c", 200)
                is_secondary = proj.get("s", False)
            else:
                ammo = "HE"
                caliber = 200
                is_secondary = False

            color = _AMMO_COLORS.get(ammo, _DEFAULT_AMMO_COLOR)
            if is_secondary:
                width = _SECONDARY_LINE_WIDTH
                alpha = _SECONDARY_ALPHA
            else:
                width = _caliber_to_width(caliber)
                alpha = _MAIN_ALPHA

            shell_visuals.append((color, width, alpha))

        # Sort by start time
        order = sorted(range(len(shells)), key=lambda i: shells[i][0])
        self._shell_data = [shells[i] for i in order]
        self._shell_visuals = [shell_visuals[i] for i in order]
        self._shell_start_times = [s[0] for s in self._shell_data]
        self._shell_cursor = 0

        # Group shells by visual key for batched rendering
        # Pre-build the visual groups: (color, width, alpha) → list of shell indices
        self._visual_keys: list[tuple[tuple[float, float, float], float, float]] = []
        seen: dict[tuple, int] = {}
        self._shell_visual_idx: list[int] = []
        for v in self._shell_visuals:
            key = v
            if key not in seen:
                seen[key] = len(self._visual_keys)
                self._visual_keys.append(key)
            self._shell_visual_idx.append(seen[key])

        # Build torpedo data
        torp_events = replay.events_of_type(TorpedoCreatedEvent)
        torps: list[dict] = []
        torp_display_teams: list[int] = []
        for evt in torp_events:
            speed = math.sqrt(evt.direction_x**2 + evt.direction_z**2)
            if speed < 1.0:
                speed = self.TORPEDO_DEFAULT_SPEED
            tdx = evt.direction_x / speed if speed > 0 else 0
            tdz = evt.direction_z / speed if speed > 0 else 0
            initial_yaw = math.atan2(evt.direction_x, evt.direction_z)

            maneuver = None
            raw = getattr(evt, "raw_data", {}) or {}
            maneuver_dump = raw.get("maneuverDump")
            if isinstance(maneuver_dump, dict):
                target_yaw = float(maneuver_dump.get("targetYaw", initial_yaw))
                yaw_speed = float(maneuver_dump.get("yawSpeed", 0))
                if yaw_speed > 0:
                    maneuver = (target_yaw, yaw_speed)

            # Use ShotDestroyedEvent for torpedo end time (same as shells)
            destroy_t = destroy_map.get((evt.owner_id, evt.shot_id))
            if destroy_t is not None and destroy_t > evt.timestamp:
                end_t = destroy_t
            else:
                end_t = evt.timestamp + self.TORPEDO_MAX_LIFETIME

            torps.append({
                "start_t": evt.timestamp,
                "end_t": end_t,
                "x": evt.x, "z": evt.z,
                "dx": tdx, "dz": tdz,
                "speed": speed,
                "initial_yaw": initial_yaw,
                "maneuver": maneuver,
            })

            # Determine display team for torpedo color
            owner = ctx.player_lookup.get(evt.owner_id)
            if owner:
                torp_display_teams.append(ctx.raw_to_display_team(owner.team_id))
            else:
                torp_display_teams.append(1)  # unknown owner → enemy

        # Sort torpedoes by start time, keep display teams in sync
        order = sorted(range(len(torps)), key=lambda i: torps[i]["start_t"])
        self._torp_data = [torps[i] for i in order]
        self._torp_display_teams = [torp_display_teams[i] for i in order]
        self._torp_start_times = [t["start_t"] for t in self._torp_data]
        self._torp_cursor = 0

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        w2p = self.ctx.world_to_pixel
        half = self.ctx.map_size / 2.0

        # ── Shells (batched by visual style) ────────────────────
        hi = bisect_right(self._shell_start_times, timestamp)
        trail_frac = self.SHELL_TRAIL_FRAC

        # Collect segments per visual group
        n_groups = len(self._visual_keys)
        group_segments: list[list[tuple[float, float, float, float]]] = [[] for _ in range(n_groups)]

        for i in range(self._shell_cursor, hi):
            s = self._shell_data[i]
            start_t, end_t = s[0], s[1]

            if end_t < timestamp:
                if i == self._shell_cursor:
                    self._shell_cursor = i + 1
                continue

            duration = end_t - start_t
            if duration <= 0:
                continue

            frac = (timestamp - start_t) / duration
            if frac > 1.0:
                frac = 1.0
            tail_frac = frac - trail_frac
            if tail_frac < 0.0:
                tail_frac = 0.0

            sx, sz, tx, tz = s[2], s[3], s[4], s[5]
            ddx, ddz = tx - sx, tz - sz
            hpx, hpy = w2p(sx + ddx * frac, sz + ddz * frac)
            tpx, tpy = w2p(sx + ddx * tail_frac, sz + ddz * tail_frac)

            gi = self._shell_visual_idx[i]
            group_segments[gi].append((tpx, tpy, hpx, hpy))

        # One stroke per visual group
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for gi, segments in enumerate(group_segments):
            if not segments:
                continue
            color, width, alpha = self._visual_keys[gi]
            cr.set_line_width(width)
            cr.set_source_rgba(color[0], color[1], color[2], alpha)
            for tpx, tpy, hpx, hpy in segments:
                cr.move_to(tpx, tpy)
                cr.line_to(hpx, hpy)
            cr.stroke()

        # ── Torpedoes (grouped by team) ────────────────────────
        hi_t = bisect_right(self._torp_start_times, timestamp)
        # Group positions by display team for batched rendering
        torp_by_team: dict[int, list[tuple[float, float]]] = {0: [], 1: []}

        for i in range(self._torp_cursor, hi_t):
            torp = self._torp_data[i]
            if torp["end_t"] < timestamp:
                if i == self._torp_cursor:
                    self._torp_cursor = i + 1
                continue

            elapsed = timestamp - torp["start_t"]
            wx, wz = self._interpolate_torpedo(torp, elapsed)
            if abs(wx) > half or abs(wz) > half:
                continue
            display_team = self._torp_display_teams[i]
            torp_by_team[display_team].append(w2p(wx, wz))

        radius = self.TORPEDO_RADIUS
        two_pi = 2 * math.pi
        team_colors = self.ctx.config.team_colors
        for display_team, positions in torp_by_team.items():
            if not positions:
                continue
            tr, tg, tb, ta = team_colors.get(display_team, (0.3, 0.9, 0.3, 0.8))
            cr.set_source_rgba(tr, tg, tb, 0.8)
            for px, py in positions:
                cr.new_sub_path()
                cr.arc(px, py, radius, 0, two_pi)
            cr.fill()

    @staticmethod
    def _interpolate_torpedo(torp: dict, elapsed: float) -> tuple[float, float]:
        """Compute torpedo world position at elapsed time."""
        origin_x = torp["x"]
        origin_z = torp["z"]
        speed = torp["speed"]
        maneuver = torp["maneuver"]

        if maneuver is None:
            return (
                origin_x + torp["dx"] * speed * elapsed,
                origin_z + torp["dz"] * speed * elapsed,
            )

        initial_yaw = torp["initial_yaw"]
        target_yaw, yaw_speed = maneuver

        yaw_diff = math.atan2(
            math.sin(target_yaw - initial_yaw),
            math.cos(target_yaw - initial_yaw),
        )
        turn_sign = 1.0 if yaw_diff >= 0 else -1.0
        w = turn_sign * yaw_speed
        turn_duration = abs(yaw_diff) / yaw_speed if yaw_speed > 0 else 0

        if elapsed < turn_duration:
            if abs(w) < 1e-9:
                return (
                    origin_x + torp["dx"] * speed * elapsed,
                    origin_z + torp["dz"] * speed * elapsed,
                )
            ratio = speed / w
            yaw_t = initial_yaw + w * elapsed
            return (
                origin_x + ratio * (-math.cos(yaw_t) + math.cos(initial_yaw)),
                origin_z + ratio * (math.sin(yaw_t) - math.sin(initial_yaw)),
            )

        if abs(w) < 1e-9:
            turn_end_x = origin_x
            turn_end_z = origin_z
        else:
            ratio = speed / w
            turn_end_x = origin_x + ratio * (-math.cos(target_yaw) + math.cos(initial_yaw))
            turn_end_z = origin_z + ratio * (math.sin(target_yaw) - math.sin(initial_yaw))

        straight_elapsed = elapsed - turn_duration
        return (
            turn_end_x + math.sin(target_yaw) * speed * straight_elapsed,
            turn_end_z + math.cos(target_yaw) * speed * straight_elapsed,
        )
