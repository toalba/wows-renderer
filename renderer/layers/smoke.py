"""Renders smoke screens on the minimap as semi-transparent gray circles."""

from __future__ import annotations

import math
import struct

import cairo

from renderer.layers.base import Layer, RenderContext


class SmokeLayer(Layer):
    """Draws smoke screen clouds on the minimap.

    SmokeScreen entities have:
    - radius: float (in space_units)
    - points: list of {x, y, z} positions (smoke puff locations)

    Each point is rendered as a semi-transparent gray circle.
    Smoke is drawn below ships but above the map background.
    """

    SMOKE_COLOR = (0.85, 0.85, 0.85)  # light gray
    FILL_ALPHA = 0.35
    RADIUS_MULTIPLIER = 1.0  # exact game radius
    _PHASE_GAP_THRESHOLD = 20.0  # seconds — gap larger than this = expiration phase

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)
        self._puff_cache = self._build_puff_cache()

    def _build_puff_cache(
        self,
    ) -> dict[int, list[tuple[float, float, float]]]:
        """Extract actual puff positions from NESTED_PROPERTY packets.

        The NON_VOLATILE_POSITION packets track the entity's anchor/center,
        NOT individual puff locations. The real puff coordinates are in
        NESTED_PROPERTY updates to the 'points' array — each update appends
        a VECTOR3 (x, y, z) encoded as 3 little-endian floats in the raw payload.

        Returns: entity_id → list of (timestamp, world_x, world_z) per puff.
        """
        from wows_replay_parser.packets.types import PacketType

        tracker = self.ctx.replay._tracker
        cache: dict[int, list[tuple[float, float, float]]] = {}

        # Identify SmokeScreen entity IDs
        smoke_ids = {
            eid
            for eid, etype in tracker._entity_types.items()
            if etype == "SmokeScreen"
        }

        for packet in self.ctx.replay.packets:
            eid = getattr(packet, "entity_id", None)
            if eid not in smoke_ids:
                continue

            if packet.type == PacketType.ENTITY_CREATE:
                # First puff position from entity creation
                if packet.position is not None:
                    cache.setdefault(eid, []).append(
                        (packet.timestamp, packet.position[0], packet.position[2]),
                    )

            elif packet.type == PacketType.NESTED_PROPERTY:
                # Puff positions are 3 floats (x, y, z) at the end of the payload.
                # Payload: entity_id(4) + header(variable) + x(4) + y(4) + z(4)
                raw = packet.raw_payload
                if len(raw) < 16:
                    continue
                # The last 12 bytes are always the VECTOR3
                x, y, z = struct.unpack_from("<fff", raw, len(raw) - 12)
                if abs(x) < 5000 and abs(z) < 5000:
                    cache.setdefault(eid, []).append(
                        (packet.timestamp, x, z),
                    )

        # Split into laying-only (discard expiration-phase entries)
        for eid in list(cache):
            entries = cache[eid]
            laying: list[tuple[float, float, float]] = [entries[0]]
            for i in range(1, len(entries)):
                if entries[i][0] - entries[i - 1][0] > self._PHASE_GAP_THRESHOLD:
                    break
                laying.append(entries[i])
            cache[eid] = laying

        return cache

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        tracker = getattr(self.ctx.replay, "_tracker", None)
        if tracker is None:
            return

        map_size = self.ctx.map_size
        mm = self.ctx.config.minimap_size
        r, g, b = self.SMOKE_COLOR

        for entity_id, puffs in self._puff_cache.items():
            if not puffs:
                continue

            props = tracker._current.get(entity_id, {})
            radius = props.get("radius", 0)
            if not radius:
                continue

            # Not yet created
            if puffs[0][0] > timestamp:
                continue

            # Check if smoke has expired (EntityLeave)
            leave_time = tracker._entity_leave_times.get(entity_id)
            if leave_time is not None and leave_time <= timestamp:
                continue

            # Trap 3: smoke radius is in space_units
            px_radius = radius * self.RADIUS_MULTIPLIER / map_size * mm

            # Draw each smoke puff that exists at this timestamp
            for puff_t, wx, wz in puffs:
                if puff_t > timestamp:
                    break  # future puffs not yet laid
                px, py = self.ctx.world_to_pixel(wx, wz)

                cr.new_sub_path()
                cr.arc(px, py, px_radius, 0, 2 * math.pi)
                cr.set_source_rgba(r, g, b, self.FILL_ALPHA)
                cr.fill()
