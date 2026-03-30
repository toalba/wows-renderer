# wows-minimap-renderer

Minimap replay renderer + Discord bot for World of Warships. Parses `.wowsreplay` files and produces mp4 videos showing ship movements, shells, torpedoes, capture points, health bars, smoke screens, consumables, and team scores. Built for the Wargaming community bounty (KOTS referee tooling).

**Deadline: April 20, 2026**

## Architecture

```
wows-minimap-renderer/
├── renderer/                  # Core rendering engine
│   ├── core.py                # MinimapRenderer — frame loop, layer compositing via shared cairo context
│   ├── game_state.py          # GameStateAdapter — bridges parser state_at() to render state
│   ├── config.py              # RenderConfig — resolution, fps, speed, team colors
│   ├── layers/                # Layer-based rendering (composited per frame, bottom to top)
│   │   ├── base.py            # Layer ABC + RenderContext (ship_db, ship_icons, player_lookup, world_to_pixel)
│   │   ├── map_bg.py          # Minimap background image
│   │   ├── capture_points.py  # Cap circles + progress + team color
│   │   ├── trails.py          # Ship movement trails (fading lines)
│   │   ├── ships.py           # Ship class icons (from game PNGs) + player names, team-colored
│   │   ├── projectiles.py     # Shell traces + torpedo dots (ammo-type colored)
│   │   ├── hud.py             # Score bar, timer, ship counts
│   │   ├── health_bars.py     # Per-ship HP bars (+ repair party recoverable)
│   │   ├── consumables.py     # Consumable icons near ships + radar/hydro detection radius circles
│   │   ├── smoke.py           # Smoke screen radius visualization
│   │   ├── team_roster.py     # Ship names + class per team (side panels) — NOT YET IMPLEMENTED
│   │   └── ribbons.py         # Ribbon popups near ships (P2) — NOT YET IMPLEMENTED
│   ├── video.py               # FFmpegPipe — raw BGRA frames from cairo surface → ffmpeg stdin → h264 mp4
│   └── assets.py              # Asset loading (minimaps, ship icons, consumable icons, projectiles, ships.json, map_sizes)
├── scripts/
│   └── decode_gameparams.py   # Decode GameParams.data → ships.json (and optionally full dump/split)
├── bot/                       # Discord bot — NOT YET IMPLEMENTED
│   └── __init__.py
├── render_quick.py            # Quick render: all layers, 20x speed, 1080px → output.mp4
├── test_render.py             # Older test render script (fewer layers)
├── pyproject.toml
└── CLAUDE.md
```

## Current Status (2026-03-30)

