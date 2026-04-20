# wows-minimap-renderer

Minimap replay renderer + Discord bot for World of Warships. Parses `.wowsreplay` files and produces mp4 videos showing ship movements, shells, torpedoes, capture points, health bars, smoke screens, consumables, aircraft, ribbons, team rosters, and team scores. Built for the Wargaming community bounty (KOTS referee tooling).

## Continuous Integration

Nightly canary (`.github/workflows/canary.yml`) runs the unit suite and a
Docker builder-stage dry run. On failure it opens a `ci-regression` issue —
almost always this means upstream `wows-replay-parser` main broke, so check
<https://github.com/toalba/wows-replay-parser> for recent changes before
poking at the renderer. Schema breaks from new WoWS patches are caught by the
parser's own canary, not this one.

## Architecture

```
wows-minimap-renderer/
├── renderer/                  # Core rendering engine
│   ├── core.py                # MinimapRenderer — frame loop, layer compositing, async frame writer
│   ├── game_state.py          # GameStateAdapter — bridges parser state to render state
│   ├── config.py              # RenderConfig — resolution, fps, speed, team colors, versioned_gamedata
│   ├── gameparams.py          # GameParams.data decode + blake2b pickle cache
│   ├── gamedata_cache.py      # Per-version gamedata cache (VersionedGamedata, git archive extraction)
│   ├── gamedata_resolver.py   # JSON cache resolver (fallback for cold-load without GameParams)
│   ├── layers/                # Layer-based rendering (composited per frame, bottom to top)
│   │   ├── base.py            # Layer ABC + RenderContext + text cache (draw_cached_text, draw_text_halo)
│   │   ├── map_bg.py          # Minimap background (pre-rendered static cache: water + minimap + grid)
│   │   ├── team_roster.py     # Left panel: team rosters (player/ship names, kills, damage, HP bar, consumable timers)
│   │   ├── capture_points.py  # Cap circles + progress + team color + buff zones (Arms Race)
│   │   ├── trails.py          # Ship movement trails (fading lines, pre-sampled)
│   │   ├── smoke.py           # Smoke screen radius visualization
│   │   ├── weather.py         # Weather zone overlay (InteractiveZone type==5)
│   │   ├── projectiles.py     # Shell traces + torpedo dots (ammo-type colored)
│   │   ├── aircraft.py        # CV squadrons + airstrike icons on minimap
│   │   ├── ships.py           # Ship class icons (rotated by yaw) + player names + spotted glow
│   │   ├── health_bars.py     # Per-ship HP bars + repair party recoverable HP + ship names
│   │   ├── consumables.py     # Consumable icons near ships + radar/hydro detection radius circles
│   │   ├── player_header.py   # Right panel: self-player header with ship silhouette HP bar
│   │   ├── damage_stats.py    # Right panel: self-player damage dealt/received/spotted/potential breakdown
│   │   ├── ribbons.py         # Right panel: recording player ribbon counters (grouped, accumulating)
│   │   ├── killfeed.py        # Right panel: kill feed + chat messages, bottom-up
│   │   ├── right_panel.py     # Right panel composite: player_header + damage_stats + ribbons + killfeed
│   │   └── hud.py             # Score bar, timer, TTW pills, 1-kill-swing indicator, match result
│   ├── video.py               # FFmpegPipe + FrameWriter (async background thread for pipe I/O)
│   └── assets.py              # Asset loading (minimaps, ship icons, consumable icons, ribbons, projectiles, ships.json, map_sizes, ship_consumables)
├── scripts/
│   └── decode_gameparams.py   # CLI: Decode GameParams.data → JSON / split files
├── bot/                       # Discord bot (slash command /render)
│   ├── main.py                # Bot entry point — creates Bot, loads cog, async cache population at boot
│   ├── config.py              # BotConfig — reads .env
│   ├── cog_render.py          # RenderCog — /render slash command, async progress polling, file upload/download
│   └── worker.py              # render_replay() — picklable function for ProcessPoolExecutor
├── render_quick.py            # Single-replay render (all layers, 20x speed, 1080px → output.mp4)
├── render_dual.py             # Dual-perspective merged render (two paired replays → single video)
├── profile_frames.py          # Per-frame timing profiler for render pipeline analysis
├── Dockerfile                 # Multi-stage build (builder + slim runtime with ffmpeg/cairo/git)
├── LICENSE                    # Apache 2.0
├── pyproject.toml
└── CLAUDE.md
```

