"""Test: render the full replay."""
import traceback
import time
from pathlib import Path
from renderer.config import RenderConfig
from renderer.core import MinimapRenderer
from renderer.layers.map_bg import MapBackgroundLayer
from renderer.layers.ships import ShipLayer
from renderer.layers.trails import TrailLayer
from renderer.layers.hud import HudLayer
from renderer.layers.projectiles import ProjectileLayer
from renderer.layers.capture_points import CapturePointLayer
from renderer.layers.health_bars import HealthBarLayer
from wows_replay_parser import parse_replay

REPLAY = Path("20260322_172639_PHSC710-Prins-Van-Oranje_56_AngelWings.wowsreplay")
GAMEDATA = Path("../wows-gamedata/data")
ENTITY_DEFS = GAMEDATA / "scripts_entity" / "entity_defs"
OUTPUT = Path("test_output.mp4")

try:
    print(f"Parsing replay: {REPLAY}")
    t0 = time.time()
    replay = parse_replay(str(REPLAY), str(ENTITY_DEFS))
    print(f"  Map: {replay.map_name}")
    print(f"  Duration: {replay.duration:.0f}s")
    print(f"  Players: {len(replay.players)}")
    print(f"  Parsed in {time.time() - t0:.1f}s")

    config = RenderConfig(
        gamedata_path=GAMEDATA,
        speed=20.0,
        fps=20,
        start_time=0.0,
        end_time=None,  # Full replay
    )

    renderer = MinimapRenderer(config)
    renderer.add_layer(MapBackgroundLayer())
    renderer.add_layer(CapturePointLayer())
    renderer.add_layer(TrailLayer())
    renderer.add_layer(ProjectileLayer())
    renderer.add_layer(ShipLayer())
    renderer.add_layer(HealthBarLayer())
    renderer.add_layer(HudLayer())

    frame_count = 0
    t_render = time.time()
    def progress(current, total):
        global frame_count
        frame_count = current
        if current % 50 == 0 or current == total:
            elapsed = time.time() - t_render
            fps = current / elapsed if elapsed > 0 else 0
            print(f"  Frame {current}/{total} ({fps:.1f} fps)", flush=True)

    print(f"Rendering to {OUTPUT} ...", flush=True)
    renderer.render(replay, OUTPUT, progress_callback=progress)
    total_time = time.time() - t0
    print(f"Done! {frame_count} frames in {total_time:.1f}s")
except Exception:
    traceback.print_exc()