### Working
- All 8 rendering layers: map background, capture points, smoke, projectiles, ships, health bars, consumables, HUD
- Ship positions (all players including self) with team colors (green=ally, red=enemy, white=self)
- Ship class icons from game PNG assets (28x28 RGBA, ally/enemy/white/sunk variants)
- Player names above ships (bold, team-colored with dark halo)
- Shell traces colored by ammo type (AP/HE/SAP) + torpedo tracks
- Smoke screen radius circles
- Consumable icons near ships + radar/hydro detection radius circles
- Dark navy blue background (#0D1426)
- Self player position tracking via PLAYER_ORIENTATION (0x2C) packets
- Self-team detection and perspective swap (Trap 5)
- Performance: iter_states() for O(delta) incremental state queries, ffmpeg ultrafast preset

### Fixed: Player-to-Entity Metadata Matching
Player-to-entity matching now uses `onArenaStateReceived` pickle data for authoritative ID-based matching.
See parser CLAUDE.md "Roster: Vehicle-to-Player Matching" section for details.

### Not Yet Implemented
- `team_roster.py` — Ship names + class per team (side panels)
- `ribbons.py` — Ribbon popups near ships (P2)
- Discord bot (`bot/`)
- CLI (`renderer/cli.py` — entry point declared in pyproject.toml but not implemented)

## Dependencies

```toml
[project]
dependencies = [
    "wows-replay-parser",        # Local/git dependency — the replay parser
    "pycairo>=1.26",             # Cairo vector graphics (2D rendering)
    "discord.py>=2.3",           # Discord bot (future)
    "click>=8.0",                # CLI (future)
    "rich>=13.0",                # CLI output (future)
]
```

The replay parser (`wows-replay-parser`) is a separate package linked as a local editable dependency:
```toml
[tool.uv.sources]
wows-replay-parser = { path = "../wows-replay-parser", editable = true }
```

**External runtime dependencies:**
- **FFmpeg** must be on PATH (used via subprocess pipe, not a Python package)
- **Cairo** system library (pycairo is a binding, needs libcairo installed on Linux/macOS; Windows wheels include it)

## Data Dependencies

The renderer needs game assets from the wows-gamedata repo:
```bash
git clone https://github.com/toalba/wows-gamedata.git
```

| Asset | Path in gamedata repo | Used for |
|---|---|---|
| Minimap images | `data/spaces/<map>/minimap.png` | Map background layer |
| Ship class icons | `data/gui/battle_hud/markers/ship/icon_*.png` | 28x28 RGBA ship class icons (ally/enemy/white/sunk × 6 classes) |
| Consumable icons | `data/gui/consumables/consumable_*.png` | Consumable layer icons near ships |
| Entity defs | `data/scripts_entity/entity_defs/` | Passed through to parser |
| ships.json | `data/ships.json` | Compact shipId→{name,species,nation,level} lookup |
| projectiles.json | `data/projectiles.json` | Projectile params_id→{ammo_type,caliber} lookup (generated by decode_gameparams.py) |
| map_sizes.json | `data/map_sizes.json` | space_size per map for coordinate transform |
| ship_consumables.json | `data/ship_consumables.json` | Per-ship consumable loadouts + detection ranges |
| GameParams split | `data/split/Ship/*.json`, `data/split/Ability/*.json` | Fallback for ship_consumables.json generation |

### GameParams Decoding

`GameParams.data` is a binary blob containing all ship/consumable/projectile/etc data. Decode format:
```
reverse all bytes → zlib decompress → pickle load (with custom unpickler)
```

Script: `scripts/decode_gameparams.py`
```bash
# Extract just ships.json (fast):
python scripts/decode_gameparams.py --ships-only ../wows-gamedata/data/content/GameParams.data --output-dir ../wows-gamedata/data/

# Full dump + split for research:
python scripts/decode_gameparams.py --full --split ../wows-gamedata/data/content/GameParams.data --output-dir .
```

### Map Size Lookup

Map sizes are loaded from `map_sizes.json` by `assets.get_map_size()`. Each entry maps a map name to its `space_size` (world coordinate bounds). Common sizes: 24000, 30000, 36000, 42000, 48000.

## Rendering Pipeline

```
ParsedReplay (from wows-replay-parser)
    │
    │  replay = parse_replay("battle.wowsreplay", gamedata_path)
    │
    ▼
GameStateAdapter
    │  Wraps replay for the renderer
    │  Resolves map_size, player_lookup (entity_id → PlayerInfo)
    │
    ▼
MinimapRenderer
    │  Creates a single reusable cairo.ImageSurface (ARGB32)
    │  Opens FFmpegPipe (subprocess, raw BGRA frames → ffmpeg stdin)
    │
    │  For each frame timestamp (iter_states for O(delta) queries):
    │    1. Clear surface (dark navy blue)
    │    2. For each layer: cr.save() → layer.render(cr, state, t) → cr.restore()
    │    3. surface.flush() → pipe.write_frame(surface.get_data())
    │
    ▼
FFmpegPipe
    │  Raw BGRA buffer → ffmpeg stdin → h264 mp4
    │  No temp frames on disk
    │
    ▼
output.mp4
```

## Layer System

Each visual element is a separate Layer. All layers draw onto a **shared cairo.Context** — no separate images, no alpha compositing step. Layers are composited in add order (first = bottom).

```python
renderer = MinimapRenderer(config)
renderer.add_layer(MapBackgroundLayer())       # Bottom
renderer.add_layer(CapturePointLayer())
renderer.add_layer(SmokeLayer())
renderer.add_layer(ProjectileLayer())          # Shell traces + torpedoes
renderer.add_layer(ShipLayer())
renderer.add_layer(HealthBarLayer())
renderer.add_layer(ConsumableLayer())
renderer.add_layer(HudLayer())                 # Top: scores, timer
# P2 (not yet implemented):
# renderer.add_layer(TeamRosterLayer())
# renderer.add_layer(RibbonLayer())
```

Adding a new visual element = adding a new Layer class. No changes to existing code.

### Layer Interface

```python
class Layer(ABC):
    def initialize(self, ctx: RenderContext) -> None:
        """Called once before rendering. Preload assets, cache data.
        Default stores ctx as self.ctx."""
        self.ctx = ctx

    @abstractmethod
    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        """Draw this layer onto the shared cairo context.
        cr: shared cairo context. state: GameState snapshot. timestamp: game seconds."""
        ...
```

### RenderContext

Shared context passed to all layers via `initialize()`:

| Field | Type | Description |
|---|---|---|
| `config` | `RenderConfig` | Layout, video, rendering settings |
| `replay` | `ParsedReplay` | The parsed replay |
| `map_size` | `float` | space_size from map_sizes.json |
| `player_lookup` | `dict[int, PlayerInfo]` | entity_id → player info |
| `ship_db` | `dict[int, dict]` | ship_id → {name, species, nation, level} |
| `ship_icons` | `dict[str, dict]` | species → {ally/enemy/white/sunk: cairo.ImageSurface} |
| `first_seen` | `dict[int, float]` | entity_id → first position timestamp |
| `scale` | `float` (property) | Scale factor relative to 760px reference |

Key methods:
- `world_to_pixel(x, z)` → `(px, py)` — world coords to canvas pixel coords
- `is_visible(entity_id, timestamp)` — whether entity has been seen yet
- `raw_to_display_team(raw_team_id)` → `0` (ally) or `1` (enemy) — handles perspective swap
- `draw_text_halo(cr, x, y, text, r, g, b, ...)` — static utility for readable text

## WG Bounty Requirements Mapping

### Core (required)

| Requirement | Layer/Component | Status |
|---|---|---|
| Parse replay → video | `core.py` + `video.py` | DONE |
| Both teams + ship names + HP bars | `ships.py` + `health_bars.py` | DONE |
| Shells and torpedoes | `projectiles.py` | DONE |
| Discord bot + user interaction | `bot/` | NOT STARTED |
| Capture points + status + progress | `capture_points.py` | DONE |
| Total team points | `hud.py` | DONE |
| Maintained for 1 year | Automated gamedata pipeline | DONE (git pull) |
| Apache 2.0, WG copyright | `LICENSE` | TODO |

### Nice-to-have (P2)

| Feature | Layer/Component | Status |
|---|---|---|
| Ribbons | `ribbons.py` | NOT STARTED |
| Repair Party recoverable HP | `health_bars.py` (lighter segment) | NOT STARTED |
| Dual perspective combined | `merge.py` in parser | NOT STARTED |
| Team roster side panels | `team_roster.py` | NOT STARTED |

## Discord Bot

### Commands (planned)

```
/render <replay_file>
    Optional: speed (default: 10x), start, end, perspective
    → Renders and uploads mp4

/render <replay_file_1> <replay_file_2>
    → Dual perspective merge (P2)
```

### Constraints

- Discord file limit: 8MB (no Nitro), 25MB (Nitro), 100MB (boosted server)
- For KOTS use, the bot will run on WG's VPS — expect boosted limits
- Target: 60s timelapse at 720p should be well under 25MB with h264

## Video Output

### Default: Timelapse

A 20-minute match at 20x speed = 1 minute of video at 20fps = 1200 frames. At 1080px this is fast to render and well within Discord limits.

### Configurable Parameters (RenderConfig)

| Parameter | Default | Description |
|---|---|---|
| `minimap_size` | 760 | Minimap resolution (square, pixels) |
| `panel_width` | 220 | Side panel width (total width = panel + minimap + panel) |
| `speed` | 10x | Playback speed multiplier |
| `fps` | 20 | Output video frames per second |
| `start_time` | 0 | Start time in seconds |
| `end_time` | None | End time (None = match end) |
| `codec` | libx264 | FFmpeg codec |
| `crf` | 23 | Quality (lower = better, 18-28 typical) |
| `trail_length` | 30.0 | Ship trail duration in seconds |
| `team_colors` | green/red | RGBA tuples per display team (0=ally, 1=enemy) |
| `self_color` | white | RGBA for the recording player's ship |

### Coordinate Mapping

```python
# In RenderContext.world_to_pixel():
scaling = minimap_size / space_size
px = world_x * scaling + half_minimap + panel_width
py = -world_z * scaling + half_minimap  # Z axis inverted
```

World: origin at center, X=east, Z=north, range ±(space_size/2).
Pixel: origin at top-left, X=right, Y=down.

## Development

```bash
# Setup
cd wows-renderer
uv venv && source .venv/bin/activate
uv pip install -e "."

# Quick render (all layers, 20x, 1080px)
python render_quick.py battle.wowsreplay output.mp4

# Tests
pytest

# Lint
ruff check .
mypy .
```

## Remaining Work

### Before Submission (April 20)
1. Discord bot (`bot/`) — slash commands, file upload, render worker
2. Team roster side panels (`team_roster.py`)
3. CLI implementation (`renderer/cli.py`)
4. LICENSE file (Apache 2.0, WG copyright)
5. Visual polish + edge case handling
6. Version-awareness for projectiles.json and ship_consumables.json (cross-patch replay support)

### Nice-to-have (P2)
6. Ribbon layer
7. Repair Party recoverable HP visualization
8. Dual perspective merge

## License

Apache 2.0 — Copyright Wargaming.net
Developed on Wargaming's request for the community.
