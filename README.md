# wows-minimap-renderer

A Cairo-based minimap replay renderer for World of Warships. Parses `.wowsreplay` files and produces MP4 timelapse videos showing ship movements, shell traces, torpedoes, capture points, health bars, consumables with charge tracking, chat, and team scores.

Built for the Wargaming community.

---

## Features

- **16 composable rendering layers** — map background, team rosters, capture points, smoke, weather, projectiles, aircraft, ships, health bars, consumables, player header, damage stats, ribbons, killfeed + chat, HUD, trails
- Ship positions with rotated class SVG icons (destroyer, cruiser, battleship, carrier, submarine, auxiliary)
- Team-colored ships (green = ally, red = enemy, white = self) with player names
- **Division mate highlighting** — gold yellow ship icons on minimap + team roster
- **Clan battle support** — clan tags displayed below score bar in each clan's color
- Shell traces colored by ammo type (AP = white, HE = orange, SAP = pink) with caliber-scaled line widths + torpedo tracks
- Capture zone circles with progress bars, team ownership, and Arms Race buff zones
- Per-ship health bars with repair party recoverable HP segment
- Smoke screen radius visualization with per-puff FIFO lifecycle
- Consumable icons + radar/hydro/hydrophone detection radius circles
- Aircraft layer with type-specific icons (fighters, bombers, torpedo planes, scouts, depth charges, skip bombers) resolved from GameParams
- Weather zone overlay
- HUD overlay with team scores, timer, TTW pills, 1-kill-swing indicator, match result, clan battle tags
- **Team roster** with kills, damage, HP bars, consumable icons with charge counts (white = ready, green = active, gray = cooldown), time-based consumable capacity tracking
- Self-player damage breakdown by weapon type (AP/HE/SAP/torp/fire/flood/secondary)
- Self-player header with ship silhouette HP bar
- **Kill feed + chat messages** — interleaved chronologically, team chat prefixed with [T]
- Ribbon counters (grouped, accumulating)
- **Per-version gamedata cache** — automatic version detection from replay, isolated cache per game version, concurrent-worker safe (no git checkout at render time)
- **GameParams.data pickle caching** — decoded once, cached with blake2b hash key
- Configurable speed, resolution, FPS, time range, and quality (with input validation)
- Direct FFmpeg pipe with async frame writer (~17ms/frame at 1080p)
- **Discord bot** — `/render` slash command with progress reporting, game type display, per-phase timing breakdown
- **Docker support** — multi-stage build with persistent gamedata cache volume

---

## Prerequisites

| Dependency | Version | Purpose |
|---|---|---|
| Python | >= 3.12 | Runtime |
| FFmpeg | any recent | Video encoding (must be on PATH) |
| Cairo | system lib | 2D vector graphics (pycairo needs it) |
| Git | any | Gamedata version cache (git archive extraction) |

### Installing Cairo

**Windows (easiest):** pycairo ships prebuilt wheels — `pip install pycairo` just works.

**Ubuntu/Debian:**
```bash
sudo apt-get install libcairo2-dev pkg-config python3-dev
```

**macOS:**
```bash
brew install cairo pkg-config
```

### Installing FFmpeg

**Windows:** Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH, or:
```powershell
winget install FFmpeg
```

**Ubuntu/Debian:**
```bash
sudo apt-get install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

---

## Setup

### 1. Clone with submodules

The renderer needs game assets (minimaps, ship icons, entity definitions) from the gamedata submodule:

```bash
git clone --recurse-submodules https://github.com/toalba/wows-renderer.git
cd wows-renderer
```

Or if already cloned:
```bash
git submodule update --init --recursive
```

### 2. Install dependencies

```bash
# Using uv (recommended)
uv venv
source .venv/bin/activate    # Linux/macOS
# or: .venv\Scripts\activate  # Windows
uv sync
```

Or with plain pip:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e "."
```

### 3. Verify the setup

```bash
# Check FFmpeg is available
ffmpeg -version

# Quick test render (auto-detects game version, caches gamedata on first run)
python render_quick.py path/to/battle.wowsreplay output.mp4
```

---

## Usage

### Quick render (script)

The simplest way to render a replay:

```bash
python render_quick.py path/to/battle.wowsreplay output.mp4
```

This renders at 20x speed, 1080px resolution, 20 FPS with all layers enabled. On first run for a new game version, it builds a gamedata cache (~10s), subsequent renders are instant.

### Python API

