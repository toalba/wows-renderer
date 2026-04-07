# wows-minimap-renderer

Minimap replay renderer + Discord bot for World of Warships. Parses `.wowsreplay` files and produces mp4 videos showing ship movements, shells, torpedoes, capture points, health bars, smoke screens, consumables, aircraft, ribbons, team rosters, and team scores. Built for the Wargaming community bounty (KOTS referee tooling).

**Deadline: April 20, 2026**

## Architecture

```
wows-minimap-renderer/
├── renderer/                  # Core rendering engine
│   ├── core.py                # MinimapRenderer — frame loop, layer compositing, async frame writer
│   ├── game_state.py          # GameStateAdapter — bridges parser state to render state
│   ├── config.py              # RenderConfig — resolution, fps, speed, team colors (with validation)
│   ├── layers/                # Layer-based rendering (composited per frame, bottom to top)
│   │   ├── base.py            # Layer ABC + RenderContext + text cache (draw_cached_text, draw_text_halo)
│   │   ├── map_bg.py          # Minimap background (pre-rendered static cache: water + minimap + grid)
│   │   ├── team_roster.py     # Left panel: team rosters (player/ship names, kills, damage, HP bar, consumable timers)
│   │   ├── capture_points.py  # Cap circles + progress + team color
│   │   ├── trails.py          # Ship movement trails (fading lines, pre-sampled)
│   │   ├── smoke.py           # Smoke screen radius visualization
│   │   ├── projectiles.py     # Shell traces + torpedo dots (ammo-type colored)
│   │   ├── aircraft.py        # CV squadrons + airstrike icons on minimap
│   │   ├── ships.py           # Ship class icons (rotated by yaw) + player names + spotted glow
│   │   ├── health_bars.py     # Per-ship HP bars + repair party recoverable HP + ship names
│   │   ├── consumables.py     # Consumable icons near ships + radar/hydro detection radius circles
│   │   ├── player_header.py   # Right panel: self-player header with ship silhouette HP bar
│   │   ├── damage_stats.py    # Right panel: self-player damage dealt/received/spotted/potential breakdown
│   │   ├── ribbons.py         # Right panel: recording player ribbon counters (grouped, accumulating)
│   │   ├── killfeed.py        # Right panel: kill feed (bottom-up)
│   │   ├── right_panel.py     # Right panel composite: player_header + damage_stats + ribbons + killfeed
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
├── profile_frames.py          # Per-frame timing profiler for render pipeline analysis
├── Dockerfile                 # Multi-stage build (builder + slim runtime with ffmpeg/cairo)
├── .dockerignore
├── pyproject.toml
└── CLAUDE.md
```

## Current Status (2026-04-07)

### Working — 16 Rendering Layers
1. **map_bg** — Water texture + minimap PNG + grid + labels (pre-rendered static cache, single paint per frame)
2. **team_roster** — Left panel with both teams: class icon, player name, ship name, kills (incremental), damage (incremental), HP bar, consumable icons with active/cooldown timers
3. **capture_points** — Cap circles with progress arcs, team colors, contested indicators, A-H labels
4. **smoke** — Smoke screen radius circles from NESTED_PROPERTY puff positions, per-puff FIFO lifecycle (earlier puffs expire first)
5. **projectiles** — Shell traces colored by ammo type (AP=white, HE=orange, SAP=pink) + torpedo dots; caliber-scaled line widths
6. **aircraft** — CV squadrons + airstrikes + consumable planes on minimap with type-specific icons (fighter/bomber/torpedo/skip/scout/depth charge) resolved from GameParams split data via params_id
7. **ships** — Ship class SVG icons (from minimap ship_icons/, tinted per team, cairosvg) + player names (cached text surfaces) + spotted glow + division mate gold icons
8. **health_bars** — Per-ship HP bars (green/yellow/red) + repair party recoverable segment + ship names (cached)
9. **consumables** — Consumable icons near ships + radar/hydro detection radius circles (team-colored: blue=ally, red=enemy)
10. **player_header** — Right panel top: self-player header with ship silhouette HP bar, healable segment, clan tag + name
11. **damage_stats** — Right panel: self-player damage breakdown (dealt by weapon type, spotting, potential) using DamageReceivedStatEvent
12. **ribbons** — Right panel: recording player's ribbon counters in grouped layout (main + sub-ribbons), accumulating per frame, first-appearance order
13. **killfeed** — Right panel: recent kills with frag icons, killer/victim names + ships, bottom-anchored growing upward
14. **right_panel** — Composite layer: player_header + damage_stats + ribbons + killfeed with clipping
15. **hud** — Score bar with projected winner highlight, MM:SS timer, TTW pills (diamond icon, winner highlighting), "1 KILL DECIDES" indicator (team-colored glow), match result overlay, clan battle clan tags (with clan colors)
16. **trails** — Fading ship movement trails (pre-sampled at init, gap detection)

