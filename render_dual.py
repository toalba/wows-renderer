"""Dual-perspective render: merge two replays from the same match."""
import sys, time
from pathlib import Path
from renderer.config import RenderConfig
from renderer.core import DualMinimapRenderer
from renderer.gamedata_cache import resolve_for_replay, VersionedGamedata
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
from renderer.layers.trails import TrailLayer
from renderer.layers.killfeed import KillfeedLayer
from wows_replay_parser import parse_replay
from wows_replay_parser.merge import merge_replays

REPLAY_A = sys.argv[1]
REPLAY_B = sys.argv[2]
OUTPUT = sys.argv[3] if len(sys.argv) > 3 else "output_dual.mp4"
GAMEDATA_REPO = Path("wows-gamedata")

# Resolve gamedata version from replay A. Both replays must belong to the
# same match (merge_replays validates arenaUniqueId) which implies the same
# client build, so either replay resolves to the same versioned cache.
try:
    vgd = resolve_for_replay(REPLAY_A, GAMEDATA_REPO)
except RuntimeError:
    vgd = VersionedGamedata.from_gamedata_path(GAMEDATA_REPO / "data")

replay_a = parse_replay(REPLAY_A, str(vgd.entity_defs_path))
replay_b = parse_replay(REPLAY_B, str(vgd.entity_defs_path))
merged = merge_replays(replay_a, replay_b)
print(f"Merged: {merged.map_name}, {merged.duration:.0f}s, {len(merged.players)} players")

config = RenderConfig(
    gamedata_path=vgd.version_dir / "data",
    versioned_gamedata=vgd,
    speed=20.0, fps=20,
    minimap_size=1080, panel_width=420,
)
renderer = DualMinimapRenderer(config, replay=merged)

# Dual layer set — no player_header, damage_stats, ribbons, right_panel.
# Killfeed is kept (server-authoritative, works in neutral observer mode).
# Trails is added here (absent from render_quick.py) because dual mode is a neutral
# observer view where movement history helps track both teams.
for L in [
    MapBackgroundLayer(),
    TeamRosterLayer(),
    CapturePointLayer(),
    WeatherLayer(),
    SmokeLayer(),
    ProjectileLayer(),
    AircraftLayer(),
    TrailLayer(),
    ShipLayer(),
    HealthBarLayer(),
    ConsumableLayer(),
    KillfeedLayer(),
    HudLayer(),
]:
    renderer.add_layer(L)

t0 = time.time()
renderer.render(merged, Path(OUTPUT),
    progress_callback=lambda c, t: print(f"  {c}/{t}") if c % 200 == 0 or c == t else None)
sz = Path(OUTPUT).stat().st_size
print(f"Done: {time.time()-t0:.1f}s, {sz/1024/1024:.2f} MB -> {OUTPUT}")
