"""Per-frame timing breakdown of the renderer.

Instruments each phase of the render loop to measure where time goes.
Outputs a summary table at the end.
"""
import sys
import time
from collections import defaultdict
from pathlib import Path

import cairo

from renderer.assets import load_ship_icons, load_ships_db
from renderer.config import RenderConfig
from renderer.game_state import GameStateAdapter
from renderer.layers.base import RenderContext
from renderer.layers.map_bg import MapBackgroundLayer
from renderer.layers.ships import ShipLayer
from renderer.layers.hud import HudLayer
from renderer.layers.projectiles import ProjectileLayer
from renderer.layers.capture_points import CapturePointLayer
from renderer.layers.health_bars import HealthBarLayer
from renderer.layers.consumables import ConsumableLayer
from renderer.layers.smoke import SmokeLayer
from renderer.layers.weather import WeatherLayer
from renderer.layers.aircraft import AircraftLayer
from renderer.layers.team_roster import TeamRosterLayer
from renderer.layers.right_panel import RightPanelLayer
from renderer.video import FFmpegPipe, FrameWriter
from wows_replay_parser import parse_replay

REPLAY = sys.argv[1] if len(sys.argv) > 1 else "20260322_225740_PBSD598-Black-Cossack_15_NE_north.wowsreplay"
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/profile_timed.mp4"
GAMEDATA = Path("wows-gamedata/data")

# --- Parse ---
t0 = time.perf_counter()
replay = parse_replay(REPLAY, str(GAMEDATA / "scripts_entity" / "entity_defs"))
parse_time = time.perf_counter() - t0
print(f"Parsed: {replay.map_name}, {replay.duration:.0f}s, {len(replay.players)} players ({parse_time:.2f}s)")

# --- Setup (same as render_quick.py) ---
config = RenderConfig(gamedata_path=GAMEDATA, speed=20.0, fps=20, minimap_size=1080, panel_width=420)

layers = [
    ("map_bg", MapBackgroundLayer()),
    ("team_roster", TeamRosterLayer()),
    ("capture_points", CapturePointLayer()),
    ("weather", WeatherLayer()),
    ("smoke", SmokeLayer()),
    ("projectiles", ProjectileLayer()),
    ("aircraft", AircraftLayer()),
    ("ships", ShipLayer()),
    ("health_bars", HealthBarLayer()),
    ("consumables", ConsumableLayer()),
    ("right_panel", RightPanelLayer()),
    ("hud", HudLayer()),
]

# --- Build context (mirrors core.py render()) ---
adapter = GameStateAdapter.from_replay(
    replay,
    minimap_size=config.minimap_size,
    panel_width=config.left_panel,
    gamedata_path=config.gamedata_path,
)
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

# Initialize layers
for name, layer in layers:
    layer.initialize(render_ctx)

# Compute timestamps (same logic as core.py)
start = config.start_time
if start == 0:
    tracker = replay.tracker
    battle_start = tracker.battle_start_time
    if battle_start is not None:
        start = max(0.0, battle_start - 10.0)
end = config.end_time if config.end_time is not None else replay.duration
dt = config.speed / config.fps

timestamps = []
t = start
while t <= end:
    timestamps.append(t)
    t += dt
total_frames = len(timestamps)
print(f"Frames: {total_frames}, dt={dt:.2f}s game-time/frame")

# Create surface
width = config.total_width
height = config.total_height
surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
cr = cairo.Context(surface)

# --- Timing accumulators ---
phase_times = defaultdict(float)
# Per-layer timing
layer_names = [name for name, _ in layers]

# --- Render loop with timing ---
state_iter = replay.iter_states(timestamps)
render_start = time.perf_counter()

with FFmpegPipe(OUTPUT, width, height, config.fps, config.crf, config.codec) as pipe:
    writer = FrameWriter(pipe)

    for frame_idx, (t, state) in enumerate(zip(timestamps, state_iter)):
        # Phase: clear
        t1 = time.perf_counter()
        cr.save()
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.restore()
        t2 = time.perf_counter()
        phase_times["clear"] += t2 - t1

        # Phase: each layer
        for name, layer in layers:
            t_layer_start = time.perf_counter()
            cr.save()
            layer.render(cr, state, t)
            cr.restore()
            t_layer_end = time.perf_counter()
            phase_times[name] += t_layer_end - t_layer_start

        # Phase: flush + encode
        t3 = time.perf_counter()
        surface.flush()
        writer.write_frame(surface.get_data())
        t4 = time.perf_counter()
        phase_times["encode"] += t4 - t3

        if frame_idx % 200 == 0:
            elapsed = time.perf_counter() - render_start
            fps_actual = (frame_idx + 1) / elapsed if elapsed > 0 else 0
            print(f"  frame {frame_idx}/{total_frames} ({fps_actual:.1f} fps)")

    writer.finish()

render_elapsed = time.perf_counter() - render_start

# Overhead = total - measured phases (iter_states, zip, loop bookkeeping)
measured_sum = sum(phase_times.values())
phase_times["overhead (iter_states + loop)"] = render_elapsed - measured_sum

# --- Report ---
print()
print("=" * 75)
print(f"  RENDER PROFILE — {total_frames} frames, {render_elapsed:.2f}s total")
print(f"  Replay: {REPLAY}")
print(f"  Resolution: {width}x{height}, speed={config.speed}x, fps={config.fps}")
print("=" * 75)
print()
print(f"{'Phase':<35} | {'Total(s)':>9} | {'Per-frame(ms)':>14} | {'% of total':>10}")
print("-" * 35 + "-+-" + "-" * 9 + "-+-" + "-" * 14 + "-+-" + "-" * 10)

# Order: clear, layers in order, encode, overhead
report_order = ["clear"] + layer_names + ["encode", "overhead (iter_states + loop)"]
for phase in report_order:
    total_s = phase_times[phase]
    per_frame_ms = (total_s / total_frames) * 1000 if total_frames > 0 else 0
    pct = (total_s / render_elapsed) * 100 if render_elapsed > 0 else 0
    print(f"{phase:<35} | {total_s:>9.3f} | {per_frame_ms:>13.2f} | {pct:>9.1f}%")

print("-" * 35 + "-+-" + "-" * 9 + "-+-" + "-" * 14 + "-+-" + "-" * 10)
per_frame_total = (render_elapsed / total_frames) * 1000 if total_frames > 0 else 0
print(f"{'TOTAL':<35} | {render_elapsed:>9.3f} | {per_frame_total:>13.2f} | {'100.0%':>10}")
print()

# Highlight bottlenecks (>5ms per frame)
print("BOTTLENECKS (>5ms per frame):")
print("-" * 50)
bottlenecks = []
for phase in report_order:
    total_s = phase_times[phase]
    per_frame_ms = (total_s / total_frames) * 1000 if total_frames > 0 else 0
    if per_frame_ms > 5.0:
        bottlenecks.append((phase, total_s, per_frame_ms))
        pct = (total_s / render_elapsed) * 100
        print(f"  {phase}: {per_frame_ms:.2f}ms/frame ({pct:.1f}% of total)")

if not bottlenecks:
    print("  (none — all phases under 5ms/frame)")

print()
sz = Path(OUTPUT).stat().st_size
print(f"Output: {sz / 1024 / 1024:.2f} MB → {OUTPUT}")