### Performance
- **~60 fps** rendering at 1920x1104 (1080px minimap + 420px panels)
- **~17ms/frame** average (encode 34%, team_roster 21%, ships 8%, right_panel 8%)
- **Async FrameWriter** — pipe I/O offloaded to background thread (video.py), queue size 16
- **FFmpeg fast preset** — 3x smaller output vs ultrafast (~5MB vs 16MB for typical match)
- **Static background cache** — map_bg renders once at init, single cr.paint() per frame
- **Text surface cache** — draw_cached_text() renders text to small surfaces once, blits via cr.paint()
- **Index-based timestamps** — avoids float accumulation drift

### Other Features
- Ship positions (all players including self) with team colors (green=ally, red=enemy, white=self)
- **Division mate highlighting** — gold yellow icons + roster icons for players in recording player's division (disabled in clan battles)
- **Clan battle support** — clan tags displayed below score bar in each clan's color (majority clan ≥4 players per team)
- **Game type in Discord message** — shows RandomBattle, ClanBattle, CooperativeBattle etc.
- **Per-phase timing instrumentation** — parse/render/encode/upload breakdown logged after each render
- Self player position tracking via PLAYER_ORIENTATION (0x2C) packets
- Self-team detection and perspective swap (Trap 5)
- **Vision-based enemy visibility** — enemies appear when first spotted via MinimapVisionEvent, not when first position packet arrives (fixes multi-second gaps)
- Undetected enemies shown at 40% alpha (detection from visibility_flags)
- Dead ships shown with sunk icon variant
- ship_consumables.json loading: works with or without split/Ship directory
- Consumable cooldown: min(gap between uses, base reload from GameParams)
- iter_states() for O(delta) incremental state queries
- RenderConfig validation (fps, speed, crf, sizes) + str-to-Path coercion
- Self-player typed damage breakdown (AP/HE/SAP/torp/fire/flood/secondary) via DamageReceivedStatEvent

### Known Issues
- Ribbon derive_ribbons() has a bug (RIBBON_NAMES dict inverted) — using extract_recording_player_ribbons() instead
- Airstrike icons for other players' airstrikes may show default (bomber_depth_charge) when params_id=0 in wire protocol

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
| Ship class icons | `data/gui/battle_hud/markers/ship/icon_*.png` | 28x28 RGBA ship class icons (ally/enemy/white/sunk x 6 classes) |
| Consumable icons | `data/gui/consumables/consumable_*.png` | Consumable layer icons near ships |
| Ribbon icons | `data/gui/ribbons/ribbon_*.png` | Ribbon counter display (main + subribbons/) |
| Plane icons | `data/gui/battle_hud/markers_minimap/plane/` | Aircraft layer (controllable/, airsupport/, consumables/) |
| Frag icons | `data/gui/battle_hud/icon_frag/` | Killfeed death reason icons |
| Damage widget icons | `data/gui/battle_hud/damage_widget/` | Team roster stat icons |
| Ship silhouettes | `data/gui/battle_hud/ship_silhouettes/` | Player header ship silhouette HP bar |
| Entity defs | `data/scripts_entity/entity_defs/` | Passed through to parser |
| ships.json | `data/ships.json` | Compact shipId->{name,species,nation,level,short_name} lookup |
| projectiles.json | `data/projectiles.json` | Projectile params_id->{ammo_type,caliber} lookup |
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
- `timings` maps category -> base reload seconds (before skills/modules)
- `ranges` maps consumable type -> detection range in meters

## Rendering Pipeline

