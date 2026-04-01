# wows-minimap-renderer

Minimap replay renderer + Discord bot for World of Warships. Parses `.wowsreplay` files and produces mp4 videos showing ship movements, shells, torpedoes, capture points, health bars, smoke screens, consumables, aircraft, ribbons, team rosters, and team scores. Built for the Wargaming community bounty (KOTS referee tooling).

**Deadline: April 20, 2026**

## Architecture

```
wows-minimap-renderer/
├── renderer/                  # Core rendering engine
│   ├── core.py                # MinimapRenderer — frame loop, layer compositing, async frame writer
│   ├── game_state.py          # GameStateAdapter — bridges parser state_at() to render state
│   ├── config.py              # RenderConfig — resolution, fps, speed, team colors
│   ├── layers/                # Layer-based rendering (composited per frame, bottom to top)
│   │   ├── base.py            # Layer ABC + RenderContext + text cache (draw_cached_text, draw_text_halo)
│   │   ├── map_bg.py          # Minimap background (pre-rendered static cache: water + minimap + grid)
│   │   ├── team_roster.py     # Left panel: team rosters (player/ship names, kills, damage, HP bar, consumable timers)
│   │   ├── capture_points.py  # Cap circles + progress + team color
│   │   ├── trails.py          # Ship movement trails (fading lines, pre-sampled)
│   │   ├── smoke.py           # Smoke screen radius visualization
│   │   ├── projectiles.py     # Shell traces + torpedo dots (ammo-type colored)
│   │   ├── aircraft.py        # CV squadrons + airstrike icons on minimap
│   │   ├── ships.py           # Ship class icons (rotated by yaw) + player names
│   │   ├── health_bars.py     # Per-ship HP bars + repair party recoverable HP + ship names
│   │   ├── consumables.py     # Consumable icons near ships + radar/hydro detection radius circles
│   │   ├── ribbons.py         # Right panel top: recording player ribbon counters (grouped, accumulating)
│   │   ├── killfeed.py        # Right panel bottom: kill feed (bottom-up)
│   │   └── hud.py             # Score bar, timer, TTW pills, 1-kill-swing indicator, match result
│   ├── video.py               # FFmpegPipe + FrameWriter (async background thread for pipe I/O)
│   └── assets.py              # Asset loading (minimaps, ship icons, consumable icons, ribbons, projectiles, ships.json, map_sizes, ship_consumables)
├── scripts/
│   └── decode_gameparams.py   # Decode GameParams.data → ships.json (and optionally full dump/split)
├── bot/                       # Discord bot (slash command /render)
│   ├── __init__.py
│   ├── main.py                # Bot entry point — creates Bot, loads cog, syncs commands
│   ├── config.py              # BotConfig — reads .env (DISCORD_TOKEN, GAMEDATA_PATH, MAX_WORKERS, etc.)
│   ├── cog_render.py          # RenderCog — /render slash command, async progress polling, file upload/download
│   └── worker.py              # render_replay() — picklable function for ProcessPoolExecutor
├── render_quick.py            # Quick render: all layers, 20x speed, 1080px → output.mp4
├── Dockerfile                 # Multi-stage build (builder + slim runtime with ffmpeg/cairo)
├── .dockerignore
├── pyproject.toml
└── CLAUDE.md
```

## Current Status (2026-03-31)

### Working — 13 Rendering Layers
1. **map_bg** — Water texture + minimap PNG + grid + labels (pre-rendered static cache, single paint per frame)
2. **team_roster** — Left panel with both teams: class icon, player name, ship name, kills (incremental), damage (incremental), HP bar, consumable icons with active/cooldown timers (base reload from GameParams as fallback for last use)
3. **capture_points** — Cap circles with progress arcs, team colors, contested indicators, A-H labels
4. **smoke** — Smoke screen radius circles from NESTED_PROPERTY puff positions
5. **projectiles** — Shell traces colored by ammo type (AP=white, HE=orange, SAP=pink) + torpedo dots; caliber-scaled line widths
6. **aircraft** — CV squadrons (controllable) + airstrikes on minimap with team-colored icons
7. **ships** — Ship class icons (28x28 RGBA, rotated by yaw) + player names (cached text surfaces)
8. **health_bars** — Per-ship HP bars (green/yellow/red) + repair party recoverable segment + ship names (cached)
9. **consumables** — Consumable icons near ships + radar/hydro detection radius circles (team-colored: blue=ally, red=enemy)
10. **ribbons** — Right panel top: recording player's ribbon counters in grouped layout (main + sub-ribbons), accumulating per frame, first-appearance order
11. **killfeed** — Right panel bottom: recent kills with frag icons, killer/victim names + ships, bottom-anchored growing upward
12. **hud** — Score bar with projected winner highlight, MM:SS timer, TTW pills (diamond icon, winner highlighting), "1 KILL DECIDES" indicator (team-colored glow), match result overlay
13. **trails** — Fading ship movement trails (pre-sampled at init, gap detection)

