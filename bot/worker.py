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

    from wows_replay_parser.api import (
        ParsedReplay,
        _load_gamedata_cached,
    )
    from wows_replay_parser.events.stream import EventStream
    from wows_replay_parser.gamedata.schema_builder import SchemaBuilder
    from wows_replay_parser.packets.decoder import PacketDecoder
    from wows_replay_parser.replay.reader import ReplayReader
    from wows_replay_parser.roster import build_roster
    from wows_replay_parser.state.tracker import GameStateTracker

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
    entity_defs_path = gd / "scripts_entity" / "entity_defs"

    if progress_queue:
        progress_queue.put(("status", "Loading gamedata..."))

    # Phase 1: Load gamedata (entity defs + alias registry)
    t0 = perf_counter()
    aliases, registry = _load_gamedata_cached(entity_defs_path)
    timings["gamedata_load"] = perf_counter() - t0

    # Phase 2: Read + decrypt replay file
    if progress_queue:
        progress_queue.put(("status", "Decrypting replay..."))
    t0 = perf_counter()
    reader = ReplayReader()
    replay_raw = reader.read(Path(replay_path))
    timings["replay_decrypt"] = perf_counter() - t0

    # Phase 3: Parse packets + build state
    if progress_queue:
        progress_queue.put(("status", "Parsing replay..."))
    t0 = perf_counter()

    # Type ID mapping from entities.xml
    entities_xml = entity_defs_path / "entities.xml"
    if not entities_xml.exists():
        entities_xml = entity_defs_path.parent / "entities.xml"
    if entities_xml.exists():
        from lxml import etree as _et

        _tree = _et.parse(str(entities_xml))
        _root = _tree.getroot()
        _cs = _root.find("ClientServerEntities")
        if _cs is not None:
            for idx, child in enumerate(
                c for c in _cs if isinstance(c.tag, str)
            ):
                registry.register_type_id(idx + 1, child.tag)

    schema = SchemaBuilder(aliases, registry)
    tracker = GameStateTracker()
    decoder = PacketDecoder(schema, registry, tracker=tracker)
    packets = decoder.decode_stream(replay_raw.packet_data)

    stream = EventStream(tracker=tracker, gamedata_path=entity_defs_path)
    events = stream.process(packets)

    players = build_roster(
        replay_raw.meta, tracker, packets=packets, registry=registry,
        gamedata_path=entity_defs_path,
    )
    for player in players:
        if player.entity_id:
            tracker.inject_property(
                player.entity_id, "teamId", player.team_id,
            )

    duration = max((p.timestamp for p in packets), default=0.0)

    replay = ParsedReplay(
        meta=replay_raw.meta,
        players=players,
        map_name=replay_raw.map_name,
        game_version=replay_raw.game_version,
        duration=duration,
        events=events,
        packets=packets,
        _tracker=tracker,
    )
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

    # Phase 4+5: Render + Encode (timed inside MinimapRenderer.render)
    renderer.render(replay, Path(output_path), progress_callback=on_progress)

    timings["render"] = renderer.timings.get("render", 0.0)
    timings["encode"] = renderer.timings.get("encode", 0.0)
    timings["_frames"] = renderer.timings.get("frames", 0.0)

    return output_path, replay.duration, timings, replay.game_version, len(players)
