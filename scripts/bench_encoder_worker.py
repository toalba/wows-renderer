"""Worker subprocess for bench_encoder.py.

Renders one replay using the same configuration as render_quick.py and
prints a JSON results line on stdout, prefixed with __BENCH_RESULT__ so
the parent driver can locate it among any other output.

Designed to be invoked once per trial via subprocess so peak RSS can be
read in-process via getrusage(RUSAGE_SELF) without contamination from
prior trials in the same Python interpreter.

Usage:
  python scripts/bench_encoder_worker.py <replay.wowsreplay> <output.mp4>
"""
from __future__ import annotations

import json
import resource
import sys
from pathlib import Path

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

if len(sys.argv) != 3:
    sys.exit("usage: bench_encoder_worker.py <replay.wowsreplay> <output.mp4>")

REPLAY = sys.argv[1]
OUTPUT = sys.argv[2]
GAMEDATA_REPO = Path("wows-gamedata")

try:
    vgd = resolve_for_replay(REPLAY, GAMEDATA_REPO)
except RuntimeError:
    vgd = VersionedGamedata.from_gamedata_path(GAMEDATA_REPO / "data")

replay = parse_replay(REPLAY, str(vgd.entity_defs_path))

config = RenderConfig(
    gamedata_path=vgd.version_dir / "data",
    versioned_gamedata=vgd,
    speed=20.0,
    fps=20,
    minimap_size=1080,
    panel_width=420,
)
renderer = MinimapRenderer(config)
for L in [
    MapBackgroundLayer(), TeamRosterLayer(), CapturePointLayer(), WeatherLayer(),
    SmokeLayer(), ProjectileLayer(), AircraftLayer(), ShipLayer(),
    HealthBarLayer(), ConsumableLayer(), RightPanelLayer(), HudLayer(),
]:
    renderer.add_layer(L)

renderer.render(replay, Path(OUTPUT))

print("__BENCH_RESULT__", json.dumps({
    "render_phase_s": float(renderer.timings["render"]),
    "encode_phase_s": float(renderer.timings["encode"]),
    "frames": int(renderer.timings["frames"]),
    "output_bytes": Path(OUTPUT).stat().st_size,
    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
}))