```
ParsedReplay (from wows-replay-parser)
    |
    |  replay = parse_replay("battle.wowsreplay", gamedata_path)
    |
    v
GameStateAdapter
    |  Wraps replay for the renderer
    |  Resolves map_size, player_lookup (entity_id -> PlayerInfo)
    |
    v
MinimapRenderer
    |  Creates a single reusable cairo.ImageSurface (ARGB32)
    |  Opens FFmpegPipe + FrameWriter (async background thread)
    |
    |  For each frame timestamp (iter_states for O(delta) queries):
    |    1. Clear surface
    |    2. For each layer: cr.save() -> layer.render(cr, state, t) -> cr.restore()
    |    3. surface.flush() -> writer.write_frame(surface.get_data())  [async]
    |
    v
FrameWriter -> FFmpegPipe
    |  bytes() copy in main thread, pipe write in background thread
    |  Raw BGRA buffer -> ffmpeg stdin -> h264 mp4
    |
    v
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
renderer.add_layer(RightPanelLayer())          # Right panel: header + damage + ribbons + killfeed
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
    # draw_cached_text(cr, x, y, text, ...) -> width — cached text surface blit
    # get_cached_text(cr, text, ...) -> (surface, width, ascent)
```

### RenderContext

Shared context passed to all layers via `initialize()`:

| Field | Type | Description |
|---|---|---|
| `config` | `RenderConfig` | Layout, video, rendering settings |
| `replay` | `ParsedReplay` | The parsed replay |
| `map_size` | `float` | space_size from map_sizes.json |
| `player_lookup` | `dict[int, PlayerInfo]` | entity_id -> player info |
| `ship_db` | `dict[int, dict] | None` | ship_id -> {name, species, nation, level} |
| `ship_icons` | `dict[str, dict] | None` | species -> {ally/enemy/white/division/sunk: cairo.ImageSurface} |
| `division_mates` | `set[int]` | entity_ids of recording player's division mates (empty for clan battles) |
| `first_seen` | `dict[int, float] | None` | entity_id -> first position timestamp |
| `scale` | `float` (property) | Scale factor relative to 760px reference |

Key methods:
- `world_to_pixel(x, z)` -> `(px, py)` — world coords to canvas pixel coords
- `is_visible(entity_id, timestamp)` — whether entity has been seen yet
- `raw_to_display_team(raw_team_id)` -> `0` (ally) or `1` (enemy) — handles perspective swap

## WG Bounty Requirements Mapping

### Core (required)

| Requirement | Layer/Component | Status |
|---|---|---|
| Parse replay -> video | `core.py` + `video.py` | DONE |
| Both teams + ship names + HP bars | `ships.py` + `health_bars.py` + `team_roster.py` | DONE |
| Shells and torpedoes | `projectiles.py` | DONE |
| Discord bot + user interaction | `bot/` | DONE |
| Capture points + status + progress | `capture_points.py` | DONE |
| Total team points | `hud.py` | DONE |
| Maintained for 1 year | Automated gamedata pipeline + gamedata_sync | DONE (auto-checkout matching version tag) |
| Apache 2.0, WG copyright | `LICENSE` | TODO |

### Nice-to-have (P2)

| Feature | Layer/Component | Status |
|---|---|---|
| Ribbons | `ribbons.py` | DONE |
| Team roster side panels | `team_roster.py` | DONE |
| Aircraft (CV squadrons + airstrikes) | `aircraft.py` | DONE (type-specific icons from GameParams) |
| TTW + projected winner | `hud.py` | DONE |
| 1 kill swing indicator | `hud.py` | DONE |
| Kill feed | `killfeed.py` | DONE |
| Repair Party recoverable HP | `health_bars.py` | DONE |
| Self-player damage breakdown | `damage_stats.py` | DONE (via DamageReceivedStatEvent) |
| Self-player header + silhouette HP | `player_header.py` | DONE |
| Division mate highlighting | `ships.py` + `team_roster.py` + `base.py` | DONE (gold icons on minimap + roster, disabled in clan battles) |
| Clan battle clan tags | `hud.py` | DONE (below score bar, clan colors, majority ≥4 players) |
| Game type in Discord message | `cog_render.py` + `worker.py` | DONE |
| Per-phase timing instrumentation | `worker.py` + `core.py` + `cog_render.py` | DONE (parse/render/encode/upload) |
| Per-player damage breakdown (all players) | — | NOT POSSIBLE (game protocol only sends typed damage for self) |
| Dual perspective combined | `merge.py` in parser | NOT STARTED |

