from __future__ import annotations
from pathlib import Path
from typing import Callable, TYPE_CHECKING

import cairo

from renderer.assets import load_ship_icons, load_ships_db
from renderer.config import RenderConfig
from renderer.layers.base import Layer, RenderContext
from renderer.game_state import GameStateAdapter
from renderer.video import FFmpegPipe, FrameWriter

if TYPE_CHECKING:
    from wows_replay_parser.api import ParsedReplay


class MinimapRenderer:
    """Orchestrates the rendering pipeline.

    Manages layers, the frame loop, and video encoding.
    All layers draw on a shared cairo context — no separate images.
    """

    def __init__(self, config: RenderConfig, replay: ParsedReplay | None = None) -> None:
        self.config = config
        self.replay = replay
        self.layers: list[Layer] = []

    @classmethod
    def from_replay_file(
        cls,
        replay_path: str | Path,
        config: RenderConfig,
        *,
        entity_defs_path: str | Path | None = None,
        auto_update_gamedata: bool = False,
    ) -> MinimapRenderer:
        """Create a renderer with a parsed replay from the local replay parser.

        Args:
            replay_path: Path to the .wowsreplay file.
            config: Render configuration.
            entity_defs_path: Path to entity_defs dir. Defaults to
                ``config.gamedata_path / "scripts_entity" / "entity_defs"``.
            auto_update_gamedata: If True, auto-sync gamedata to match
                the replay version.

        Returns:
            A MinimapRenderer with the replay already parsed and attached.
        """
        from wows_replay_parser import parse_replay

        if entity_defs_path is None:
            entity_defs_path = Path(config.gamedata_path) / "scripts_entity" / "entity_defs"

        replay = parse_replay(
            str(replay_path),
            str(entity_defs_path),
            auto_update_gamedata=auto_update_gamedata,
        )
        return cls(config, replay=replay)

    @staticmethod
    def _detect_battle_start(replay: ParsedReplay) -> float:
        """Find when battleStage transitions to 0 (battle active).

        battleStage values: 2=loading, 1=countdown, 0=active, 3=ended.
        """
        tracker = replay._tracker
        for change in tracker._history:
            if change.property_name == "battleStage" and change.new_value == 0:
                return change.timestamp
        return 0.0

    def add_layer(self, layer: Layer) -> None:
        """Add a layer to the rendering stack. First added = bottom."""
        self.layers.append(layer)

    def render(
        self,
        replay: ParsedReplay | None = None,
        output_path: str | Path = "output.mp4",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Render a replay to an mp4 file.

        Args:
            replay: Parsed replay from wows-replay-parser. If None, uses the
                replay attached via ``from_replay_file`` or the constructor.
            output_path: Where to write the mp4.
            progress_callback: Optional (current_frame, total_frames) callback.

        Returns:
            Path to the output file.

        Raises:
            ValueError: If no replay is available.
        """
        if replay is None:
            replay = self.replay
        if replay is None:
            raise ValueError(
                "No replay provided. Either pass one to render() or use "
                "MinimapRenderer.from_replay_file() to attach one."
            )
        output_path = Path(output_path)
        config = self.config

        # Build adapter and context
        adapter = GameStateAdapter.from_replay(
            replay,
            minimap_size=config.minimap_size,
            panel_width=config.left_panel,
            gamedata_path=config.gamedata_path,
        )

        # Load ship database and icons
        gp = Path(config.gamedata_path)
        ship_db = load_ships_db(gp)
        ship_icons = load_ship_icons(gp, config.team_colors, config.self_color)

        render_ctx = RenderContext(
            config=config,
            replay=replay,
            map_size=adapter.map_size,
            player_lookup=adapter.player_lookup,
            ship_db=ship_db,
            ship_icons=ship_icons,
        )

        # Initialize all layers
        for layer in self.layers:
            layer.initialize(render_ctx)

        # Compute frame timestamps
        start = config.start_time
        if start == 0:
            # Auto-detect match start from battleStage (0 = battle active)
            # Start 10s early so the video begins gracefully during countdown
            start = max(0.0, self._detect_battle_start(replay) - 10.0)
        end = config.end_time if config.end_time is not None else replay.duration
        dt = config.speed / config.fps  # game-seconds per frame

        # Use index-based computation to avoid float accumulation drift
        import math
        total_frames = int(math.floor((end - start) / dt)) + 1
        timestamps = [start + i * dt for i in range(total_frames)]

        # Create reusable cairo surface
        width = config.total_width
        height = config.total_height
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)

        # Open ffmpeg pipe with async frame writer
        with FFmpegPipe(output_path, width, height, config.fps, config.crf, config.codec) as pipe:
            writer = FrameWriter(pipe)

            # Use iter_states for O(delta) incremental state queries
            # instead of state_at() which is O(history) per frame.
            state_iter = replay.iter_states(timestamps)

            for frame_idx, (t, state) in enumerate(zip(timestamps, state_iter)):
                # 1. Clear surface
                cr.save()
                cr.set_operator(cairo.OPERATOR_CLEAR)
                cr.paint()
                cr.restore()

                # 2. Draw all layers
                for layer in self.layers:
                    cr.save()
                    layer.render(cr, state, t)
                    cr.restore()

                # 3. Copy frame data and write async (pipe I/O in background thread)
                surface.flush()
                writer.write_frame(surface.get_data())

                # 4. Progress
                if progress_callback:
                    progress_callback(frame_idx + 1, total_frames)

            writer.finish()

        return output_path
