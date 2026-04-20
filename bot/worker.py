"""Render worker function for ProcessPoolExecutor.

All imports are inside the function body so it stays picklable.
"""
from __future__ import annotations

import logging
import os
from multiprocessing import Queue

log = logging.getLogger(__name__)

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
    from renderer.gamedata_cache import VersionedGamedata, resolve_for_replay
    from renderer.layers.aircraft import AircraftLayer
    from renderer.layers.capture_points import CapturePointLayer
    from renderer.layers.consumables import ConsumableLayer
    from renderer.layers.health_bars import HealthBarLayer
    from renderer.layers.hud import HudLayer
    from renderer.layers.map_bg import MapBackgroundLayer
    from renderer.layers.projectiles import ProjectileLayer
    from renderer.layers.right_panel import RightPanelLayer
    from renderer.layers.ships import ShipLayer
    from renderer.layers.smoke import SmokeLayer
    from renderer.layers.team_roster import TeamRosterLayer
    from renderer.layers.weather import WeatherLayer

    timings: dict[str, float] = {}
    gamedata_repo = Path(gamedata_path).parent  # gamedata_path is repo/data, parent is repo

    log.info("worker: start replay=%s preset=%s", Path(replay_path).name, preset)
    if progress_queue:
        progress_queue.put(("status", "Parsing replay..."))

    # Resolve version-specific gamedata
    t0 = perf_counter()
    try:
        vgd = resolve_for_replay(replay_path, gamedata_repo)
    except RuntimeError:
        # Fallback: cold-load from provided gamedata directory
        vgd = VersionedGamedata.from_gamedata_path(Path(gamedata_path))
    timings["resolve"] = perf_counter() - t0
    log.info("worker: resolved gamedata in %.2fs", timings["resolve"])

    # Phase 1: Parse replay
    t1 = perf_counter()
    replay = parse_replay(replay_path, str(vgd.entity_defs_path))
    timings["parse"] = perf_counter() - t1
    log.info("worker: parsed replay in %.2fs (%d players)", timings["parse"], len(replay.players))

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
        gamedata_path=vgd.version_dir / "data",
        versioned_gamedata=vgd,
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
        MapBackgroundLayer(), CapturePointLayer(), WeatherLayer(),
        SmokeLayer(), ProjectileLayer(), AircraftLayer(), ShipLayer(),
        HealthBarLayer(), ConsumableLayer(), HudLayer(),
    ]

    if preset == "full":
        layers = [
            MapBackgroundLayer(), TeamRosterLayer(), CapturePointLayer(),
            WeatherLayer(), SmokeLayer(), ProjectileLayer(), AircraftLayer(),
            ShipLayer(), HealthBarLayer(), ConsumableLayer(),
            RightPanelLayer(), HudLayer(),
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
    log.info("worker: entering render loop")
    renderer.render(replay, Path(output_path), progress_callback=on_progress)
    log.info(
        "worker: render loop finished (render=%.2fs encode=%.2fs frames=%d)",
        renderer.timings.get("render", 0.0),
        renderer.timings.get("encode", 0.0),
        int(renderer.timings.get("frames", 0.0)),
    )

    timings["render"] = renderer.timings.get("render", 0.0)
    timings["encode"] = renderer.timings.get("encode", 0.0)
    timings["_frames"] = renderer.timings.get("frames", 0.0)
    timings["setup"] = renderer.timings.get("setup", 0.0)
    timings["layer_init"] = renderer.timings.get("layer_init", {})

    # Generate ShipBuilder build URLs for all players.
    # Disabled by default — the feature walks the ~15 MB gameparams dict
    # and has historically been a perf footgun plus the resulting embed
    # regularly exceeds Discord's 1024-char per-field limit. Set
    # ENABLE_BUILD_URLS=true in .env to re-enable for A/B testing.
    build_urls: list[tuple[str, str, int, str | None]] = []
    t_builds = perf_counter()
    if os.environ.get("ENABLE_BUILD_URLS", "false").lower() in ("1", "true", "yes"):
        try:
            from renderer.build_export import generate_all_build_urls
            build_urls = generate_all_build_urls(replay, vgd)
        except Exception:
            log.exception("worker: build_urls generation failed")
    timings["build_urls"] = perf_counter() - t_builds
    log.info(
        "worker: build_urls %s in %.2fs (%d entries)",
        "generated" if build_urls else "skipped",
        timings["build_urls"], len(build_urls),
    )

    game_type = replay.meta.get("gameType", "Unknown")
    return output_path, replay.duration, timings, replay.game_version, len(replay.players), game_type, build_urls
