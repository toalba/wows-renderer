from __future__ import annotations
from pathlib import Path
from time import perf_counter
from typing import Callable, TYPE_CHECKING

import cairo

from renderer.assets import (
    _load_consumable_type_ids,
    get_map_size,
    load_ship_icons,
    load_ships_db,
)
from renderer.config import RenderConfig
from renderer.layers.base import (
    BaseRenderContext,
    DualRenderContext,
    Layer,
    SingleRenderContext,
)
from renderer.game_state import GameStateAdapter
from renderer.video import FFmpegPipe, FrameWriter

if TYPE_CHECKING:
    from wows_replay_parser.api import ParsedReplay
    from wows_replay_parser.interfaces import ReplaySource
    from wows_replay_parser.merge import MergedReplay


class BaseMinimapRenderer:
    """Shared infrastructure for single- and dual-perspective renderers.

    Owns the layer list, frame loop, ffmpeg pipe, timestamp computation, and
    per-phase timing dict. Subclasses provide a concrete render context by
    implementing :meth:`_build_context`.
    """

    def __init__(self, config: RenderConfig) -> None:
        self.config = config
        self.layers: list[Layer] = []
        self.timings: dict[str, object] = {}  # populated after render()

    def add_layer(self, layer: Layer) -> None:
        """Add a layer to the rendering stack. First added = bottom."""
        self.layers.append(layer)

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------
    def _build_context(
        self,
        replay: "ReplaySource",
        gamedata_path: Path,
    ) -> BaseRenderContext:
        """Build the render context for ``replay``.

        Subclasses return either a :class:`SingleRenderContext` or a
        :class:`DualRenderContext`.
        """
        raise NotImplementedError

    @staticmethod
    def _battle_start_time(replay: "ReplaySource") -> float:
        """Return the timestamp where the battle becomes active."""
        battle_start = getattr(replay, "battle_start_time", None)
        return battle_start if battle_start is not None else 0.0

    # ------------------------------------------------------------------
    # Shared render loop
    # ------------------------------------------------------------------
    def _render_frames(
        self,
        replay: "ReplaySource",
        render_ctx: BaseRenderContext,
        output_path: Path,
        progress_callback: Callable[[int, int], None] | None,
        t_setup: float,
    ) -> Path:
        config = self.config

        # Initialize all layers
        layer_timings: dict[str, float] = {}
        for layer in self.layers:
            t_layer = perf_counter()
            layer.initialize(render_ctx)
            layer_timings[type(layer).__name__] = perf_counter() - t_layer

        self.timings["setup"] = perf_counter() - t_setup
        self.timings["layer_init"] = layer_timings

        # Compute frame timestamps
        start = config.start_time
        if start == 0:
            # Auto-detect match start from battleStage (0 = battle active).
            # Start 10s early so the video begins gracefully during countdown.
            start = max(0.0, self._battle_start_time(replay) - 10.0)
        end = config.end_time if config.end_time is not None else replay.duration
        dt = config.speed / config.fps  # game-seconds per frame

        # Index-based computation avoids float accumulation drift.
        import math
        total_frames = int(math.floor((end - start) / dt)) + 1
        timestamps = [start + i * dt for i in range(total_frames)]

        # Create reusable cairo surface
        width = config.total_width
        height = config.total_height
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)

        # Open ffmpeg pipe — manual close so we can time encode separately
        pipe = FFmpegPipe(output_path, width, height, config.fps, config.crf, config.codec)
        try:
            t_render_start = perf_counter()
            writer = FrameWriter(pipe, maxsize=16)

            # iter_states for O(delta) incremental state queries instead of
            # state_at() which is O(history) per frame.
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
            t_render_end = perf_counter()

            # Close pipe: flushes stdin + waits for ffmpeg to finish encoding.
            pipe.close()
            t_encode_end = perf_counter()
        except Exception:
            pipe.close()
            raise

        self.timings["render"] = t_render_end - t_render_start
        self.timings["encode"] = t_encode_end - t_render_end
        self.timings["frames"] = total_frames

        return output_path

    def render(
        self,
        replay: "ReplaySource",
        output_path: str | Path = "output.mp4",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Render ``replay`` to an mp4 using this renderer's layer stack."""
        if replay is None:
            raise ValueError("render() requires a replay (ReplaySource).")
        output_path = Path(output_path)
        gp = self.config.effective_gamedata_path
        t_setup = perf_counter()
        render_ctx = self._build_context(replay, gp)
        return self._render_frames(
            replay, render_ctx, output_path, progress_callback, t_setup,
        )


class MinimapRenderer(BaseMinimapRenderer):
    """Single-perspective renderer.

    Builds a :class:`SingleRenderContext` from a :class:`ParsedReplay` via
    :class:`GameStateAdapter`. Self-team detection, division mates, and the
    Trap-5 perspective swap all live on the context. This is the renderer
    used by the Discord bot and quick-render for regular single-replay jobs.
    """

    def __init__(self, config: RenderConfig, replay: "ParsedReplay | None" = None) -> None:
        super().__init__(config)
        self.replay = replay

    @classmethod
    def from_replay_file(
        cls,
        replay_path: str | Path,
        config: RenderConfig,
        *,
        entity_defs_path: str | Path | None = None,
        auto_update_gamedata: bool = False,
    ) -> "MinimapRenderer":
        """Create a renderer with a parsed replay from the local replay parser.

        Args:
            replay_path: Path to the .wowsreplay file.
            config: Render configuration.
            entity_defs_path: Path to entity_defs dir. Defaults to
                ``config.gamedata_path / "scripts_entity" / "entity_defs"``.
            auto_update_gamedata: Deprecated. Use ``resolve_for_replay()``
                from ``renderer.gamedata_cache`` instead, which uses the
                per-version cache system without git checkout.

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
    def _detect_battle_start(replay: "ParsedReplay") -> float:
        """Back-compat shim (pre-refactor name). Delegates to the base class."""
        return BaseMinimapRenderer._battle_start_time(replay)

    def _build_context(
        self,
        replay: "ReplaySource",
        gamedata_path: Path,
    ) -> SingleRenderContext:
        adapter = GameStateAdapter.from_replay(
            replay,  # type: ignore[arg-type]
            minimap_size=self.config.minimap_size,
            panel_width=self.config.left_panel,
            gamedata_path=gamedata_path,
        )

        # Load ship database, icons, and consumable type IDs
        ship_db = load_ships_db(gamedata_path)
        ship_icons = load_ship_icons(
            gamedata_path, self.config.team_colors, self.config.self_color,
        )
        _load_consumable_type_ids(gamedata_path)

        # Resolve recording_player_id from the self-player (relation == 0).
        recording_player_id: int | None = None
        for eid, player in adapter.player_lookup.items():
            if getattr(player, "relation", None) == 0:
                recording_player_id = eid
                break

        return SingleRenderContext(
            config=self.config,
            replay=replay,
            map_size=adapter.map_size,
            player_lookup=adapter.player_lookup,
            ship_db=ship_db,
            ship_icons=ship_icons,
            recording_player_id=recording_player_id,
        )

    def render(  # type: ignore[override]
        self,
        replay: "ParsedReplay | None" = None,
        output_path: str | Path = "output.mp4",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Render a replay to an mp4 file.

        Args:
            replay: Parsed replay from wows-replay-parser. If ``None``, uses
                the replay attached via :meth:`from_replay_file` or the
                constructor.
            output_path: Where to write the mp4.
            progress_callback: Optional ``(current_frame, total_frames)``
                callback invoked once per frame after it has been queued
                for encoding.

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
        return super().render(replay, output_path, progress_callback)


class DualMinimapRenderer(BaseMinimapRenderer):
    """Neutral-observer renderer for a :class:`MergedReplay`.

    Team 0 is always rendered green (left/ally side), team 1 is always red
    (right/enemy side). No Trap-5 perspective swap, no recording player, no
    division highlighting. Self-centric layers (``player_header``,
    ``damage_stats``, ``ribbons``, ``killfeed``, ``right_panel``) must be
    omitted by the caller — they type against :class:`SingleRenderContext`
    and won't accept a :class:`DualRenderContext`.
    """

    def __init__(self, config: RenderConfig, replay: "MergedReplay | None" = None) -> None:
        super().__init__(config)
        self.replay = replay

    @classmethod
    def from_replay_files(
        cls,
        replay_a_path: str | Path,
        replay_b_path: str | Path,
        config: RenderConfig,
        *,
        entity_defs_path: str | Path | None = None,
    ) -> "DualMinimapRenderer":
        """Parse both replays and construct a merged renderer."""
        from wows_replay_parser import parse_replay
        from wows_replay_parser.merge import merge_replays

        if entity_defs_path is None:
            entity_defs_path = Path(config.gamedata_path) / "scripts_entity" / "entity_defs"

        replay_a = parse_replay(str(replay_a_path), str(entity_defs_path))
        replay_b = parse_replay(str(replay_b_path), str(entity_defs_path))
        merged = merge_replays(replay_a, replay_b)
        return cls(config, replay=merged)

    @staticmethod
    def _extract_replay_meta(replay: "ParsedReplay") -> dict:
        """Pull a minimal label-friendly dict from a single replay's meta."""
        meta = replay.meta
        return {
            "player_name": meta.get("playerName", ""),
            "ship_id": meta.get("playerVehicle", ""),
        }

    def _build_context(
        self,
        replay: "ReplaySource",
        gamedata_path: Path,
    ) -> DualRenderContext:
        """Build a :class:`DualRenderContext` for a merged replay.

        Does not use :class:`GameStateAdapter` — the merged replay already
        exposes a unified :class:`~wows_replay_parser.interfaces.ReplaySource`
        surface.
        """
        # Dual mode uses team colors for both sides; there is no "self" ship
        # that needs the white-tinted icon variant.
        ship_db = load_ships_db(gamedata_path)
        ship_icons = load_ship_icons(gamedata_path, self.config.team_colors, None)
        _load_consumable_type_ids(gamedata_path)

        player_lookup = {p.entity_id: p for p in replay.players if p.entity_id}
        map_size = get_map_size(replay.map_name, gamedata_path)

        # Optional per-side labels (pull from the underlying single replays
        # if the merged replay exposes them; fall back to empty dicts).
        replay_a_meta: dict = {}
        replay_b_meta: dict = {}
        ra = getattr(replay, "replay_a", None)
        rb = getattr(replay, "replay_b", None)
        if ra is not None:
            replay_a_meta = self._extract_replay_meta(ra)
        if rb is not None:
            replay_b_meta = self._extract_replay_meta(rb)

        return DualRenderContext(
            config=self.config,
            replay=replay,
            map_size=map_size,
            player_lookup=player_lookup,
            ship_db=ship_db,
            ship_icons=ship_icons,
            replay_a_meta=replay_a_meta,
            replay_b_meta=replay_b_meta,
        )

    def render(  # type: ignore[override]
        self,
        replay: "MergedReplay | None" = None,
        output_path: str | Path = "output_dual.mp4",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Render a :class:`MergedReplay` to an mp4 file."""
        if replay is None:
            replay = self.replay
        if replay is None:
            raise ValueError(
                "No merged replay provided. Pass one to render() or use "
                "DualMinimapRenderer.from_replay_files() to attach one."
            )
        return super().render(replay, output_path, progress_callback)
