from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import cairo

if TYPE_CHECKING:
    from wows_replay_parser.api import ParsedReplay
    from wows_replay_parser.roster import PlayerInfo
    from renderer.config import RenderConfig


@dataclass
class RenderContext:
    """Shared context passed to all layers during initialization."""
    config: RenderConfig
    replay: ParsedReplay
    map_size: float  # space_size from map_sizes.json
    player_lookup: dict[int, PlayerInfo]  # entity_id -> PlayerInfo
    first_seen: dict[int, float] = None  # entity_id -> first position timestamp

    def __post_init__(self) -> None:
        if self.first_seen is None:
            self.first_seen = self._build_first_seen()

    def _build_first_seen(self) -> dict[int, float]:
        """Build lookup of first real position update per entity.

        Ships should not be rendered before their first actual position update.
        A position at t=0.0 is only valid for ally ships (relation 0 or 1),
        since enemies cannot be spotted at match start.
        """
        tracker = getattr(self.replay, "_tracker", None)
        if tracker is None:
            return {}
        positions = getattr(tracker, "_positions", {})
        result: dict[int, float] = {}
        for entity_id, pos_list in positions.items():
            if not pos_list:
                continue
            first_t = pos_list[0][0]
            # If first position is at t=0 for an enemy, it's a fake default —
            # use the second position entry instead (if available)
            player = self.player_lookup.get(entity_id)
            if player and player.relation == 2 and first_t < 1.0:
                if len(pos_list) > 1:
                    first_t = pos_list[1][0]
                else:
                    first_t = float("inf")  # never seen
            result[entity_id] = first_t
        return result

    def is_visible(self, entity_id: int, timestamp: float) -> bool:
        """Check if an entity should be rendered at this timestamp."""
        first_t = self.first_seen.get(entity_id)
        if first_t is None:
            return True  # unknown entity, show it
        return timestamp >= first_t

    def world_to_pixel(self, world_x: float, world_z: float) -> tuple[float, float]:
        """Convert world coordinates to pixel coordinates on the full canvas.

        Uses the WoWs community formula:
            pixel_x = pos_x * scaling + half_minimap
            pixel_y = -pos_z * scaling + half_minimap  (Z axis inverted)
        where scaling = minimap_size / space_size
        """
        scaling = self.config.minimap_size / self.map_size
        half_mm = self.config.minimap_size / 2.0
        px = world_x * scaling + half_mm + self.config.panel_width
        py = -world_z * scaling + half_mm
        return (px, py)


class Layer(ABC):
    """Abstract base class for renderer layers.

    Each layer draws directly onto a shared cairo.Context.
    Layers are composited in order (first added = bottom).
    """

    def initialize(self, ctx: RenderContext) -> None:
        """Called once before rendering. Preload assets, cache data.

        Default implementation stores the context. Override to add custom init.
        """
        self.ctx = ctx

    @abstractmethod
    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        """Draw this layer onto the shared cairo context.

        Args:
            cr: The shared cairo drawing context.
            state: GameState from the replay parser at this timestamp.
            timestamp: Current game time in seconds.
        """
        ...
