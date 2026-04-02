"""Render worker function for ProcessPoolExecutor.

All imports are inside the function body so it stays picklable.
"""
from __future__ import annotations

from multiprocessing import Queue

# Preset names — used by cog as app_commands.Choice values
PRESETS = ["full", "map", "playerdata"]


def render_replay(
    replay_path: str,
    output_path: str,
    gamedata_path: str,
    progress_queue: Queue | None = None,
    *,
    preset: str = "full",
    speed: float = 20.0,
    fps: int = 20,
    minimap_size: int = 1080,
    panel_width: int = 420,
) -> tuple[str, float, dict[str, float], str, int]:
    """Parse and render a replay to mp4. Runs in a worker process.

    Presets:
        full       — all layers, both side panels
        map        — minimap only (no roster/killfeed/ribbons), panel_width=0
        playerdata — no team roster, right panel only

    Sends (current_frame, total_frames) tuples to progress_queue
    every 50 frames (and on the last frame).

    Returns:
        (output_path, replay_duration, timings_dict, game_version, num_players)
    """
    from pathlib import Path
    from time import perf_counter

    from wows_replay_parser import parse_replay

    from renderer.config import RenderConfig
    from renderer.core import MinimapRenderer
    from renderer.layers.map_bg import MapBackgroundLayer
    from renderer.layers.ships import ShipLayer
    from renderer.layers.hud import HudLayer
    from renderer.layers.projectiles import ProjectileLayer
    from renderer.layers.capture_points import CapturePointLayer
    from renderer.layers.health_bars import HealthBarLayer
    from renderer.layers.consumables import ConsumableLayer
    from renderer.layers.smoke import SmokeLayer
    from renderer.layers.aircraft import AircraftLayer
    from renderer.layers.team_roster import TeamRosterLayer
    from renderer.layers.right_panel import RightPanelLayer

    timings: dict[str, float] = {}
    gd = Path(gamedata_path)

    if progress_queue:
        progress_queue.put(("status", "Parsing replay..."))

    # Phase 1: Parse replay (gamedata load + decrypt + decode + state tracking)
    t0 = perf_counter()
    replay = parse_replay(replay_path, str(gd / "scripts_entity" / "entity_defs"))
    timings["parse"] = perf_counter() - t0

    if progress_queue:
        progress_queue.put(("status", "Rendering... 0%"))

    # Adjust panel widths per preset
    left_pw: int | None = None
    right_pw: int | None = None
    if preset == "map":
        panel_width = 0
    elif preset == "playerdata":
        left_pw = 0
        right_pw = panel_width

    config = RenderConfig(
        gamedata_path=gd,
        speed=speed,
        fps=fps,
        minimap_size=minimap_size,
        panel_width=panel_width,
        left_panel_width=left_pw,
        right_panel_width=right_pw,
    )
    renderer = MinimapRenderer(config)

    # Common map layers (all presets)
    map_layers = [
        MapBackgroundLayer(), CapturePointLayer(), SmokeLayer(),
        ProjectileLayer(), AircraftLayer(), ShipLayer(),
        HealthBarLayer(), ConsumableLayer(), HudLayer(),
    ]

    if preset == "full":
        layers = [
            MapBackgroundLayer(), TeamRosterLayer(), CapturePointLayer(),
            SmokeLayer(), ProjectileLayer(), AircraftLayer(), ShipLayer(),
            HealthBarLayer(), ConsumableLayer(), RightPanelLayer(), HudLayer(),
        ]
    elif preset == "map":
        layers = map_layers
    elif preset == "playerdata":
        layers = map_layers[:-1] + [RightPanelLayer(), HudLayer()]
    else:
        layers = map_layers

    for layer in layers:
        renderer.add_layer(layer)

    def on_progress(current: int, total: int) -> None:
        if progress_queue and (current % 50 == 0 or current == total):
            progress_queue.put((current, total))

    # Phase 2+3: Render + Encode (timed inside MinimapRenderer.render)
    renderer.render(replay, Path(output_path), progress_callback=on_progress)

    timings["render"] = renderer.timings.get("render", 0.0)
    timings["encode"] = renderer.timings.get("encode", 0.0)
    timings["_frames"] = renderer.timings.get("frames", 0.0)

    game_type = replay.meta.get("gameType", "Unknown")
    return output_path, replay.duration, timings, replay.game_version, len(replay.players), game_type
