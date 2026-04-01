"""Quick render of a replay."""
import sys, time
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
from renderer.layers.consumables import ConsumableLayer
from renderer.layers.smoke import SmokeLayer
from renderer.layers.aircraft import AircraftLayer
from renderer.layers.team_roster import TeamRosterLayer
from renderer.layers.right_panel import RightPanelLayer
from wows_replay_parser import parse_replay

REPLAY = sys.argv[1] if len(sys.argv) > 1 else "20260322_225740_PBSD598-Black-Cossack_15_NE_north.wowsreplay"
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else "output.mp4"
GAMEDATA = Path("wows-gamedata/data")

replay = parse_replay(REPLAY, str(GAMEDATA / "scripts_entity" / "entity_defs"))
print(f"Parsed: {replay.map_name}, {replay.duration:.0f}s, {len(replay.players)} players")

config = RenderConfig(gamedata_path=GAMEDATA, speed=20.0, fps=20, minimap_size=1080, panel_width=420)
renderer = MinimapRenderer(config)
for L in [MapBackgroundLayer(), TeamRosterLayer(), CapturePointLayer(), SmokeLayer(), ProjectileLayer(),
          AircraftLayer(), ShipLayer(), HealthBarLayer(), ConsumableLayer(), RightPanelLayer(), HudLayer()]:
    renderer.add_layer(L)

t0 = time.time()
renderer.render(replay, Path(OUTPUT),
    progress_callback=lambda c, t: print(f"  {c}/{t}") if c % 200 == 0 or c == t else None)
sz = Path(OUTPUT).stat().st_size
print(f"Done: {time.time()-t0:.1f}s, {sz/1024/1024:.2f} MB → {OUTPUT}")
