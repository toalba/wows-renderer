# wows-minimap-renderer

A Cairo-based minimap replay renderer for World of Warships. Parses `.wowsreplay` files and produces MP4 timelapse videos showing ship movements, shell traces, torpedoes, capture points, health bars, and team scores.

Built for the Wargaming community bounty (KOTS referee tooling).

---

## Features

- Layer-based rendering pipeline with 8 composable visual layers
- Ship positions with rotated class icons (destroyer, cruiser, battleship, carrier, submarine)
- Team-colored ships (green = ally, red = enemy, white = self) with player names
- Shell traces and torpedo tracks from shot events
- Capture zone circles with progress bars and team ownership
- Per-ship health bars
- Smoke screen visualization
- Consumable radius indicators
- HUD overlay with team scores, timer, and ship counts
- Configurable speed, resolution, FPS, time range, and quality
- Direct FFmpeg pipe (no temp frames on disk)

---

## Prerequisites

| Dependency | Version | Purpose |
|---|---|---|
| Python | >= 3.12 | Runtime |
| [uv](https://docs.astral.sh/uv/) | latest | Package manager (recommended) |
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

### 1. Clone the gamedata repository

The renderer needs game assets (minimaps, ship icons, entity definitions) from the gamedata repo. Clone it as a sibling directory:

```bash
cd /path/to/projects/wows
git clone https://github.com/toalba/wows-gamedata.git wows-gamedata
```

Expected directory layout:
```
wows/
├── wows-replay-parser/    # Parser library
├── wows-renderer/         # This project
└── wows-gamedata/         # Game assets (cloned above)
    └── data/
        ├── scripts_entity/entity_defs/   # Entity definitions (passed to parser)
        ├── spaces/<map_name>/minimap.png  # Minimap background images
        ├── gui/battle_hud/markers/ship/   # Ship class icons (28x28 PNGs)
        ├── ships.json                     # Ship ID → name/class/tier lookup
        └── map_sizes.json                 # World coordinate bounds per map
```

### 2. Install the parser and renderer

```bash
cd wows-renderer

# Using uv (recommended)
uv venv
source .venv/bin/activate    # Linux/macOS
# or: .venv\Scripts\activate  # Windows

uv pip install -e "."
# This installs the renderer AND the parser (linked from ../wows-replay-parser)
```

Or with plain pip:
```bash
cd wows-renderer
python -m venv .venv
source .venv/bin/activate
pip install -e "../wows-replay-parser[cli]"
pip install -e "."
```

### 3. Verify the setup

```bash
# Check parser works
wowsreplay info path/to/battle.wowsreplay

# Check FFmpeg is available
ffmpeg -version
```

---

## Usage

### Quick render (script)

The simplest way to render a replay:

```bash
cd wows-renderer
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
from renderer.layers.hud import HudLayer
from wows_replay_parser import parse_replay

# Parse the replay
replay = parse_replay(
    "battle.wowsreplay",
    "../wows-gamedata/data/scripts_entity/entity_defs"
)

# Configure the renderer
config = RenderConfig(
    gamedata_path=Path("../wows-gamedata/data"),
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
    CapturePointLayer(),
    SmokeLayer(),
    TrailLayer(),
    ProjectileLayer(),
    ShipLayer(),
    HealthBarLayer(),
    ConsumableLayer(),
    HudLayer(),
]:
    renderer.add_layer(layer)

# Render to MP4
renderer.render(replay, Path("output.mp4"))
```

---

## Configuration

All rendering parameters are controlled via `RenderConfig`:

| Parameter | Default | Description |
|---|---|---|
| `minimap_size` | 760 | Minimap resolution in pixels (square) |
| `panel_width` | 220 | Side panel width in pixels |
| `fps` | 20 | Output video frame rate |
| `speed` | 10.0 | Playback speed multiplier (10x = 20min match in 2min) |
| `start_time` | 0.0 | Start rendering at this timestamp (seconds) |
| `end_time` | None | Stop rendering at this timestamp (None = end of match) |
| `codec` | libx264 | FFmpeg video codec |
| `crf` | 23 | Constant rate factor / quality (18-28 typical range) |
| `trail_length` | 30.0 | Ship movement trail duration in seconds |
| `team_colors` | green/red | RGBA tuples per team ID |
| `self_color` | white | RGBA tuple for the recording player's ship |

Total output resolution = `panel_width + minimap_size + panel_width` x `minimap_size`.

---

## Rendering Layers

Layers are composited bottom-to-top. Each layer is independent and optional.

| Layer | Description |
|---|---|
| `MapBackgroundLayer` | Minimap PNG background image |
| `CapturePointLayer` | Capture zone circles + progress bars + team ownership |
| `SmokeLayer` | Smoke screen radius visualization |
| `ProjectileLayer` | Shell traces + torpedo tracks |
| `ShipLayer` | Rotated ship class icons, player names, team colors |
| `TrailLayer` | Fading ship movement trails |
| `HealthBarLayer` | Per-ship HP bars above ship icons |
| `ConsumableLayer` | Consumable effect radius indicators |
| `HudLayer` | Score bar, match timer, ship counts |

### Adding a custom layer

```python
from renderer.layers.base import Layer, RenderContext

class MyLayer(Layer):
    def initialize(self, ctx: RenderContext) -> None:
        """Called once before rendering. Preload assets, cache data."""
        ...

    def render(self, cr, state, timestamp: float) -> None:
        """Draw onto the Cairo context for this frame."""
        # cr = cairo.Context
        # state = GameState (ships, battle, capture_points)
        # Use ctx.world_to_pixel(x, z) for coordinate mapping
        ...
```

---

## Architecture

```
.wowsreplay file
    │
    ▼
parse_replay()                    # wows-replay-parser
    │  Decrypt → decompress → decode packets → build events + state tracker
    ▼
ParsedReplay
    │  .state_at(t) → GameState   # Ships, battle info, capture points
    │  .events                    # Typed events (shots, damage, deaths, etc.)
    ▼
MinimapRenderer
    │  For each frame at t += 1/(fps × speed):
    │    state = replay.state_at(t)
    │    for layer in layers:
    │      layer.render(cairo_context, state, t)
    │    pipe frame to FFmpeg
    ▼
output.mp4                        # h264, Discord/YouTube compatible
```

---

## Updating for new game patches

When a new WoWs patch releases, just update the gamedata:

```bash
cd wows-gamedata
git pull
```

No code changes needed — the parser dynamically loads entity definitions from the `.def` files.

---

## License

Apache 2.0 — Copyright Wargaming.net