### Performance Optimizations
- **Async FrameWriter** — pipe I/O offloaded to background thread (video.py)
- **Static background cache** — map_bg renders once at init, single cr.paint() per frame
- **Text surface cache** — draw_cached_text() renders text to small surfaces once, blits via cr.paint(); used by ships, health_bars, killfeed, team_roster
- **Shadow-based text halo** — replaced expensive text_path+stroke with double show_text shadow
- **Baseline**: ~23ms/frame → **After**: ~12ms/frame (1.9x speedup)

### Other Features
- Ship positions (all players including self) with team colors (green=ally, red=enemy, white=self)
- Self player position tracking via PLAYER_ORIENTATION (0x2C) packets
- Self-team detection and perspective swap (Trap 5)
- Undetected enemies shown at 40% alpha (detection from visibility_flags)
- Dead ships shown with sunk icon variant
- ship_consumables.json loading: works with or without split/Ship directory
- Consumable cooldown: min(gap between uses, base reload from GameParams)
- iter_states() for O(delta) incremental state queries

### Known Issues
- Aircraft airstrike team_id not always correct (parser fix in progress)
- Ribbon derive_ribbons() has a bug (RIBBON_NAMES dict inverted) — using extract_recording_player_ribbons() instead
- Per-player damage type breakdown (AP/HE/fire/flood) needs parser to expose receiveDamageStat fields

## Dependencies

```toml
[project]
dependencies = [
    "wows-replay-parser",        # Git dependency — the replay parser
    "pycairo>=1.26",             # Cairo vector graphics (2D rendering)
    "discord.py>=2.3",           # Discord bot
    "python-dotenv>=1.0",        # .env file loading for bot config
    "click>=8.0",                # CLI (future)
    "rich>=13.0",                # CLI output (future)
]
```

```toml
[tool.uv.sources]
wows-replay-parser = { git = "ssh://git@github.com/toalba/wows-replay-parser.git" }
```

**External runtime dependencies:**
- **FFmpeg** must be on PATH (used via subprocess pipe, not a Python package)
- **Cairo** system library (pycairo is a binding, needs libcairo installed on Linux/macOS; Windows wheels include it)

## Data Dependencies

The renderer needs game assets from the wows-gamedata repo (git submodule):

| Asset | Path in gamedata repo | Used for |
|---|---|---|
| Minimap images | `data/spaces/<map>/minimap.png` | Map background layer |
| Water texture | `data/spaces/<map>/minimap_water.png` | Full canvas water background |
| Ship class icons | `data/gui/battle_hud/markers/ship/icon_*.png` | 28x28 RGBA ship class icons (ally/enemy/white/sunk × 6 classes) |
| Consumable icons | `data/gui/consumables/consumable_*.png` | Consumable layer icons near ships |
| Ribbon icons | `data/gui/ribbons/ribbon_*.png` | Ribbon counter display (main + subribbons/) |
| Plane icons | `data/gui/battle_hud/markers_minimap/plane/` | Aircraft layer (controllable/, airsupport/, consumables/) |
| Frag icons | `data/gui/battle_hud/icon_frag/` | Killfeed death reason icons |
| Damage widget icons | `data/gui/battle_hud/damage_widget/` | Team roster stat icons |
| Entity defs | `data/scripts_entity/entity_defs/` | Passed through to parser |
| ships.json | `data/ships.json` | Compact shipId→{name,species,nation,level,short_name} lookup |
| projectiles.json | `data/projectiles.json` | Projectile params_id→{ammo_type,caliber} lookup |
| map_sizes.json | `data/map_sizes.json` | space_size per map for coordinate transform |
| ship_consumables.json | `data/ship_consumables.json` | Per-ship consumable loadouts, detection ranges, reload timings |

### ship_consumables.json Format

```json
{
  "ship_id": {
    "slots": [["damage_control"], ["hydroacoustic", "surveillance_radar"], ...],
    "abilities": ["PCY009_CrashCrewPremium", ...],
    "has_repair_party": true,
    "ranges": {"sonar": 5000.0, "rls": 12000.0},
    "timings": {"damage_control": 60.0, "hydroacoustic": 120.0, "repair_party": 80.0}
  }
}
```

