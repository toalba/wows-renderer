from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wows_replay_parser.api import ParsedReplay
    from wows_replay_parser.roster import PlayerInfo


@dataclass
class GameStateAdapter:
    """Bridges the replay parser to the renderer."""
    replay: ParsedReplay
    map_size: float  # space_size from map_sizes.json
    minimap_size: int
    panel_width: int
    player_lookup: dict[int, PlayerInfo]

    @classmethod
    def from_replay(cls, replay: ParsedReplay, minimap_size: int = 760, panel_width: int = 220, gamedata_path: object = None) -> GameStateAdapter:
        """Build an adapter from a parsed replay."""
        from pathlib import Path
        from renderer.assets import get_map_size

        player_lookup = {p.entity_id: p for p in replay.players}
        gp = Path(gamedata_path) if gamedata_path is not None else None
        map_size = get_map_size(replay.map_name, gp)

        return cls(
            replay=replay,
            map_size=map_size,
            minimap_size=minimap_size,
            panel_width=panel_width,
            player_lookup=player_lookup,
        )