```python
from pathlib import Path
from renderer.config import RenderConfig
from renderer.core import MinimapRenderer
from renderer.gamedata_cache import resolve_for_replay
from renderer.layers.map_bg import MapBackgroundLayer
from renderer.layers.ships import ShipLayer
from renderer.layers.trails import TrailLayer
from renderer.layers.projectiles import ProjectileLayer
from renderer.layers.capture_points import CapturePointLayer
from renderer.layers.health_bars import HealthBarLayer
from renderer.layers.consumables import ConsumableLayer
from renderer.layers.smoke import SmokeLayer
from renderer.layers.weather import WeatherLayer
from renderer.layers.aircraft import AircraftLayer
from renderer.layers.team_roster import TeamRosterLayer
from renderer.layers.right_panel import RightPanelLayer
from renderer.layers.hud import HudLayer
from wows_replay_parser import parse_replay

# Resolve gamedata version for this replay (builds cache if needed)
vgd = resolve_for_replay("battle.wowsreplay", Path("wows-gamedata"))

# Parse the replay with version-correct entity definitions
replay = parse_replay("battle.wowsreplay", str(vgd.entity_defs_path))

# Configure the renderer
config = RenderConfig(
    gamedata_path=vgd.version_dir / "data",
    versioned_gamedata=vgd,
    speed=20.0,           # 20x playback speed
    fps=20,               # 20 frames per second
    minimap_size=1080,    # 1080px square minimap
    panel_width=420,      # Side panel width
    crf=23,               # Video quality (lower = better, 0-51)
)

# Build the renderer with layers (order = draw order, first = bottom)
renderer = MinimapRenderer(config)
for layer in [
    MapBackgroundLayer(),
    TeamRosterLayer(),
    CapturePointLayer(),
    WeatherLayer(),
    SmokeLayer(),
    TrailLayer(),
    ProjectileLayer(),
    AircraftLayer(),
    ShipLayer(),
    HealthBarLayer(),
    ConsumableLayer(),
    RightPanelLayer(),
    HudLayer(),
]:
    renderer.add_layer(layer)

# Render to MP4
renderer.render(replay, Path("output.mp4"))
```

### Profiling

Measure per-layer timing breakdown:

```bash
python profile_frames.py path/to/battle.wowsreplay /tmp/profile.mp4
```

Outputs a table showing total time, per-frame ms, and percentage for each layer + encode phase.

---

## Configuration

All rendering parameters are controlled via `RenderConfig`. Invalid values raise `ValueError` at construction time.

| Parameter | Default | Description |
|---|---|---|
| `minimap_size` | 760 | Minimap resolution in pixels (square) |
| `panel_width` | 220 | Side panel width in pixels |
| `left_panel_width` | None | Override left panel width (None = use panel_width) |
| `right_panel_width` | None | Override right panel width (None = use panel_width) |
| `fps` | 20 | Output video frame rate |
| `speed` | 10.0 | Playback speed multiplier (10x = 20min match in 2min) |
| `start_time` | 0.0 | Start rendering at this timestamp (0 = auto-detect battle start) |
| `end_time` | None | Stop rendering at this timestamp (None = end of match) |
| `codec` | libx264 | FFmpeg video codec |
| `crf` | 23 | Constant rate factor / quality (0-51, 18-28 typical) |
| `trail_length` | 30.0 | Ship movement trail duration in seconds |
| `team_colors` | green/red | RGBA tuples per team ID |
| `self_color` | white | RGBA tuple for the recording player's ship |
| `division_color` | gold yellow | RGBA tuple for division mate highlighting |
| `versioned_gamedata` | None | VersionedGamedata for version-specific data (set by resolve_for_replay) |

Total output resolution = `left_panel + minimap_size + right_panel` x `minimap_size + hud_height`.

---

## Rendering Layers

Layers are composited bottom-to-top. Each layer is independent and optional.

| Layer | Description |
|---|---|
| `MapBackgroundLayer` | Water texture + minimap PNG + grid (pre-rendered static cache) |
| `TeamRosterLayer` | Left panel: both teams with names, kills, damage, HP bars, consumable charge tracking |
| `CapturePointLayer` | Cap circles with progress arcs, team colors, contested indicators, Arms Race buff zones |
| `WeatherLayer` | Weather zone radius circles |
| `SmokeLayer` | Smoke screen radius visualization with per-puff FIFO lifecycle |
| `ProjectileLayer` | Shell traces (AP/HE/SAP colored, caliber-scaled) + torpedo tracks |
| `AircraftLayer` | CV squadrons + airstrikes + consumable planes with type-specific icons |
| `ShipLayer` | Rotated ship class SVG icons, player names, team colors, spotted glow, division mate gold icons |
| `TrailLayer` | Fading ship movement trails |
| `HealthBarLayer` | Per-ship HP bars + repair party recoverable HP |
| `ConsumableLayer` | Consumable icons + radar/hydro/hydrophone detection radius circles |
| `RightPanelLayer` | Composite: player header + damage stats + ribbons + killfeed/chat |
| `HudLayer` | Score bar, timer, TTW pills, 1-kill-swing indicator, match result, clan battle tags |

The `RightPanelLayer` is a composite of four sub-layers:
- **PlayerHeaderLayer** — Self-player ship silhouette with HP bar + clan tag + name
- **DamageStatsLayer** — Damage dealt/spotting/potential breakdown by weapon type
- **RibbonLayer** — Recording player ribbon counters (grouped, accumulating)
- **KillfeedLayer** — Recent kills with frag icons + chat messages (interleaved chronologically)

### Adding a custom layer

