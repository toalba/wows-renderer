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
    ship_db: dict[int, dict] = None  # ship_id -> {name, species, nation, level}
    ship_icons: dict[str, dict] = None  # species -> {ally/enemy/white: cairo.ImageSurface}
    first_seen: dict[int, float] = None  # entity_id -> first position timestamp
    _self_team_raw: int | None = None  # raw team_id of the recording player

    # Scale factor relative to 760px reference resolution.
    # All font sizes, icon sizes, offsets, line widths should multiply by this.
    _REFERENCE_SIZE: int = 760

    @property
    def scale(self) -> float:
        """Scale factor for rendering at resolutions above 760px."""
        return self.config.minimap_size / self._REFERENCE_SIZE

    def __post_init__(self) -> None:
        if self.first_seen is None:
            self.first_seen = self._build_first_seen()
        if self._self_team_raw is None:
            self._self_team_raw = self._detect_self_team_raw()

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

    def _detect_self_team_raw(self) -> int:
        """Detect the raw team_id of the recording player.

        Looks at the BattleLogic teams data and the self player's
        entity to determine which raw team (from game data) corresponds
        to the self/ally side (display team 0).
        """
        # Find self player
        self_player = None
        for p in self.player_lookup.values():
            if p.relation == 0:
                self_player = p
                break

        if self_player is None:
            return 0

        # Use the roster player's team_id (most reliable source)
        if hasattr(self_player, "team_id") and self_player.team_id is not None:
            return int(self_player.team_id)

        # Fallback: check meta vehicles for raw teamId
        meta = getattr(self.replay, "meta", {})
        for vehicle in meta.get("vehicles", []):
            if not isinstance(vehicle, dict):
                continue
            if vehicle.get("relation") == 0:
                raw_tid = vehicle.get("teamId")
                if raw_tid is not None:
                    return int(raw_tid)

        return 0

    def raw_to_display_team(self, raw_team_id: int) -> int:
        """Map a raw team_id from game data to display team (0=ally, 1=enemy).

        Handles the perspective swap (Trap 5): if the recording player's
        raw team is 1, all raw team IDs need to be swapped.
        """
        if self._self_team_raw == 0:
            return raw_team_id  # No swap needed
        # Self raw team is 1: swap 0↔1
        if raw_team_id == self._self_team_raw:
            return 0  # Self team → display 0 (ally/green)
        return 1  # Other team → display 1 (enemy/red)

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

    @staticmethod
    def draw_text_halo(
        cr: cairo.Context,
        x: float,
        y: float,
        text: str,
        r: float,
        g: float,
        b: float,
        alpha: float = 1.0,
        font_size: float = 10.0,
        bold: bool = False,
        outline_width: float = 3.0,
    ) -> None:
        """Draw text with a dark stroke halo for readability.

        The halo guarantees text is readable against any background.
        """
        cr.select_font_face(
            "sans-serif",
            cairo.FONT_SLANT_NORMAL,
            cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL,
        )
        cr.set_font_size(font_size)

        # 1. Dark stroke outline
        cr.move_to(x, y)
        cr.text_path(text)
        cr.set_source_rgba(0, 0, 0, 0.9 * alpha)
        cr.set_line_width(outline_width)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()

        # 2. Fill on top
        cr.set_source_rgba(r, g, b, alpha)
        cr.move_to(x, y)
        cr.show_text(text)