## Discord Bot

### Architecture
- **ProcessPoolExecutor** (not threads) — cairo rendering is CPU-bound; separate processes bypass the GIL
- **`multiprocessing.Manager().Queue()`** — cross-process progress reporting, polled every 2s to update Discord message
- **Temp directory per render** — replay + mp4 in isolated tmpdir, cleaned up in `finally`
- **Deadline-based timeout** — cancels render if it exceeds `RENDER_TIMEOUT` (default 120s)
- **Path sanitization** — replay filenames stripped of directory traversal

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
2. Validate extension + file size -> defer interaction
3. Download to temp dir -> dispatch to `ProcessPoolExecutor`
4. Poll progress queue -> edit Discord message with `Rendering... X%`
5. Send mp4 with game type, match duration, render time, file size
6. Log per-phase timing breakdown (parse/render/encode/upload)
7. Cleanup temp dir

## Configurable Parameters (RenderConfig)

All parameters are validated in `__post_init__()`. String paths are auto-coerced to `Path`.

| Parameter | Default | Description |
|---|---|---|
| `minimap_size` | 760 | Minimap resolution (square, pixels); must be > 0 |
| `panel_width` | 220 | Side panel width (total width = panel + minimap + panel); must be >= 0 |
| `speed` | 10x | Playback speed multiplier; must be > 0 |
| `fps` | 20 | Output video frames per second; must be > 0 |
| `start_time` | 0 | Start time in seconds (0 = auto-detect battle start); must be >= 0 |
| `end_time` | None | End time (None = match end) |
| `codec` | libx264 | FFmpeg codec |
| `crf` | 23 | Quality (lower = better); must be 0-51 |
| `trail_length` | 30.0 | Ship trail duration in seconds; must be >= 0 |
| `team_colors` | green/red | RGBA tuples per display team (0=ally, 1=enemy) |
| `self_color` | white | RGBA for the recording player's ship |
| `division_color` | gold yellow | RGBA for division mate highlighting |
| `hud_height` | 24 | Score bar height above minimap |

## Development

```bash
# Setup
cd wows-renderer
uv venv && source .venv/bin/activate
uv pip install -e "."

# Quick render (all layers, 20x, 1080px)
python render_quick.py battle.wowsreplay output.mp4

# Profile render performance (per-layer timing breakdown)
python profile_frames.py battle.wowsreplay /tmp/profile.mp4

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
4. ~~Fix aircraft airstrike team_id~~ DONE (parser commit e335703)
5. ~~Per-player damage type breakdown~~ DONE for self player; NOT POSSIBLE for other players (game protocol limitation)
6. ~~Visual polish + edge case handling~~ DONE (visibility, smoke lifecycle, aircraft icons, SVG ship icons)

### Nice-to-have (P2)
7. Dual perspective merge
8. ~~Version-awareness for gamedata~~ DONE (gamedata_sync auto-checkouts matching tag)

## Damage Breakdown

### What's implemented

**Self-player breakdown** via `DamageReceivedStatEvent` (from `receiveDamageStat` packets):
- `damage_stats.py` renders weapon-category breakdown: AP, HE, SAP, secondary, torpedo, fire, flood, rockets, bombs, depth charges, ram
- Sections: damage dealt (ENEMY), spotting damage (SPOT), potential damage (AGRO)
- 85 weapon categories available from the parser

**All-player total damage** via `DamageEvent` (from `receiveDamagesOnShip` packets):
- `team_roster.py` shows aggregate damage per player (no type breakdown)

### Limitation
Per-player typed damage breakdown for all players is **not possible** — the game protocol only sends `receiveDamageStat` (which includes ammo_id) for the recording player. Other players only get `receiveDamagesOnShip` with total damage, no type info. This is a game protocol constraint, not a parser limitation.

## License

Apache 2.0 — Copyright Wargaming.net
Developed on Wargaming's request for the community.
