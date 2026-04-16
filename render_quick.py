"""Quick render of a replay."""
import sys
import time
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

if len(sys.argv) < 2:
    sys.exit("usage: render_quick.py <replay.wowsreplay> [output.mp4]")

REPLAY = sys.argv[1]
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else "output.mp4"
GAMEDATA_REPO = Path("wows-gamedata")

# Resolve version-specific gamedata (populates cache on first run)
try:
    vgd = resolve_for_replay(REPLAY, GAMEDATA_REPO)
except RuntimeError:
    # Fallback: cold-load from gamedata directory
    vgd = VersionedGamedata.from_gamedata_path(GAMEDATA_REPO / "data")

replay = parse_replay(REPLAY, str(vgd.entity_defs_path))
print(f"Parsed: {replay.map_name}, {replay.duration:.0f}s, {len(replay.players)} players")

config = RenderConfig(
    gamedata_path=vgd.version_dir / "data",
    versioned_gamedata=vgd,
    speed=20.0,
    fps=20,
    minimap_size=1080,
    panel_width=420,
)
renderer = MinimapRenderer(config)
for L in [MapBackgroundLayer(), TeamRosterLayer(), CapturePointLayer(), WeatherLayer(), SmokeLayer(), ProjectileLayer(),
          AircraftLayer(), ShipLayer(), HealthBarLayer(), ConsumableLayer(), RightPanelLayer(), HudLayer()]:
    renderer.add_layer(L)

t0 = time.time()
renderer.render(replay, Path(OUTPUT),
    progress_callback=lambda c, t: print(f"  {c}/{t}") if c % 200 == 0 or c == t else None)
sz = Path(OUTPUT).stat().st_size
print(f"Done: {time.time()-t0:.1f}s, {sz/1024/1024:.2f} MB → {OUTPUT}")