## Features

### Rendering Layers (16 total, composited bottom to top)
1. **map_bg** — Water texture + minimap PNG + grid + labels (pre-rendered static cache, single paint per frame)
2. **team_roster** — Left panel with both teams: class icon, player name, ship name, kills (incremental), damage (incremental), HP bar, consumable icons with active/cooldown timers and charge counts
3. **capture_points** — Cap circles with progress arcs, team colors, contested indicators, A-H labels, Arms Race buff zones
4. **weather** — White semi-transparent circles from `GameState.weather_zones` (InteractiveZone type==5)
5. **smoke** — Smoke screen radius circles from NESTED_PROPERTY puff positions, per-puff FIFO lifecycle (earlier puffs expire first)
6. **trails** — Fading ship movement trails (pre-sampled at init, gap detection)
7. **projectiles** — Shell traces colored by ammo type (AP=white, HE=orange, SAP=pink) + torpedo dots; caliber-scaled line widths
8. **aircraft** — CV squadrons + airstrikes + consumable planes on minimap with type-specific icons (fighter/bomber/torpedo/skip/scout/depth charge) resolved from GameParams split data via params_id
9. **ships** — Ship class SVG icons (from minimap ship_icons/, tinted per team, cairosvg) + player names (cached text surfaces) + spotted glow + division mate gold icons
10. **health_bars** — Per-ship HP bars (green/yellow/red) + repair party recoverable segment + ship names (cached)
11. **consumables** — Consumable icons near ships + radar/hydro/hydrophone detection radius circles (team-colored: blue=ally, red=enemy)
12. **player_header** — Right panel top: self-player header with ship silhouette HP bar, healable segment, clan tag + name
13. **damage_stats** — Right panel: self-player damage breakdown (dealt by weapon type, spotting, potential) using DamageReceivedStatEvent
14. **ribbons** — Right panel: recording player's ribbon counters in grouped layout (main + sub-ribbons), accumulating per frame, first-appearance order
15. **killfeed** — Right panel: recent kills with frag icons, killer/victim names + ships, interleaved with chat messages (team chat prefixed `[T]`, pre-battle `[P]`), bottom-anchored growing upward
16. **right_panel** — Composite layer: player_header + damage_stats + ribbons + killfeed with clipping
17. **hud** — Score bar with projected winner highlight, MM:SS timer, TTW pills (diamond icon, winner highlighting), "1 KILL DECIDES" indicator (team-colored glow), match result overlay, clan battle clan tags (with clan colors)

Note: layer 4 (`weather`) and layer 6 (`trails`) are omitted in `render_quick.py` — see the actual layer list in that script for the canonical ordering.

### Performance
- **~57 fps** rendering at 1920x1104 (1080px minimap + 420px panels)
- **~17ms/frame** average (encode 40%, team_roster 16%, overhead 12%, right_panel 9%, ships 7%)
- **Async FrameWriter** — pipe I/O offloaded to background thread (video.py), queue size 16
- **FFmpeg fast preset** — 3x smaller output vs ultrafast (~5MB vs 16MB for typical match)
- **Static background cache** — map_bg renders once at init, single cr.paint() per frame
- **Text surface cache** — draw_cached_text() renders text to small surfaces once, blits via cr.paint()
- **Index-based timestamps** — avoids float accumulation drift
- **Per-version gamedata cache** — immutable cache dirs, no git checkout at render time, concurrent-worker safe
- **Lazy GameParams pickle** — 15MB pickle loaded on first property access, not at construction
- **In-memory consumable reload calc** — pre-indexed Modernization/Crew dicts, no file I/O

