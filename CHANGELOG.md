# Changelog

All notable changes to `wows-minimap-renderer` are documented here.

## [Unreleased]

### Added

#### Gamedata Cache System
- **Per-version gamedata cache** — isolated cache directories per game version under `~/.cache/wows-gamedata/v{build_id}/`. No `git checkout` at render time — concurrent workers can render different version replays simultaneously. Uses `git archive` for extraction.
- **GameParams.data decode + pickle caching** — `renderer/gameparams.py` decodes the binary (reverse + zlib + Python 2 pickle), caches result as standard pickle keyed by blake2b hash.
- **VersionedGamedata dataclass** — lazy `@cached_property` for ships_db, projectiles_db, ship_consumables, aircraft_icon_map, modernizations, crews. GameParams pickle loaded on first property access, not at construction.
- **Async cache population at bot boot** — `populate_all_caches()` runs as background asyncio task, pre-caching all known version tags.
- **Cold-load fallback** — `VersionedGamedata.from_gamedata_path()` decodes GameParams.data directly from a raw gamedata directory without git.

#### Consumable Enhancements
- **Consumable charge tracking** — team roster shows remaining charges per consumable for all players. Computes initial charges from GameParams with modernization + captain skill modifiers applied.
- **Time-based consumable support** — detects `lifeCycleType=1` consumables (EU BB speed boost etc.), shows remaining capacity in seconds instead of charge count.
- **Consumable state display** — white = ready (with charge count), green = active (with timer), gray = cooldown (with timer), dark = depleted.
- **In-memory consumable reload calculation** — `compute_effective_reloads_from_data()` uses pre-indexed Modernization/Crew dicts instead of scanning 762 split JSON files. TeamRosterLayer init: 7s → 2s on ARM.

#### New Features
- **Chat messages in killfeed** — `onChatMessage` events (battle_common, battle_team, battle_prebattle) displayed interleaved with kills. Sender names team-colored, team chat prefixed [T], pre-battle [P].
- **Arms Race buff zones** — buff drop icons from GameParams + BattleLogic state history
- **Weather zone overlay** — white semi-transparent circles from InteractiveZone type==5
- **Detailed per-phase timing** — resolve/parse/setup/render/encode/upload breakdown + per-layer init timings logged after each render.

#### Earlier Features
- **Aircraft icons from GameParams** — `aircraft_icons.json` maps `params_id` to correct icon (consumable fighters vs CV attack fighters)
- **Smoke puff FIFO lifecycle** — puffs expire individually instead of all at once
- **Vision-based enemy visibility** — uses vision events instead of position timestamps for accurate spotted/unspotted rendering
- **Self-player damage in roster** — uses authoritative `receiveDamageStat` for recording player's damage column
- **Division highlighting simplified** — gold player names (removed gold-tinted icons)
- **Ribbon icons from parser** — derived from parser ribbon API instead of manual mapping

### Fixed
- **CONSUMABLE_TYPE_ID_MAP mutation bug** — dict was reassigned instead of mutated in-place, causing `consumables.py` to hold a stale empty reference. Fixed with `.clear()` + `.update()`.
- **ShipConfig consumable parsing** — Exteriors section extra data (autobuy + colorSchemes) was misinterpreted as next section count, causing empty consumable lists for ~25% of players.
- Team color perspective swap in 6 layers + dead ship orientation
- Smoke puffs now expire individually (FIFO) instead of all at once

### Removed
- Dead `load_font_face()` and `get_font_path()` functions from assets.py

### Data
- **aircraft_icons.json** — params_id to icon_base mapping generated from GameParams
- **buff_drops.json** — Arms Race buff icon mapping from GameParams
- Consumable fighters distinguished from CV attack fighters in aircraft icon mapping

## [0.1.0] — 2026-04-02

### Added