```python
from renderer.layers.base import Layer, RenderContext

class MyLayer(Layer):
    def initialize(self, ctx: RenderContext) -> None:
        """Called once before rendering. Preload assets, cache data."""
        super().initialize(ctx)

    def render(self, cr, state, timestamp: float) -> None:
        """Draw onto the Cairo context for this frame."""
        # cr = cairo.Context
        # state = GameState (ships, battle, capture_points)
        # Use self.ctx.world_to_pixel(x, z) for coordinate mapping
        ...
```

---

## Gamedata Version Cache

The renderer automatically detects the game version from each replay and uses version-specific gamedata. This ensures replays from different game patches render correctly.

- **Cache location:** `~/.cache/wows-gamedata/v{build_id}/`
- **Population:** Automatic on first render of a new version (extracts via `git archive`, decodes GameParams.data, writes pickle cache)
- **Warm path:** Single `pickle.load()` — near-instant
- **Concurrent-safe:** Multiple workers can render different version replays simultaneously (no git checkout, no locks)
- **Bot startup:** Pre-populates caches for all known version tags in the background

Override the cache directory with the `GAMEDATA_CACHE_DIR` environment variable.

---

## Discord Bot

The bot provides a `/render` slash command that accepts a `.wowsreplay` file and returns the rendered minimap video.

### Setup

1. Create a `.env` file in the project root:
   ```
   DISCORD_TOKEN=your_bot_token_here
   ```

2. Run the bot:
   ```bash
   python -m bot.main
   # or: wows-bot
   ```

### Configuration

All config is via environment variables (or `.env` file):

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | (required) | Bot token |
| `GAMEDATA_PATH` | `wows-gamedata/data` | Path to game assets (fallback) |
| `GAMEDATA_REPO_PATH` | `wows-gamedata` | Path to wows-gamedata git repo |
| `GAMEDATA_CACHE_DIR` | `~/.cache/wows-gamedata` | Override cache directory |
| `MAX_WORKERS` | `2` | Concurrent render processes |
| `RENDER_TIMEOUT` | `120` | Max seconds per render |
| `COOLDOWN_SECONDS` | `60` | Per-user rate limit |
| `MAX_UPLOAD_MB` | `50` | Max replay file size |

The bot renders replays in a `ProcessPoolExecutor` (separate processes for CPU-bound cairo work), reports progress to Discord in real-time, and includes game type, match duration, render time, and file size in the response. Detailed per-phase timing (resolve/parse/setup/render/encode/upload + per-layer init) is logged for performance monitoring.

### Presets

The `/render` command supports three presets:
- **Full** (default) — all layers + both panels
- **Map** — minimap only, no side panels
- **Player data** — minimap + killfeed/ribbons

---

## Docker

```bash
# Make sure submodules are initialized
git submodule update --init --recursive

# Create .env with at minimum:
#   DISCORD_TOKEN=your_bot_token_here

# Build and run with docker compose (SSH agent needed for parser dependency)
eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519
DOCKER_BUILDKIT=1 docker compose build --ssh default
docker compose up -d

# View logs
docker compose logs -f
```

The docker-compose setup includes:
- `wows-gamedata` mounted read-only for git archive extraction
- `.git/modules/wows-gamedata` mounted for tag access in the container
- `gamedata-cache` named volume for persistent version caches across restarts

The image is a multi-stage build: builder stage installs dependencies with SSH forwarding, runtime stage is `python:3.12-slim` with `ffmpeg`, `libcairo2`, and `git`.

---

## Architecture

```
.wowsreplay file
    |
    |  resolve_for_replay()           # gamedata_cache.py — version detection + cache
    |  → extracts build ID from replay header
    |  → populates ~/.cache/wows-gamedata/v{build_id}/ if needed
    |    (git archive + GameParams.data decode + pickle cache)
    v
parse_replay()                        # wows-replay-parser
    |  Decrypt → decompress → decode packets → build events + state tracker
    v
ParsedReplay
    |  .iter_states(timestamps)       # O(delta) incremental state queries
    |  .events                        # Typed events (shots, damage, deaths, chat, etc.)
    v
MinimapRenderer
    |  For each frame:
    |    state = next(state_iter)
    |    for layer in layers:
    |      layer.render(cairo_context, state, t)
    |    pipe frame to FFmpeg (async background thread)
    v
output.mp4                            # h264, Discord/YouTube compatible
```

---

## Updating for new game patches

When a new WoWs patch releases, update the gamedata submodule:

```bash
cd wows-gamedata
git fetch --tags
cd ..
git submodule update --remote wows-gamedata
```

The renderer automatically detects the replay's game version and builds a version-specific cache on first render. No code changes needed — entity definitions are loaded dynamically from `.def` files, and GameParams data is decoded from `GameParams.data`.

---

## Related projects

- [`landaire/wows-toolkit`](https://github.com/landaire/wows-toolkit) — another community World of Warships replay tooling project (Rust).

---

## License

Apache 2.0 — Copyright Wargaming.net
