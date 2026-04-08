from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import cairo

# Primary font: WoWS Warhelios. CJK fallback: Source Han Sans CN (from gamedata).
FONT_FAMILY = "Warhelios"
FONT_FAMILY_CJK = "Source Han Sans CN Bold WH"


def _has_cjk(text: str) -> bool:
    """Return True if text contains any CJK characters."""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF        # CJK Unified Ideographs
            or 0x3400 <= cp <= 0x4DBF      # CJK Extension A
            or 0x3000 <= cp <= 0x303F      # CJK Symbols and Punctuation
            or 0x3040 <= cp <= 0x309F      # Hiragana
            or 0x30A0 <= cp <= 0x30FF      # Katakana
            or 0xAC00 <= cp <= 0xD7AF      # Hangul Syllables
            or 0xFF00 <= cp <= 0xFFEF      # Fullwidth Forms
            or 0x20000 <= cp <= 0x2A6DF):  # CJK Extension B
            return True
    return False


def _font_for_text(text: str) -> str:
    """Return the appropriate font family for the given text."""
    return FONT_FAMILY_CJK if _has_cjk(text) else FONT_FAMILY

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
    ship_db: dict[int, dict] | None = None  # ship_id -> {name, species, nation, level}
    ship_icons: dict[str, dict] | None = None  # species -> {ally/enemy/white: cairo.ImageSurface}
    first_seen: dict[int, float] | None = None  # entity_id -> first position timestamp
    _self_team_raw: int | None = None  # raw team_id of the recording player
    division_mates: set[int] | None = None  # entity_ids in recording player's division (excl self)

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
        if self.division_mates is None:
            self.division_mates = self._build_division_mates()

    def _build_first_seen(self) -> dict[int, float]:
        """Build lookup of first real visibility per entity.

        Allies (relation 0, 1): use first position timestamp (always valid).
        Enemies (relation 2): use first MinimapVisionEvent where is_visible=True,
        falling back to the first real position timestamp. Vision events arrive
        before position packets, so this avoids the 1-second gap where an enemy
        is spotted but has no position data yet.
        """
        tracker = self.replay.tracker
        if tracker is None:
            return {}
        positions = tracker.positions_dict
        minimap = tracker.minimap_positions_dict
        result: dict[int, float] = {}

        for entity_id, pos_list in positions.items():
            if not pos_list:
                continue
            player = self.player_lookup.get(entity_id)

            if player and player.relation == 2:
                # Enemy: prefer first vision event (spotted) timestamp
                first_vision_t = float("inf")
                mm_entries = minimap.get(entity_id, [])
                for entry in mm_entries:
                    # entry: (timestamp, world_x, world_z, heading_rad, is_visible, is_disappearing)
                    if entry[4] and not entry[5]:  # is_visible and not is_disappearing
                        first_vision_t = entry[0]
                        break

                # Fall back to first real position (skip t<1.0 fakes)
                first_pos_t = float("inf")
                for pos in pos_list:
                    if pos[0] >= 1.0:
                        first_pos_t = pos[0]
                        break

                result[entity_id] = min(first_vision_t, first_pos_t)
            else:
                # Ally/self: first position is always valid
                result[entity_id] = pos_list[0][0]
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

    def _build_division_mates(self) -> set[int]:
        """Find entity_ids sharing the recording player's division (excl self).

        Disabled for clan battles where everyone shares the same prebattle_id.
        """
        if self.replay.meta.get("gameType") == "ClanBattle":
            return set()

        self_player = None
        for p in self.player_lookup.values():
            if p.relation == 0:
                self_player = p
                break
        if self_player is None or not self_player.prebattle_id:
            return set()
        return {
            eid for eid, p in self.player_lookup.items()
            if p.prebattle_id == self_player.prebattle_id and p.relation != 0
        }

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
        px = world_x * scaling + half_mm + self.config.left_panel
        py = -world_z * scaling + half_mm + self.config.hud_height
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

    # Text surface cache: (text, font_family, font_size_int, bold, r8, g8, b8) → ImageSurface
    _text_cache: dict[tuple, cairo.ImageSurface] = {}

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
        """Draw text with a dark shadow for readability.

        Uses a double-draw shadow technique instead of text_path+stroke
        for much better performance.
        """
        font_family = _font_for_text(text)
        weight = cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL
        cr.select_font_face(font_family, cairo.FONT_SLANT_NORMAL, weight)
        cr.set_font_size(font_size)

        # Shadow offset scaled to font size
        offset = max(1.0, font_size * 0.08)

        # 1. Dark shadow (offset down-right)
        cr.set_source_rgba(0, 0, 0, 0.85 * alpha)
        cr.move_to(x + offset, y + offset)
        cr.show_text(text)

        # 2. Dark shadow (offset up-left for fuller coverage)
        cr.set_source_rgba(0, 0, 0, 0.5 * alpha)
        cr.move_to(x - offset * 0.5, y - offset * 0.5)
        cr.show_text(text)

        # 3. Main text on top
        cr.set_source_rgba(r, g, b, alpha)
        cr.move_to(x, y)
        cr.show_text(text)

    @staticmethod
    def get_cached_text(
        cr: cairo.Context,
        text: str,
        font_size: float,
        bold: bool,
        r: float,
        g: float,
        b: float,
    ) -> tuple[cairo.ImageSurface, float, float]:
        """Get or create a cached text surface with shadow halo.

        Returns (surface, width, height) where width/height are the
        padded surface dimensions. The text baseline is at pad_y from top.
        """
        font_family = _font_for_text(text)
        # Quantize to avoid cache explosion
        fs_key = round(font_size * 2)
        r8, g8, b8 = int(r * 255), int(g * 255), int(b * 255)
        key = (text, font_family, fs_key, bold, r8, g8, b8)

        cached = Layer._text_cache.get(key)
        if cached is not None:
            return cached

        weight = cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL
        cr.select_font_face(font_family, cairo.FONT_SLANT_NORMAL, weight)
        cr.set_font_size(font_size)
        extents = cr.text_extents(text)

        offset = max(1.0, font_size * 0.08)
        pad = int(offset * 2 + 2)
        w = int(extents.width + pad * 2 + 2)
        h = int(extents.height + pad * 2 + 2)
        if w <= 0 or h <= 0:
            # Degenerate — return a 1x1 surface
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
            result = (surf, 0.0, 0.0)
            Layer._text_cache[key] = result
            return result

        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        tc = cairo.Context(surf)
        tc.select_font_face(font_family, cairo.FONT_SLANT_NORMAL, weight)
        tc.set_font_size(font_size)

        tx = pad - extents.x_bearing
        ty = pad - extents.y_bearing  # y_bearing is negative (ascent above baseline)

        tc.set_source_rgba(0, 0, 0, 0.85)
        tc.move_to(tx + offset, ty + offset)
        tc.show_text(text)

        tc.set_source_rgba(0, 0, 0, 0.5)
        tc.move_to(tx - offset * 0.5, ty - offset * 0.5)
        tc.show_text(text)

        tc.set_source_rgba(r, g, b, 1.0)
        tc.move_to(tx, ty)
        tc.show_text(text)

        surf.flush()
        ascent = -extents.y_bearing  # positive distance from baseline to top
        result = (surf, extents.width, ascent)
        Layer._text_cache[key] = result
        return result

    @staticmethod
    def draw_cached_text(
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
    ) -> float:
        """Draw text using cached surface. Returns the text width."""
        surf, text_w, ascent = Layer.get_cached_text(cr, text, font_size, bold, r, g, b)
        if surf.get_width() <= 1:
            return 0.0

        offset = max(1.0, font_size * 0.08)
        pad = int(offset * 2 + 2)

        # Position: x,y is the text origin (left baseline)
        sx = x - pad
        # The baseline in the cached surface is at pad + ascent from top
        sy = y - pad - ascent

        cr.save()
        cr.set_source_surface(surf, sx, sy)
        if alpha < 0.99:
            cr.paint_with_alpha(alpha)
        else:
            cr.paint()
        cr.restore()
        return text_w
