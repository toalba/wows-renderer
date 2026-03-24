from __future__ import annotations

import math

import cairo

from renderer.layers.base import Layer, RenderContext


class ProjectileLayer(Layer):
    """Draws shell traces and torpedo tracks on the minimap."""

    SHELL_LINE_WIDTH = 1.5
    SHELL_TRAIL_FRAC = 0.12  # 12% trail behind head
    TORPEDO_RADIUS = 2.5
    TORPEDO_MAX_LIFETIME = 90.0  # seconds
    TORPEDO_DEFAULT_SPEED = 60.0  # game units/s

    _shells: list[dict]  # sorted by create time
    _torpedoes: list[dict]

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        replay = ctx.replay

        # Import event types
        from wows_replay_parser.events.models import (
            ShotCreatedEvent,
            ShotDestroyedEvent,
            TorpedoCreatedEvent,
        )

        # Build shell lifecycle data
        created = replay.events_of_type(ShotCreatedEvent)
        destroyed = replay.events_of_type(ShotDestroyedEvent)

        destroy_map: dict[int, float] = {}
        for evt in destroyed:
            destroy_map[evt.shot_id] = evt.timestamp

        self._shells = []
        for evt in created:
            end_t = destroy_map.get(evt.shot_id)
            if end_t is None:
                # Estimate from server_time_left or default ~5s flight
                end_t = evt.timestamp + getattr(evt, "server_time_left", 5.0)

            self._shells.append(
                {
                    "start_t": evt.timestamp,
                    "end_t": end_t,
                    "owner_id": evt.owner_id,
                    "spawn_x": evt.spawn_x,
                    "spawn_z": evt.spawn_z,
                    "target_x": evt.target_x,
                    "target_z": evt.target_z,
                }
            )

        # Sort by start time for efficient windowed lookup
        self._shells.sort(key=lambda s: s["start_t"])

        # Build torpedo data (Trap 9: includes S-turn support)
        torp_events = replay.events_of_type(TorpedoCreatedEvent)
        self._torpedoes = []
        for evt in torp_events:
            speed = math.sqrt(evt.direction_x**2 + evt.direction_z**2)
            if speed < 1.0:
                speed = self.TORPEDO_DEFAULT_SPEED
            # Normalize direction
            dx = evt.direction_x / speed if speed > 0 else 0
            dz = evt.direction_z / speed if speed > 0 else 0

            initial_yaw = math.atan2(evt.direction_x, evt.direction_z)

            # S-turn data from raw_data (maneuverDump)
            maneuver = None
            raw = getattr(evt, "raw_data", {}) or {}
            maneuver_dump = raw.get("maneuverDump")
            if isinstance(maneuver_dump, dict):
                target_yaw = float(maneuver_dump.get("targetYaw", initial_yaw))
                yaw_speed = float(maneuver_dump.get("yawSpeed", 0))
                if yaw_speed > 0:
                    maneuver = {
                        "target_yaw": target_yaw,
                        "yaw_speed": yaw_speed,
                    }

            self._torpedoes.append(
                {
                    "start_t": evt.timestamp,
                    "end_t": evt.timestamp + self.TORPEDO_MAX_LIFETIME,
                    "owner_id": evt.owner_id,
                    "x": evt.x,
                    "z": evt.z,
                    "dx": dx,
                    "dz": dz,
                    "speed": speed,
                    "initial_yaw": initial_yaw,
                    "maneuver": maneuver,
                }
            )
        self._torpedoes.sort(key=lambda t: t["start_t"])

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        map_size = self.ctx.map_size
        half = map_size / 2.0
        player_lookup = self.ctx.player_lookup
        team_colors = config.team_colors
        w2p = self.ctx.world_to_pixel

        # Draw shells (Trap 8: animated line segments, not dots)
        cr.set_line_width(self.SHELL_LINE_WIDTH)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        for shell in self._shells:
            if shell["start_t"] > timestamp:
                break  # sorted, no more active
            if shell["end_t"] < timestamp:
                continue

            # Interpolate head and tail positions
            duration = shell["end_t"] - shell["start_t"]
            if duration <= 0:
                continue
            frac = (timestamp - shell["start_t"]) / duration
            frac = max(0.0, min(1.0, frac))
            tail_frac = max(0.0, frac - self.SHELL_TRAIL_FRAC)

            sx, sz = shell["spawn_x"], shell["spawn_z"]
            tx, tz = shell["target_x"], shell["target_z"]

            head_x = sx + (tx - sx) * frac
            head_z = sz + (tz - sz) * frac
            tail_x = sx + (tx - sx) * tail_frac
            tail_z = sz + (tz - sz) * tail_frac

            hpx, hpy = w2p(head_x, head_z)
            tpx, tpy = w2p(tail_x, tail_z)

            # Color by owner team (player.team_id is already display team from roster)
            player = player_lookup.get(shell["owner_id"])
            if player:
                color = team_colors.get(player.team_id, (1.0, 0.95, 0.4, 0.85))
                cr.set_source_rgba(color[0], color[1], color[2], 0.85)
            else:
                cr.set_source_rgba(1.0, 0.95, 0.4, 0.85)

            cr.move_to(tpx, tpy)
            cr.line_to(hpx, hpy)
            cr.stroke()

        # Draw torpedoes (Trap 9: S-turn support)
        for torp in self._torpedoes:
            if torp["start_t"] > timestamp:
                break
            if torp["end_t"] < timestamp:
                continue

            elapsed = timestamp - torp["start_t"]
            wx, wz = self._interpolate_torpedo(torp, elapsed)

            # Check if still within map bounds
            if abs(wx) > half or abs(wz) > half:
                continue

            px, py = w2p(wx, wz)

            # Color by owner team (player.team_id is already display team from roster)
            player = player_lookup.get(torp["owner_id"])
            owner_team = player.team_id if player else 1
            color = team_colors.get(owner_team, (0.5, 0.5, 0.5, 1.0))

            cr.set_source_rgba(color[0], color[1], color[2], 0.8)
            cr.new_sub_path()
            cr.arc(px, py, self.TORPEDO_RADIUS, 0, 2 * math.pi)
            cr.fill()

    def _interpolate_torpedo(
        self, torp: dict, elapsed: float,
    ) -> tuple[float, float]:
        """Compute torpedo world position at elapsed time.

        Handles both straight-line and S-turn (maneuverDump) torpedoes.
        """
        maneuver = torp.get("maneuver")
        origin_x = torp["x"]
        origin_z = torp["z"]
        speed = torp["speed"]

        if maneuver is None:
            # Straight line
            return (
                origin_x + torp["dx"] * speed * elapsed,
                origin_z + torp["dz"] * speed * elapsed,
            )

        # S-turn interpolation (Trap 9)
        initial_yaw = torp["initial_yaw"]
        target_yaw = maneuver["target_yaw"]
        yaw_speed = maneuver["yaw_speed"]

        yaw_diff = target_yaw - initial_yaw
        # Normalize to [-π, π]
        yaw_diff = math.atan2(math.sin(yaw_diff), math.cos(yaw_diff))

        turn_sign = 1.0 if yaw_diff >= 0 else -1.0
        w = turn_sign * yaw_speed
        turn_duration = abs(yaw_diff) / yaw_speed if yaw_speed > 0 else 0

        if elapsed < turn_duration:
            # Arc phase
            if abs(w) < 1e-9:
                return (
                    origin_x + torp["dx"] * speed * elapsed,
                    origin_z + torp["dz"] * speed * elapsed,
                )
            ratio = speed / w
            yaw_t = initial_yaw + w * elapsed
            x = origin_x + ratio * (-math.cos(yaw_t) + math.cos(initial_yaw))
            z = origin_z + ratio * (math.sin(yaw_t) - math.sin(initial_yaw))
            return (x, z)
        else:
            # Straight line after turn
            if abs(w) < 1e-9:
                ratio_offset_x = 0.0
                ratio_offset_z = 0.0
            else:
                ratio = speed / w
                ratio_offset_x = ratio * (-math.cos(target_yaw) + math.cos(initial_yaw))
                ratio_offset_z = ratio * (math.sin(target_yaw) - math.sin(initial_yaw))

            turn_end_x = origin_x + ratio_offset_x
            turn_end_z = origin_z + ratio_offset_z
            straight_elapsed = elapsed - turn_duration
            # Direction after turn: target_yaw
            dx = math.sin(target_yaw)
            dz = math.cos(target_yaw)
            return (
                turn_end_x + dx * speed * straight_elapsed,
                turn_end_z + dz * speed * straight_elapsed,
            )