#### Rendering Layers (16 total)
1. **map_bg** — water texture + minimap PNG + grid + labels (pre-rendered static cache)
2. **team_roster** — left panel with both teams: class icon, player name, ship name, kills, damage, HP bar, consumable timers
3. **capture_points** — cap circles with progress arcs, team colors, contested indicators, A-H labels
4. **smoke** — smoke screen radius circles from nested property puff positions
5. **projectiles** — shell traces colored by ammo type (AP=white, HE=orange, SAP=pink) + torpedo dots; caliber-scaled widths
6. **aircraft** — CV squadrons (controllable) + airstrikes on minimap with team-colored icons
7. **ships** — ship class icons (rotated by yaw) + player names + spotted glow + division mate gold names
8. **health_bars** — per-ship HP bars (green/yellow/red) + repair party recoverable segment
9. **consumables** — consumable icons near ships + radar/hydro detection radius circles
10. **player_header** — right panel: self-player header with ship silhouette HP bar, clan tag + name
11. **damage_stats** — right panel: self-player damage breakdown by weapon type (AP/HE/SAP/torp/fire/flood/secondary)
12. **ribbons** — right panel: recording player ribbon counters, grouped layout, accumulating per frame
13. **killfeed** — right panel: recent kills with frag icons, bottom-anchored growing upward
14. **right_panel** — composite: player_header + damage_stats + ribbons + killfeed with clipping
15. **hud** — score bar with projected winner, timer, TTW pills, 1-kill-swing indicator, match result, clan battle clan tags
16. **trails** — fading ship movement trails (pre-sampled, gap detection)

#### Core Features
- **Cairo-based rendering** — all layers draw on a shared cairo.Context, no compositing step
- **Async frame writer** — pipe I/O offloaded to background thread, queue size 16
- **FFmpeg fast preset** — 3x smaller output vs ultrafast (~5MB vs 16MB typical)
- **Static background cache** — map_bg renders once, single `cr.paint()` per frame
- **Text surface cache** — `draw_cached_text()` renders once, blits via `cr.paint()`
- **~60 fps** rendering at 1920x1104 (~17ms/frame average)
- **Index-based timestamps** — avoids float accumulation drift

#### Ship Display
- All player positions with team colors (green=ally, red=enemy, white=self)
- Undetected enemies at 40% alpha
- Dead ships shown with sunk icon variant
- Ship class icons rotated by yaw (28x28 RGBA)

#### Discord Bot
- `/render` slash command with `.wowsreplay` attachment upload
- ProcessPoolExecutor for CPU-bound rendering (bypasses GIL)
- Cross-process progress reporting via `Manager().Queue()`
- Per-phase timing instrumentation (parse/render/encode/upload)
- Game type + game version in render message
- Per-user rate limiting, file size validation, deadline-based timeout
- Docker + docker-compose deployment

#### Data Pipeline
- `ships.json` — compact ship lookup (shipId -> name, species, nation, level)
- `ship_names.json` — display names from global.mo localization
- `ship_consumables.json` — per-ship consumable loadouts, detection ranges, reload timings
- `projectiles.json` — projectile params_id to ammo_type/caliber mapping
- `map_sizes.json` — space_size per map for coordinate transforms
- `decode_gameparams.py` — GameParams.data decoder (binary: reverse bytes -> zlib -> pickle)

#### HUD Features
- Score bar with projected winner highlight
- MM:SS countdown timer
- TTW (Time To Win) pills with diamond icons
- "1 KILL DECIDES" indicator with team-colored glow
- Match result overlay (Victory/Defeat/Draw)
- Clan battle clan tags below score bar (majority clan >= 4 players, clan colors)

#### Other
- Division mate highlighting (gold names on minimap + roster, disabled in clan battles)
- Game type display (RandomBattle, ClanBattle, CooperativeBattle, etc.)
- Self-player typed damage breakdown via DamageReceivedStatEvent
- RenderConfig validation (fps, speed, crf, sizes) + str-to-Path coercion
- Configurable team colors, self color, division color, trail length, HUD height

### Fixed
- Team swap perspective (self-team always green)
- Enemy ship icons rotated 180 degrees after SVG switch
- Stale capture point state at battle start
- False capture progress arc on pre-owned zones
- Player header text overlap and clipping
- Consumable cooldown using `compute_effective_reloads` from parser
- `ship_consumables.json` includes all slot options, not just first