### Other Features
- Ship positions (all players including self) with team colors (green=ally, red=enemy, white=self)
- **Division mate highlighting** — gold yellow icons + roster icons for players in recording player's division (disabled in clan battles)
- **Clan battle support** — clan tags displayed below score bar in each clan's color (majority clan ≥4 players per team)
- **Game type in Discord message** — shows RandomBattle, ClanBattle, CooperativeBattle etc.
- **Per-phase timing instrumentation** — resolve/parse/setup/render/encode/upload breakdown + per-layer init timings logged after each render
- Self player position tracking via PLAYER_ORIENTATION (0x2C) packets
- Self-team detection and perspective swap (see Trap 5 below)
- **Vision-based enemy visibility** — enemies appear when first spotted via MinimapVisionEvent, not when first position packet arrives (fixes multi-second gaps)
- Undetected enemies shown at 40% alpha (detection from visibility_flags)
- Dead ships shown with sunk icon variant
- Consumable cooldown: computed from base reload (GameParams) + modernization/skill modifiers (in-memory, no file I/O)
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
    "click>=8.0",                # CLI dependencies (reserved)
    "rich>=13.0",                # CLI dependencies (reserved)
]
```

```toml
[tool.uv.sources]
wows-replay-parser = { path = "../wows-replay-parser" }  # local dev; Docker uses SSH git source
```

**External runtime dependencies:**
- **FFmpeg** must be on PATH (used via subprocess pipe, not a Python package)
- **Cairo** system library (pycairo is a binding, needs libcairo installed on Linux/macOS; Windows wheels include it)
- **Git** must be on PATH (used by gamedata_cache.py for `git archive` + `git tag` to extract version-specific data)

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

## Gamedata Cache System

Per-version gamedata isolation using `renderer/gamedata_cache.py`. Each game version gets an immutable cache directory under `~/.cache/wows-gamedata/v{build_id}/`. No `git checkout` at render time — multiple workers can render different version replays concurrently.

### How It Works
1. Replay comes in → `resolve_for_replay()` reads JSON header, extracts build ID
2. Cache hit (`.ready` sentinel exists) → return `VersionedGamedata` instantly (no pickle load yet)
3. Cache miss → `git archive` extracts files from matching tag into temp dir → decode `GameParams.data` → pickle cache → atomic rename
4. Layers access `config.effective_gamedata_path` (→ `version_dir/data/`) for file assets
5. GameParams pickle loaded lazily on first access to `vgd.ships_db`, `vgd.modernizations`, etc.

### Cache Layout
```
~/.cache/wows-gamedata/v{build_id}/
├── .ready                         # sentinel
├── gameparams.pickle              # decoded GameParams (loaded lazily)
├── gameparams.blake2b             # hash for invalidation
├── data/
│   ├── scripts_entity/entity_defs/  # for parser
│   ├── ships.json, projectiles.json, ship_consumables.json, ...
│   ├── split/Modernization/, split/Crew/  # for consumable_calc.py
│   ├── gui/                       # icons, ribbons, fonts
│   ├── spaces/                    # minimaps
│   └── global.mo                  # localization
```

### Key Components
- **`renderer/gameparams.py`** — `decode_gameparams()` (reverse + zlib + pickle), `decode_and_cache_gameparams()` (blake2b-keyed pickle cache)
- **`renderer/gamedata_cache.py`** — `VersionedGamedata` dataclass with `@cached_property` for ships_db/projectiles_db/ship_consumables/aircraft_icon_map/modernizations/crews, `ensure_version_cache()`, `resolve_for_replay()`, `populate_all_caches()`
- **`renderer/config.py`** — `versioned_gamedata` field, `effective_gamedata_path` property
- **Cold-load fallback** — `VersionedGamedata.from_gamedata_path()` decodes `GameParams.data` directly from a raw gamedata directory

### Bot Startup
`bot/main.py` runs `populate_all_caches()` as an async background task in `setup_hook` — all version tags are pre-cached before the first render request. New versions get cached lazily.

## Rendering Pipeline

```
.wowsreplay file
    |
    |  vgd = resolve_for_replay(replay_path, gamedata_repo)  # version cache
    |  replay = parse_replay(replay_path, vgd.entity_defs_path)
    |
    v
ParsedReplay (from wows-replay-parser)
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

## Dual Perspective Rendering