- `slots` is `list[list[str]]` — each slot has multiple consumable options
- `timings` maps category → base reload seconds (before skills/modules)
- `ranges` maps consumable type → detection range in meters

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
    │  Opens FFmpegPipe + FrameWriter (async background thread)
    │
    │  For each frame timestamp (iter_states for O(delta) queries):
    │    1. Clear surface
    │    2. For each layer: cr.save() → layer.render(cr, state, t) → cr.restore()
    │    3. surface.flush() → writer.write_frame(surface.get_data())  [async]
    │
    ▼
FrameWriter → FFmpegPipe
    │  bytes() copy in main thread, pipe write in background thread
    │  Raw BGRA buffer → ffmpeg stdin → h264 mp4
    │
    ▼
output.mp4
```

## Layer System

Each visual element is a separate Layer. All layers draw onto a **shared cairo.Context** — no separate images, no alpha compositing step. Layers are composited in add order (first = bottom).

```python
renderer = MinimapRenderer(config)
renderer.add_layer(MapBackgroundLayer())       # Bottom: water + minimap + grid (cached)
renderer.add_layer(TeamRosterLayer())          # Left panel: team rosters
renderer.add_layer(CapturePointLayer())
renderer.add_layer(SmokeLayer())
renderer.add_layer(ProjectileLayer())          # Shell traces + torpedoes
renderer.add_layer(AircraftLayer())            # CV squadrons + airstrikes
renderer.add_layer(ShipLayer())                # Ship icons + names
renderer.add_layer(HealthBarLayer())           # HP bars + ship names
renderer.add_layer(ConsumableLayer())          # Icons + radar/hydro circles
renderer.add_layer(RibbonLayer())              # Right panel: ribbon counters
renderer.add_layer(KillfeedLayer())            # Right panel: kill feed
renderer.add_layer(HudLayer())                 # Top: scores, timer, TTW
```

### Layer Interface

```python
class Layer(ABC):
    def initialize(self, ctx: RenderContext) -> None:
        """Called once before rendering. Preload assets, cache data."""
        self.ctx = ctx

    @abstractmethod
    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        """Draw this layer onto the shared cairo context."""
        ...

    # Text utilities (static methods on Layer):
    # draw_text_halo(cr, x, y, text, r, g, b, ...) — shadow-based readable text
    # draw_cached_text(cr, x, y, text, ...) → width — cached text surface blit
    # get_cached_text(cr, text, ...) → (surface, width, ascent)
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

## WG Bounty Requirements Mapping

### Core (required)

| Requirement | Layer/Component | Status |
|---|---|---|
| Parse replay → video | `core.py` + `video.py` | DONE |
| Both teams + ship names + HP bars | `ships.py` + `health_bars.py` + `team_roster.py` | DONE |
| Shells and torpedoes | `projectiles.py` | DONE |
| Discord bot + user interaction | `bot/` | DONE |
| Capture points + status + progress | `capture_points.py` | DONE |
| Total team points | `hud.py` | DONE |
| Maintained for 1 year | Automated gamedata pipeline | DONE (git pull) |
| Apache 2.0, WG copyright | `LICENSE` | TODO |

### Nice-to-have (P2)

| Feature | Layer/Component | Status |
|---|---|---|
| Ribbons | `ribbons.py` | DONE |
| Team roster side panels | `team_roster.py` | DONE |
| Aircraft (CV squadrons + airstrikes) | `aircraft.py` | DONE (airstrike team_id fix pending) |
| TTW + projected winner | `hud.py` | DONE |
| 1 kill swing indicator | `hud.py` | DONE |
| Kill feed | `killfeed.py` | DONE |
| Repair Party recoverable HP | `health_bars.py` | DONE |
| Per-player damage breakdown | `team_roster.py` | BLOCKED (needs parser typed damage) |
| Dual perspective combined | `merge.py` in parser | NOT STARTED |

## Discord Bot

### Architecture
- **ProcessPoolExecutor** (not threads) — cairo rendering is CPU-bound; separate processes bypass the GIL
- **`multiprocessing.Manager().Queue()`** — cross-process progress reporting, polled every 2s to update Discord message
- **Temp directory per render** — replay + mp4 in isolated tmpdir, cleaned up in `finally`
- **Deadline-based timeout** — cancels render if it exceeds `RENDER_TIMEOUT` (default 120s)

