# wows-minimap-renderer

A Cairo-based minimap replay renderer for World of Warships. Parses `.wowsreplay` files and produces MP4 timelapse videos showing ship movements, shell traces, torpedoes, capture points, health bars, and team scores.

Built for the Wargaming community bounty (KOTS referee tooling).

---

## Features

- **16 composable rendering layers** — map background, team rosters, capture points, smoke, projectiles, aircraft, ships, health bars, consumables, player header, damage stats, ribbons, killfeed, right panel composite, HUD, trails
- Ship positions with rotated class icons (destroyer, cruiser, battleship, carrier, submarine)
- Team-colored ships (green = ally, red = enemy, white = self) with player names
- Shell traces colored by ammo type (AP/HE/SAP) + torpedo tracks
- Capture zone circles with progress bars and team ownership
- Per-ship health bars with repair party recoverable HP
- Smoke screen, consumable radius indicators (radar/hydro circles)
- Aircraft layer (CV squadrons + airstrikes)
- HUD overlay with team scores, timer, TTW pills, 1-kill-swing indicator, match result
- Team roster side panels with kills, damage, HP, consumable timers
- Self-player damage breakdown by weapon type (AP/HE/SAP/torp/fire/flood/secondary)
- Self-player header with ship silhouette HP bar
- Kill feed + ribbon counters
- Configurable speed, resolution, FPS, time range, and quality (with input validation)
- Direct FFmpeg pipe with async frame writer (~17ms/frame at 1080p)
- **Discord bot** — `/render` slash command with progress reporting
- **Docker support** — multi-stage build with all dependencies

---

## Prerequisites

| Dependency | Version | Purpose |
|---|---|---|
| Python | >= 3.12 | Runtime |
| FFmpeg | any recent | Video encoding (must be on PATH) |
| Cairo | system lib | 2D vector graphics (pycairo needs it) |
| Git | any | Cloning gamedata |

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
git submodule update --init
```

### 2. Install dependencies

```bash
# Using uv (recommended)
uv venv
source .venv/bin/activate    # Linux/macOS
# or: .venv\Scripts\activate  # Windows

uv pip install -e "."
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

# Quick test render
python render_quick.py path/to/battle.wowsreplay output.mp4
```

---

## Usage

### Quick render (script)

The simplest way to render a replay:

```bash
python render_quick.py path/to/battle.wowsreplay output.mp4
```

This renders at 20x speed, 1080px resolution, 20 FPS with all layers enabled.

### Python API

```python
from pathlib import Path
from renderer.config import RenderConfig
from renderer.core import MinimapRenderer
from renderer.layers.map_bg import MapBackgroundLayer
from renderer.layers.ships import ShipLayer
from renderer.layers.trails import TrailLayer
from renderer.layers.projectiles import ProjectileLayer
from renderer.layers.capture_points import CapturePointLayer
from renderer.layers.health_bars import HealthBarLayer
from renderer.layers.consumables import ConsumableLayer
from renderer.layers.smoke import SmokeLayer
from renderer.layers.team_roster import TeamRosterLayer
from renderer.layers.right_panel import RightPanelLayer
from renderer.layers.hud import HudLayer
from wows_replay_parser import parse_replay

# Parse the replay
replay = parse_replay(
    "battle.wowsreplay",
    "wows-gamedata/data/scripts_entity/entity_defs"
)

# Configure the renderer
config = RenderConfig(
    gamedata_path=Path("wows-gamedata/data"),
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
    SmokeLayer(),
    TrailLayer(),
    ProjectileLayer(),
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

Total output resolution = `left_panel + minimap_size + right_panel` x `minimap_size + hud_height`.

---

## Rendering Layers

Layers are composited bottom-to-top. Each layer is independent and optional.

| Layer | Description |
|---|---|
| `MapBackgroundLayer` | Water texture + minimap PNG + grid (pre-rendered static cache) |
| `TeamRosterLayer` | Left panel: both teams with names, kills, damage, HP bars, consumable timers |
| `CapturePointLayer` | Cap circles with progress arcs, team colors, contested indicators |
| `SmokeLayer` | Smoke screen radius visualization |
| `ProjectileLayer` | Shell traces (AP/HE/SAP colored) + torpedo tracks |
| `AircraftLayer` | CV squadrons + airstrike icons |
| `ShipLayer` | Rotated ship class icons, player names, team colors, spotted glow |
| `TrailLayer` | Fading ship movement trails |
| `HealthBarLayer` | Per-ship HP bars + repair party recoverable HP |
| `ConsumableLayer` | Consumable icons + radar/hydro detection radius circles |
| `RightPanelLayer` | Composite: player header + damage stats + ribbons + killfeed |
| `HudLayer` | Score bar, timer, TTW pills, 1-kill-swing indicator, match result |

The `RightPanelLayer` is a composite of four sub-layers:
- **PlayerHeaderLayer** — Self-player ship silhouette with HP bar + clan tag + name
- **DamageStatsLayer** — Damage dealt/spotting/potential breakdown by weapon type
- **RibbonLayer** — Recording player ribbon counters (grouped, accumulating)
- **KillfeedLayer** — Recent kills with frag icons, bottom-anchored

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
| `GAMEDATA_PATH` | `wows-gamedata/data` | Path to game assets |
| `MAX_WORKERS` | `2` | Concurrent render processes |
| `RENDER_TIMEOUT` | `120` | Max seconds per render |
| `COOLDOWN_SECONDS` | `60` | Per-user rate limit |
| `MAX_UPLOAD_MB` | `50` | Max replay file size |

The bot renders replays in a `ProcessPoolExecutor` (separate processes for CPU-bound cairo work), reports progress to Discord in real-time, and includes match duration + render time in the response.

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

# Build and run with docker compose
docker compose build
docker compose up -d

# View logs
docker compose logs -f
```

Or build manually:

```bash
eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_rsa
DOCKER_BUILDKIT=1 docker build --ssh default -t wows-renderer .
docker run --env-file .env -v ./wows-gamedata:/app/wows-gamedata:ro wows-renderer
```

The image is a multi-stage build: builder stage installs dependencies with SSH forwarding, runtime stage is `python:3.12-slim` with `ffmpeg` and `libcairo2`. The `wows-gamedata` submodule is mounted as a volume so you can update game assets without rebuilding.

---

## Architecture

```
.wowsreplay file
    |
    v
parse_replay()                    # wows-replay-parser
    |  Decrypt -> decompress -> decode packets -> build events + state tracker
    v
ParsedReplay
    |  .iter_states(timestamps)   # O(delta) incremental state queries
    |  .events                    # Typed events (shots, damage, deaths, ribbons, etc.)
    v
MinimapRenderer
    |  For each frame:
    |    state = next(state_iter)
    |    for layer in layers:
    |      layer.render(cairo_context, state, t)
    |    pipe frame to FFmpeg (async background thread)
    v
output.mp4                        # h264, Discord/YouTube compatible
```

---

## Updating for new game patches

When a new WoWs patch releases, just update the gamedata submodule:

```bash
git submodule update --remote wows-gamedata
```

No code changes needed — the parser dynamically loads entity definitions from the `.def` files.

---

## License

Apache 2.0 — Copyright Wargaming.net