Entry point: `python render_dual.py a.wowsreplay b.wowsreplay output.mp4`. Both replays must come from the same match (parser's `merge_replays` validates `arenaUniqueId` and map_name). `DualMinimapRenderer` consumes the `MergedReplay` identically to a `ParsedReplay` via the `ReplaySource` protocol. Drops self-centric layers (`player_header`, `damage_stats`, `ribbons`, `killfeed`, `right_panel`). Neutral observer mode: no Trap-5 perspective swap — team 0 = green/left, team 1 = red/right regardless of either recorder's side. `division_mates` is empty (no recording player in merged view). Validated end-to-end on real paired replays.

## Layer System

Each visual element is a separate Layer. All layers draw onto a **shared cairo.Context** — no separate images, no alpha compositing step. Layers are composited in add order (first = bottom).

```python
renderer = MinimapRenderer(config)
renderer.add_layer(MapBackgroundLayer())       # Bottom: water + minimap + grid (cached)
renderer.add_layer(TeamRosterLayer())          # Left panel: team rosters
renderer.add_layer(CapturePointLayer())
renderer.add_layer(WeatherLayer())
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
| `ship_db` | `dict[int, dict] \| None` | ship_id -> {name, species, nation, level} |
| `ship_icons` | `dict[str, dict] \| None` | species -> {ally/enemy/white/division/sunk: cairo.ImageSurface} |
| `division_mates` | `set[int]` | entity_ids of recording player's division mates (empty for clan battles) |
| `first_seen` | `dict[int, float] \| None` | entity_id -> first position timestamp |
| `scale` | `float` (property) | Scale factor relative to 760px reference |

Key methods:
- `world_to_pixel(x, z)` -> `(px, py)` — world coords to canvas pixel coords
- `is_visible(entity_id, timestamp)` — whether entity has been seen yet
- `raw_to_display_team(raw_team_id)` -> `0` (ally) or `1` (enemy) — handles perspective swap

## Renderer Traps (invariants)

Pitfalls that bite the minimap renderer. Each of these has been hit in development and is now load-bearing — change with care.

### Trap 1: Coordinate Mapping — space_size, not game meters

Replay positions are in BigWorld space units. The formula:
```python
scaling = 760.0 / space_size
pixel_x = round(pos_x * scaling + 380)
pixel_y = round(-pos_z * scaling + 380)  # Z axis inverted
```

`space_size` per map comes from `manifest.json` / `space.settings` (via `map_sizes.json`). Typical values: 800, 1000, 1200, 1400, 1600. **Do not confuse with in-game meters (24000, 30000, 42000, 48000).** Using game meters as map size clusters every ship in the middle of the map.

### Trap 2: NormalizedPos → World → Pixel (three steps)

MinimapVisionInfo positions must first be converted to world coordinates, then run through the normal `world_to_pixel()`:
```python
# Step 1: Stored → raw 11-bit
raw_x = (stored_x + 1.5) * 512.0
raw_y = (stored_y + 1.5) * 512.0
# Step 2: Raw → world
world_x = raw_x / 2047.0 * 5000.0 - 2500.0
world_z = raw_y / 2047.0 * 5000.0 - 2500.0
# Step 3: World → pixel (same formula as position packets)
```
Without this path, MinimapVisionInfo ships render at completely wrong positions.

### Trap 3: Radius conversion — cap vs consumable

**Cap point radius and smoke radius are in space units:**
```python
px_radius = radius / space_size * minimap_size        # No / 30!
```
**Weapon ranges and consumable radii are in meters:**
```python
px_radius = radius_meters / 30.0 / space_size * minimap_size
px_radius = radius_km * 1000.0 / 30.0 / space_size * minimap_size
```
Mixing them up is a factor-30 error — cap circles that fill half the screen, or become invisibly small.

### Trap 4: Yaw/heading conversion

Minimap heading (from `updateMinimapVisionInfo`) is in degrees, compass format (0 = north, clockwise positive). For screen rendering:
```python
screen_yaw = math.pi / 2 - math.radians(heading_degrees)
```
World yaw (from position packets) uses a different convention. **Prefer minimap heading** for ship icon rotation when available — it is more accurate.

### Trap 5: Self-team ID and perspective swap

The replay file is from one perspective. `team_id` is 0 or 1, but which team the recorder belongs to varies per replay.
1. Find the own player via `relation == Self` (or `relation == 0`)
2. Read that player's `team_id` from the Vehicle entity
3. If `self_team_id == 1`: swap everything (score bar side, cap colors, ship colors, team advantage)

Without the swap, colors are wrong in ~50% of replays. `RenderContext.raw_to_display_team()` encapsulates this. In dual-perspective rendering, the swap is disabled (neutral observer mode).

### Trap 6: Detected vs undetected ships

| Status | Rendering |
|--------|-----------|
| Detected (minimap.visible = true) | Full opacity, name, HP bar |
| Undetected (minimap.visible = false) | 40% opacity, no name, no HP |
| Dead | Sunk icon at last known position |

Undetected ships are shown at their last-known minimap position, not hidden. A KOTS referee needs to see where a ship was last spotted.

### Trap 7: Dead ship positions

Dead ships need their last position:
- `dead.position` — world position at time of death (preferred)
- `dead.minimap_position` — minimap position as fallback
- Last known heading for icon rotation

Dead ships vanishing instead of showing a sunk icon loses critical referee information.

### Trap 8: Shell tracer animation

Shells render as animated tracers, not static lines. Per frame:
```python
elapsed = current_time - fired_at
flight_duration = distance(origin, target) / speed
frac = elapsed / flight_duration
if 0 <= frac <= 1:
    head = lerp(origin, target, frac)
    tail = lerp(origin, target, max(0, frac - 0.12))  # 12% trail length
    draw_line(tail, head, team_color)
```
Without flight-time interpolation you either see nothing (shells exist for a single frame) or static lines that linger the entire match.

### Trap 9: Torpedo position interpolation

Torpedoes have no target point. Position is computed from origin + direction:

**Straight torps:**
```python
pos = origin + direction * elapsed
```
**S-turn torps (`maneuverDump != None`):**
```python
initial_yaw = atan2(dir.x, dir.z)
speed = magnitude(direction)
w = sign(target_yaw - initial_yaw) * yaw_speed
turn_duration = abs(target_yaw - initial_yaw) / yaw_speed
if elapsed < turn_duration:
    # Arc integral
    ratio = speed / w
    yaw_t = initial_yaw + w * elapsed
    x = origin.x + ratio * (-cos(yaw_t) + cos(initial_yaw))
    z = origin.z + ratio * (sin(yaw_t) - sin(initial_yaw))
else:
    # Straight line from end of arc
    ...
```
**Boundary check:** skip torps outside the map (`|x| > space_size/2` or `|z| > space_size/2`).

### Trap 10: Timer — BattleStage is inverted

```
BattleStage "Battle"  (raw 1) = PRE-BATTLE COUNTDOWN → show countdown
BattleStage "Waiting" (raw 0) = BATTLE ACTIVE        → show elapsed/remaining
```
For elapsed time: `elapsed = clock - battle_start_clock`. `battle_start_clock` is supplied by the parser.

### Trap 11: Ship colors

| Relation | Color |
|----------|-------|
| Self | White `(255, 255, 255)` |
| Division mate | Gold `(255, 215, 0)` |
| Ally | Green `(76, 232, 170)` |
| Enemy | Red `(254, 77, 42)` |

Division mates are **not** highlighted in clan battles (the whole team is effectively one division).

### Trap 12: HP bar color thresholds

```python
if fraction > 0.66: color = (0, 255, 0)      # Green
elif fraction > 0.33: color = (255, 255, 0)  # Yellow
else: color = (255, 0, 0)                    # Red
```
Background: `(50, 50, 50)` at 70% alpha.

### Trap 13: Prefer minimap position for rendering

MinimapVisionInfo position is the authoritative source for detected ships on the minimap — not the world position from position packets. World position can be stale (last position update); MinimapVisionInfo is sent by the server specifically for the minimap.

For **trails**, use world position when available, MinimapVisionInfo as fallback.

## Design Guidelines

Visual rules for the renderer. Optimized for readability in a fixed-resolution timelapse (760px minimap reference, 20 FPS, 10–20x speed).

### Typography

Currently the renderer uses the system `sans-serif` face via Cairo's toy text API. Custom fonts (Barlow for names, JetBrains Mono for digits) would require FreeType bindings — noted as a possible future improvement, not implemented.

Every text element must have a **dark stroke halo**. Single most important readability rule. Implementation pattern:
```python
# 1. Dark stroke outline
cr.set_source_rgba(0, 0, 0, 0.9)
cr.set_line_width(3.0)
cr.set_line_join(cairo.LINE_JOIN_ROUND)
cr.move_to(tx, ty); cr.text_path(text); cr.stroke()
# 2. Fill on top
cr.set_source_rgba(r, g, b, alpha)
cr.move_to(tx, ty); cr.show_text(text)
```
Use `draw_text_halo` / `draw_cached_text` on `Layer` rather than rolling your own.

### Color Palette

| Token | Hex | Purpose |
|---|---|---|
| `sea-bg` | `#0D1520` | Water fill |
| `label-primary` | `#E8E4D9` | Player names (off-white) |
| `label-secondary` | `#9BA4AB` | Ship names (neutral — avoid dim team-colored text on dark backgrounds) |
| `friendly` | `#5DE682` | Ally team |
| `enemy` | `#FF6B6B` | Enemy team |
| `self` | `#FFFFFF` | Recording player |
| `contested` | `#FFC83C` | Contested cap (amber) |

The chosen greens and reds differ in luminance, so they remain distinguishable under deuteranopia.

**Ship name rule:** ship names use the neutral `label-secondary`, not team color. Team identity is already carried by the ship icon and player name color.

### Visual Hierarchy

Bottom-to-top: background → objectives (cap zones) → trails → ships and labels → HUD. No lower-layer element should ever obscure a higher one — the text halo enforces this.

### Capture Zones

Static zone (held):
- Fill: team color ~8% opacity
- Ring: team color ~40% opacity, 2.5px
- Label: team color, bold ~18px

Contested zone adds:
- Progress arc: invader color, 4px stroke
- Inner wedge: invader color ~12% opacity
- Outer ring: dashed amber (`#FFC83C`) ~25% opacity

Neutral zone: white fill ~4%, ring ~18%, label ~70%.

### HUD

Score bar background should fade (linear gradient from `rgba(0,0,0,0.7)` at top → transparent, ~38px tall). Score numbers use team colors, 18px; timer uses white at 60% opacity, 16px.

### Not applicable (don't bother)

These belong to interactive minimaps and add no value to a fixed-resolution timelapse video:
- Label collision avoidance / leader lines
- Zoom-dependent detail
- Label fade-in/out animations (at 20x speed, too fast to notice)
- Particle effects, bloom, glow

## Damage Breakdown

### What's implemented

**Self-player breakdown** via `DamageReceivedStatEvent` (from `receiveDamageStat` packets):
- `damage_stats.py` renders weapon-category breakdown: AP, HE, SAP, secondary, torpedo, fire, flood, rockets, bombs, depth charges, ram
- Sections: damage dealt (ENEMY), spotting damage (SPOT), potential damage (AGRO)
- 85 weapon categories available from the parser

**All-player total damage** via `DamageEvent` (from `receiveDamagesOnShip` packets):
- `team_roster.py` shows aggregate damage per player (no type breakdown)

### Limitations

Per-player typed damage breakdown for all players is **not possible** — the game protocol only sends `receiveDamageStat` (which includes ammo_id) for the recording player. Other players only get `receiveDamagesOnShip` with total damage, no type info. This is a game protocol constraint, not a parser limitation.

## WG Bounty Requirements Mapping

### Core

| Requirement | Component | Status |
|---|---|---|
| Parse replay → video | `core.py` + `video.py` | Done |
| Both teams + ship names + HP bars | `ships.py` + `health_bars.py` + `team_roster.py` | Done |
| Shells and torpedoes | `projectiles.py` | Done |
| Discord bot + user interaction | `bot/` | Done |
| Capture points + status + progress | `capture_points.py` | Done |
| Total team points | `hud.py` | Done |
| Maintained for 1 year | Per-version gamedata cache | Done (git archive extraction, no checkout) |
| Apache 2.0, WG copyright | `LICENSE` | Done |

### Nice-to-have

| Feature | Component | Status |
|---|---|---|
| Ribbons | `ribbons.py` | Done |
| Team roster side panels | `team_roster.py` | Done |
| Aircraft (CV squadrons + airstrikes) | `aircraft.py` | Done (type-specific icons from GameParams) |
| TTW + projected winner | `hud.py` | Done |
| 1 kill swing indicator | `hud.py` | Done |
| Kill feed + chat messages | `killfeed.py` | Done |
| Repair Party recoverable HP | `health_bars.py` | Done |
| Self-player damage breakdown | `damage_stats.py` | Done (via DamageReceivedStatEvent) |
| Self-player header + silhouette HP | `player_header.py` | Done |
| Division mate highlighting | `ships.py` + `team_roster.py` + `base.py` | Done |
| Clan battle clan tags | `hud.py` | Done |
| Game type in Discord message | `cog_render.py` + `worker.py` | Done |
| Per-phase timing instrumentation | `worker.py` + `core.py` + `cog_render.py` | Done |
| Per-version gamedata awareness | `gamedata_cache.py` | Done |
| Weather zone overlay | `weather.py` | Done |
| Dual perspective merged render | `render_dual.py` + `merge.py` in parser | Done |
| Per-player typed damage (all players) | — | Not possible (game protocol limitation) |

## Feature Ideas

Unscheduled ideas — kept as a reference for future work.

- **Buildings layer** — 8 building types (Airbase, AA, Artillery, Generator, Radar, Station, Supply, Tower) with relation-state icons. Parser already tracks `BuildingState` in `state.buildings`. Needs a new `BuildingLayer` + icon loading from `gui/game_map_markers/{type}_{relation}.png`.
- **Detection radius per-type coloring** — Per-type colors are defined in `consumables.py` (lines 14–22) but currently overridden by team colors at lines 162–165. Desired: use per-type colors (radar=red, hydro=teal, hydrophone=blue, sub surveillance=purple) instead of blanket team colors.
- **Team advantage scoring** — Replace "1 KILL DECIDES" with a 3-factor model: Score Projection (0–10: score gap + TTW + projected final), Fleet Power (0–10: class-weighted HP with DD=1.5, SS=1.3, CV=1.2, CL/BB=1.0), Strategic Threat (0–5: DD/SS survival + class diversity + CV advantage). Levels: Even (<1), Weak (≥1), Moderate (≥3), Strong (≥6), Absolute (≥10). Pure math, no new data deps — needs ship class + HP + cap income from existing state.
- **Per-turret direction layer** — Parser accumulates `ShipState.turret_yaws` (gun_id → yaw) from syncGun. Needs a dedicated `TurretLayer` that draws individual gun direction lines per turret, separate from the aim heading line in `ships.py`. Parser data is ready; renderer layer not started.
- **Frame dump / thumbnail** — Skip FFmpeg, write a single Cairo surface to PNG at a given timestamp. Useful for Discord embed previews. Very low effort.
- **Trail coloring** (rainbow / speed-based) — minor visual upgrade.
- **Pre-battle countdown timer** — trivial, low value.
- **Chat overlay layer** — parser has `ChatEvent`; already interleaved into killfeed, a dedicated on-map overlay is a different direction.
- **Armament color indicator** — ammo type icon tinting; needs `SetAmmoForWeapon` in the state tracker.
- **Custom fonts (Barlow + JetBrains Mono)** — requires FreeType bindings in Cairo.
- **Contested cap pulse animation** — outer ring radius oscillates ±8% over ~2.5s; ring opacity breathes between 15% and 35%.

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
| `GAMEDATA_PATH` | `wows-gamedata/data` | Path to game assets (fallback) |
| `GAMEDATA_REPO_PATH` | `wows-gamedata` | Path to wows-gamedata git repo (for version cache) |
| `GAMEDATA_CACHE_DIR` | `~/.cache/wows-gamedata` | Override cache directory |
| `MAX_WORKERS` | `2` | Concurrent render processes |
| `RENDER_MAX_TASKS_PER_CHILD` | `4` | Recycle each worker after N renders (bounds cairo/ffmpeg memory growth) |
| `RENDER_TIMEOUT` | `120` | Seconds before render is cancelled |
| `COOLDOWN_SECONDS` | `60` | Per-user rate limit |
| `MAX_UPLOAD_MB` | `50` | Max replay file size |
| `AUTHORIZED_GUILD_IDS` | *(empty)* | Comma-separated guild IDs allowed to use `/render_batch`; empty disables the command globally |

### Slash Command Flow
1. `/render` + `.wowsreplay` attachment
2. Validate extension + file size → defer interaction
3. Download to temp dir → dispatch to `ProcessPoolExecutor`
4. Poll progress queue → edit Discord message with `Rendering... X%`
5. Send mp4 with game type, match duration, render time, file size
6. Log per-phase timing breakdown (parse/render/encode/upload)
7. Cleanup temp dir

### `/render_batch` — bulk render (authorized guilds only)
Up to 10 replays in one invocation, gated by `AUTHORIZED_GUILD_IDS`.
Per-user 10-min cooldown. All replays dispatched to the shared pool
(throttled by `MAX_WORKERS`); videos stream back as individual
follow-up messages as each render completes; a final summary embed
lists per-replay status, match type, duration, and render time.
Failures (bad file, worker crash, oversize output) don't abort the
batch — they're marked ❌ in the summary and other replays continue.

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
git submodule update --init --recursive
uv venv && source .venv/bin/activate
uv sync

# Quick render (auto-resolves gamedata version from replay, caches on first run)
python render_quick.py battle.wowsreplay output.mp4

# Dual-perspective merged render (two paired replays from same match)
python render_dual.py a.wowsreplay b.wowsreplay output.mp4

# Profile render performance (per-layer timing breakdown)
python profile_frames.py battle.wowsreplay /tmp/profile.mp4

# Run Discord bot (requires .env with DISCORD_TOKEN)
python -m bot.main
# or: wows-bot

# Docker (server — see docker-compose.yml for volume mounts)
DOCKER_BUILDKIT=1 docker compose build
docker compose up -d
```

## Operations

### Liveness
The bot touches `/tmp/bot_heartbeat` from an asyncio background task every 30s
(see `bot/main.py::_heartbeat_bg`). The Docker `HEALTHCHECK` considers the
container unhealthy if the file is stale for more than 120s, which catches
event-loop hangs and silent task death — not just "python is running".

Check from the host:
```bash
docker compose ps           # STATUS column shows (healthy) / (unhealthy)
docker inspect --format '{{.State.Health.Status}}' wows-renderer-bot-1
```

### Resource limits
`docker-compose.yml` caps the bot at **4.5 GB RAM / 2 CPU cores** (4608 MiB —
docker-compose needs integer values). Cairo renders at 1080p can spike well
past 1 GB on complex matches (notably long Soviet cruiser replays tripped the
old 2 GB cap via the cgroup OOM killer), so the limit is generous. Reservation
floor is 1 GB so the scheduler won't starve the bot under load. Adjust
downward only if co-located with more containers on a smaller VPS.

`RENDER_MAX_TASKS_PER_CHILD` defaults to unset (no worker recycling). Setting
it to any positive int forces Python's `multiprocessing` to use the **spawn**
start method per the `ProcessPoolExecutor` docs — that re-imports every module
and reloads the 15 MB GameParams pickle per worker lifecycle, adding ~5-10s of
overhead per spawn on ARM. Only enable it if you've observed accumulated
per-worker memory growth that pool recovery can't handle.

### Log rotation
JSON-file driver with rolling window: **10 MB × 5 files = ~50 MB ceiling**.
Prevents disk fill from render-heavy sessions. Logs persist across restarts
until rotated out.

### Restart policy
`restart: unless-stopped` — the bot auto-restarts on crash or host reboot, but
stays down if you `docker compose stop` it explicitly.

## License

Apache 2.0 — Copyright Wargaming.net
Developed on Wargaming's request for the community.
