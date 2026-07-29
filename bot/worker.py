"""Render worker function for ProcessPoolExecutor.

All imports are inside the function body so it stays picklable.
"""
from __future__ import annotations

import logging
import os
import sys
from multiprocessing import Queue

log = logging.getLogger(__name__)

# Preset names — used by cog as app_commands.Choice values
PRESETS = ["full", "map", "playerdata"]


def _peak_rss_bytes() -> float:
    """Peak resident set size of this worker process, in bytes.

    Reported back through the ``timings`` dict rather than the return tuple,
    which is unpacked at three call sites and contract-tested.

    ``ru_maxrss`` is a high-water mark that never resets, so with
    ``RENDER_MAX_TASKS_PER_CHILD`` unset (the prod default — long-lived forked
    workers) this covers the worker's entire lifetime, not one render. That is
    still the signal we want: whether a worker is creeping toward the 4.5 GB
    cgroup cap.

    Returns 0.0 where unavailable — ``resource`` is Unix-only.
    """
    try:
        import resource
    except ImportError:  # Windows
        return 0.0
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS reports bytes.
    return float(usage) if sys.platform == "darwin" else float(usage) * 1024


def _format_chat_log(replay) -> str:
    """Format ChatEvents as a plain-text transcript.

    Each line: `[mm:ss] [T|P|C] PlayerName: message` (T=team, P=prebattle,
    C=common). Unknown senders fall back to `Player#<id>`. Returns "" if
    the replay carried no chat (which is common in solo-queue replays).
    """
    from wows_replay_parser.events.models import ChatEvent

    name_by_account = {p.account_id: p.name for p in replay.players}
    lines: list[str] = []
    for event in replay.events:
        if not isinstance(event, ChatEvent) or not event.message:
            continue
        mins, secs = divmod(int(event.timestamp), 60)
        if event.channel == "battle_team":
            ch = "T"
        elif event.channel == "battle_prebattle":
            ch = "P"
        else:
            ch = "C"
        name = name_by_account.get(event.sender_id) or f"Player#{event.sender_id}"
        lines.append(f"[{mins:02d}:{secs:02d}] [{ch}] {name}: {event.message}")
    return "\n".join(lines)


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
    flags: frozenset[str] = frozenset(),
    theme: str = "default",
) -> tuple[str, float, dict[str, float], str, int, str, list, str]:
    """Parse and render a replay to mp4. Runs in a worker process.

    Presets:
        full       — all layers, both side panels
        map        — minimap only (no roster/killfeed/ribbons), panel_width=0
        playerdata — no team roster, right panel only

    Sends (current_frame, total_frames) tuples to progress_queue
    every 50 frames (and on the last frame).

    Returns:
        (output_path, replay_duration, timings, game_version, num_players,
         game_type, build_urls, chat_text)
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
    from renderer.layers.watermark import WatermarkLayer
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
        flags=flags,
        theme=theme,
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

    layers.append(WatermarkLayer())

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

    chat_text = _format_chat_log(replay)
    game_type = replay.meta.get("gameType", "Unknown")
    timings["_peak_rss_bytes"] = _peak_rss_bytes()
    return (
        output_path, replay.duration, timings, replay.game_version,
        len(replay.players), game_type, build_urls, chat_text,
    )


def render_dual_replay(
    replay_a_path: str,
    replay_b_path: str,
    output_path: str,
    gamedata_path: str,
    progress_queue: Queue | None = None,
    *,
    speed: float = 20.0,
    fps: int = 20,
    minimap_size: int = 1080,
    panel_width: int = 420,
    flags: frozenset[str] = frozenset(),
    theme: str = "default",
) -> tuple[str, float, dict[str, float], str, int, str, list, str]:
    """Dual-perspective merged render of two replays from the same match.

    Returns the same tuple shape as render_replay so the cog handler can
    unpack uniformly. build_urls is always empty (no self-player in the
    merged/neutral-observer view); chat_text aggregates ChatEvents from
    the merged stream so dedup matches what the killfeed layer shows.
    """
    from pathlib import Path
    from time import perf_counter

    from wows_replay_parser import parse_replay
    from wows_replay_parser.merge import merge_replays

    from renderer.config import RenderConfig
    from renderer.core import DualMinimapRenderer
    from renderer.gamedata_cache import VersionedGamedata, resolve_for_replay
    from renderer.layers.aircraft import AircraftLayer
    from renderer.layers.capture_points import CapturePointLayer
    from renderer.layers.consumables import ConsumableLayer
    from renderer.layers.health_bars import HealthBarLayer
    from renderer.layers.hud import HudLayer
    from renderer.layers.killfeed import KillfeedLayer
    from renderer.layers.map_bg import MapBackgroundLayer
    from renderer.layers.projectiles import ProjectileLayer
    from renderer.layers.ships import ShipLayer
    from renderer.layers.smoke import SmokeLayer
    from renderer.layers.team_roster import TeamRosterLayer
    from renderer.layers.watermark import WatermarkLayer
    from renderer.layers.weather import WeatherLayer

    timings: dict[str, float] = {}
    gamedata_repo = Path(gamedata_path).parent

    log.info("worker: dual start replay_a=%s replay_b=%s",
             Path(replay_a_path).name, Path(replay_b_path).name)
    if progress_queue:
        progress_queue.put(("status", "Parsing replays..."))

    # Both replays belong to the same match → identical client build → same cache.
    t0 = perf_counter()
    try:
        vgd = resolve_for_replay(replay_a_path, gamedata_repo)
    except RuntimeError:
        vgd = VersionedGamedata.from_gamedata_path(Path(gamedata_path))
    timings["resolve"] = perf_counter() - t0

    t1 = perf_counter()
    replay_a = parse_replay(replay_a_path, str(vgd.entity_defs_path))
    replay_b = parse_replay(replay_b_path, str(vgd.entity_defs_path))
    merged = merge_replays(replay_a, replay_b)
    timings["parse"] = perf_counter() - t1
    log.info("worker: parsed+merged in %.2fs (%d players, map=%s)",
             timings["parse"], len(merged.players), merged.map_name)

    if progress_queue:
        progress_queue.put(("status", "Rendering... 0%"))

    config = RenderConfig(
        gamedata_path=vgd.version_dir / "data",
        versioned_gamedata=vgd,
        speed=speed,
        fps=fps,
        minimap_size=minimap_size,
        panel_width=panel_width,
        flags=flags,
        theme=theme,
    )
    renderer = DualMinimapRenderer(config, replay=merged)

    # Dual layer set — no self-centric layers (player_header, damage_stats,
    # ribbons, right_panel). Killfeed stays (server-authoritative). Trails
    # are omitted: the neutral-observer view is busy enough with both teams
    # and the fading lines add clutter without referee value.
    for layer in [
        MapBackgroundLayer(), TeamRosterLayer(), CapturePointLayer(),
        WeatherLayer(), SmokeLayer(), ProjectileLayer(), AircraftLayer(),
        ShipLayer(), HealthBarLayer(), ConsumableLayer(),
        KillfeedLayer(), HudLayer(), WatermarkLayer(),
    ]:
        renderer.add_layer(layer)

    def on_progress(current: int, total: int) -> None:
        if progress_queue and (current % 50 == 0 or current == total):
            progress_queue.put((current, total))

    log.info("worker: dual entering render loop")
    renderer.render(merged, Path(output_path), progress_callback=on_progress)
    log.info(
        "worker: dual render loop finished (render=%.2fs encode=%.2fs frames=%d)",
        renderer.timings.get("render", 0.0),
        renderer.timings.get("encode", 0.0),
        int(renderer.timings.get("frames", 0.0)),
    )

    timings["render"] = renderer.timings.get("render", 0.0)
    timings["encode"] = renderer.timings.get("encode", 0.0)
    timings["_frames"] = renderer.timings.get("frames", 0.0)
    timings["setup"] = renderer.timings.get("setup", 0.0)
    timings["layer_init"] = renderer.timings.get("layer_init", {})

    # Dual mode: no self-player → no build URLs.
    timings["build_urls"] = 0.0

    chat_text = _format_chat_log(merged)
    game_type = merged.meta.get("gameType", "Unknown") if hasattr(merged, "meta") else "Unknown"
    timings["_peak_rss_bytes"] = _peak_rss_bytes()
    return (
        output_path, merged.duration, timings, merged.game_version,
        len(merged.players), game_type, [], chat_text,
    )