### Config (.env)
| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | (required) | Bot token |
| `GAMEDATA_PATH` | `wows-gamedata/data` | Path to game assets |
| `MAX_WORKERS` | `2` | Concurrent render processes |
| `RENDER_TIMEOUT` | `120` | Seconds before render is cancelled |
| `COOLDOWN_SECONDS` | `60` | Per-user rate limit |
| `MAX_UPLOAD_MB` | `50` | Max replay file size |

### Slash Command Flow
1. `/render` + `.wowsreplay` attachment
2. Validate extension + file size → defer interaction
3. Download to temp dir → dispatch to `ProcessPoolExecutor`
4. Poll progress queue → edit Discord message with `Rendering... X%`
5. Send mp4 with match duration, render time, file size
6. Cleanup temp dir

## Configurable Parameters (RenderConfig)

| Parameter | Default | Description |
|---|---|---|
| `minimap_size` | 760 | Minimap resolution (square, pixels) |
| `panel_width` | 220 | Side panel width (total width = panel + minimap + panel) |
| `speed` | 10x | Playback speed multiplier |
| `fps` | 20 | Output video frames per second |
| `start_time` | 0 | Start time in seconds (0 = auto-detect battle start) |
| `end_time` | None | End time (None = match end) |
| `codec` | libx264 | FFmpeg codec |
| `crf` | 23 | Quality (lower = better, 18-28 typical) |
| `trail_length` | 30.0 | Ship trail duration in seconds |
| `team_colors` | green/red | RGBA tuples per display team (0=ally, 1=enemy) |
| `self_color` | white | RGBA for the recording player's ship |
| `hud_height` | 24 | Score bar height above minimap |

## Development

```bash
# Setup
cd wows-renderer
uv venv && source .venv/bin/activate
uv pip install -e "."

# Quick render (all layers, 20x, 1080px)
python render_quick.py battle.wowsreplay output.mp4

# Run Discord bot (requires .env with DISCORD_TOKEN)
python -m bot.main
# or: wows-bot

# Docker
eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_rsa
DOCKER_BUILDKIT=1 docker build --ssh default -t wows-renderer .
docker run --env-file .env wows-renderer

# Update parser
uv pip install "wows-replay-parser @ git+ssh://git@github.com/toalba/wows-replay-parser.git" --reinstall
```

## Remaining Work

### Before Submission (April 20)
1. ~~Discord bot (`bot/`) — slash commands, file upload, render worker~~ DONE
2. CLI implementation (`renderer/cli.py`)
3. LICENSE file (Apache 2.0, WG copyright)
4. Fix aircraft airstrike team_id (parser fix in progress)
5. Per-player damage type breakdown (AP/HE/fire/flood) — needs parser typed damage events
6. Visual polish + edge case handling

### Nice-to-have (P2)
7. Dual perspective merge
8. Version-awareness for projectiles.json and ship_consumables.json (cross-patch replay support)

## Parser Requirements for Damage Breakdown

The renderer needs typed damage events to show per-player damage breakdown (AP/HE/SAP/torp/fire/flood) in the team roster.

### What exists in replay data

**`receiveDamageStat`** (self player only, OWN_CLIENT method):
- `arg0` = `ammo_id` (maps to projectiles.json → `{a: "AP"|"HE"|"SAP"|"torpedo", c: caliber}`)
- `arg1` = flags (bitfield, meaning TBD)
- `arg2` = damage amount
- `arg3` = target entity_id

**`receiveDamage`** (all players, simpler):
- `vehicleID` = attacker entity_id
- `damage` = amount
- No ammo type. When `vehicleID` = self, these are fire/flood DoT dealt by self.

### What the parser should expose on DamageEvent

1. **`ammo_id`** — populate from `receiveDamageStat` arg0 (currently empty)
2. **`damage_type`** — one of: `"AP"`, `"HE"`, `"SAP"`, `"torpedo"`, `"fire"`, `"flood"`, `"secondary"`
   - For `receiveDamageStat`: look up arg0 in projectiles.json
   - For `receiveDamage` DoT (self as vehicleID): classify fire vs flood (tick pattern: ~228/tick = fire; cross-ref with `burningFlags` or flood ribbon events)
3. **`attacker_id`** — the entity that dealt the damage (currently in `raw_data["vehicleID"]`, should be a first-class field)

### Limitations
- Typed damage is only available for the **recording player** (via `receiveDamageStat`)
- Other players only have total damage from `receiveDamage` — no type breakdown possible
- The renderer will show typed breakdown for self, total for others

## License

Apache 2.0 — Copyright Wargaming.net
Developed on Wargaming's request for the community.
