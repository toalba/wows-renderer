from __future__ import annotations

import math

import cairo

from renderer.layers.base import Layer, RenderContext


class ProjectileLayer(Layer):
    """Draws shell traces and torpedo tracks on the minimap."""

    SHELL_RADIUS = 1.5
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

        # Build torpedo data
        torp_events = replay.events_of_type(TorpedoCreatedEvent)
        self._torpedoes = []
        for evt in torp_events:
            speed = math.sqrt(evt.direction_x**2 + evt.direction_z**2)
            if speed < 1.0:
                speed = self.TORPEDO_DEFAULT_SPEED
            # Normalize direction
            dx = evt.direction_x / speed if speed > 0 else 0
            dz = evt.direction_z / speed if speed > 0 else 0

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

        # Draw shells
        cr.set_source_rgba(1.0, 0.95, 0.4, 0.85)  # Bright yellow
        for shell in self._shells:
            if shell["start_t"] > timestamp:
                break  # sorted, no more active
            if shell["end_t"] < timestamp:
                continue

            # Interpolate position
            duration = shell["end_t"] - shell["start_t"]
            if duration <= 0:
                continue
            progress = (timestamp - shell["start_t"]) / duration
            progress = max(0.0, min(1.0, progress))

            wx = shell["spawn_x"] + (shell["target_x"] - shell["spawn_x"]) * progress
            wz = shell["spawn_z"] + (shell["target_z"] - shell["spawn_z"]) * progress

            px, py = w2p(wx, wz)

            cr.new_sub_path()
            cr.arc(px, py, self.SHELL_RADIUS, 0, 2 * math.pi)
            cr.fill()

        # Draw torpedoes
        for torp in self._torpedoes:
            if torp["start_t"] > timestamp:
                break
            if torp["end_t"] < timestamp:
                continue

            elapsed = timestamp - torp["start_t"]
            wx = torp["x"] + torp["dx"] * torp["speed"] * elapsed
            wz = torp["z"] + torp["dz"] * torp["speed"] * elapsed

            # Check if still within map bounds
            if abs(wx) > half or abs(wz) > half:
                continue

            px, py = w2p(wx, wz)

            # Color by owner team
            player = player_lookup.get(torp["owner_id"])
            owner_team = player.team_id if player else 1
            color = team_colors.get(owner_team, (0.5, 0.5, 0.5, 1.0))

            cr.set_source_rgba(color[0], color[1], color[2], 0.8)
            cr.new_sub_path()
            cr.arc(px, py, self.TORPEDO_RADIUS, 0, 2 * math.pi)
            cr.fill()
