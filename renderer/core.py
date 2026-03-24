from __future__ import annotations
from pathlib import Path
from typing import Callable, TYPE_CHECKING

import cairo

from renderer.config import RenderConfig
from renderer.layers.base import Layer, RenderContext
from renderer.game_state import GameStateAdapter
from renderer.video import FFmpegPipe

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
            panel_width=config.panel_width,
            gamedata_path=config.gamedata_path,
        )

        render_ctx = RenderContext(
            config=config,
            replay=replay,
            map_size=adapter.map_size,
            player_lookup=adapter.player_lookup,
        )

        # Initialize all layers
        for layer in self.layers:
            layer.initialize(render_ctx)

        # Compute frame timestamps
        start = config.start_time
        end = config.end_time if config.end_time is not None else replay.duration
        dt = config.speed / config.fps  # game-seconds per frame

        total_frames = int((end - start) / dt) + 1

        # Create reusable cairo surface
        width = config.total_width
        height = config.total_height
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)

        # Open ffmpeg pipe
        with FFmpegPipe(output_path, width, height, config.fps, config.crf, config.codec) as pipe:
            t = start
            frame_idx = 0

            while t <= end:
                # 1. Clear surface (black)
                cr.set_source_rgb(0, 0, 0)
                cr.paint()

                # 2. Get game state at this timestamp
                state = replay.state_at(t)

                # 3. Draw all layers
                for layer in self.layers:
                    cr.save()
                    layer.render(cr, state, t)
                    cr.restore()

                # 4. Extract frame bytes and pipe to ffmpeg
                surface.flush()
                buf = surface.get_data()
                pipe.write_frame(bytes(buf))

                # 5. Progress
                frame_idx += 1
                if progress_callback:
                    progress_callback(frame_idx, total_frames)

                t += dt

        return output_path
