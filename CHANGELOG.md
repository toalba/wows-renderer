# Changelog

All notable changes to `wows-minimap-renderer` are documented here.

## [Unreleased]

### Added

#### HTTP Render API + Cloudflare Tunnel
- **`bot/api.py`** — aiohttp render API served from the bot process: `POST /v1/jobs` (multipart replay + options), `GET /v1/jobs/{id}` (state/progress/result metadata), `GET /v1/jobs/{id}/result` (mp4 or png), `GET /healthz`. Every route but `/healthz` requires `Authorization: Bearer $API_TOKEN`, compared with `hmac.compare_digest`. Unset `API_TOKEN` means the server never starts — there is no unauthenticated mode.
- **Jobs are asynchronous on purpose.** Cloudflare's edge aborts a proxied request that hasn't produced its first byte in ~100s (error 524) on non-Enterprise plans, while renders routinely take longer (prod `RENDER_TIMEOUT=300`). Submit → poll → download keeps every response prompt; only the artifact download moves bulk bytes, and transfer duration is unbounded.
- **`bot/jobs.py`** — `Job` + `JobRegistry`: per-job temp dir, runner task that polls the worker's progress queue every 2s (both `("status", str)` and `(current, total)` wire shapes), deadline enforcement, and a TTL sweeper that drops finished artifacts after `API_RESULT_TTL`. HTTP-free, so the whole lifecycle is unit-tested without a client or a real pool.
- **`bot/render_service.py`** — the pool, pool lock and progress-queue `Manager` extracted from `RenderCog` so the cog and the API share **one** `ProcessPoolExecutor`. `MAX_WORKERS` is sized against the host's 4 vCPU / 4.5 GB cap, so a second pool would oversubscribe it; sharing also keeps `wows_renders_in_flight` meaningful. The forkserver rationale moved verbatim with it.
- **`bot/worker.py::render_stats`** — third pool job: parse + `extract_match_stats` + `render_stats_board` → PNG, drawn worker-side rather than on the bot's event loop. Unlike the `/render` path, where stats are garnish that degrades to a missing button, failures propagate: a replay with no post-battle results packet raises the new `StatsUnavailableError` and the client is told exactly that. Theme and layout are validated before any parsing, turning a `KeyError` from deep inside a draw call into an immediate, message-bearing rejection.
- **`KNOWN_FLAGS` / `parse_flags` moved to `bot/worker.py`** (beside `PRESETS`), so the API validates flags without importing discord. Unknown flags are dropped exactly as they are for the slash commands.
- **`cloudflared` sidecar** in `docker-compose.yml` running a remotely-managed tunnel; the hostname → service mapping lives in the Cloudflare dashboard (`http://bot:8080`), so changing the hostname needs no redeploy. The API port uses `expose:`, never `ports:` — the tunnel plus the bearer token are the only ingress. Gated behind a `tunnel` compose profile so deployments that don't want a public endpoint aren't left with a container looping on an empty token; set `COMPOSE_PROFILES=tunnel` in `.env` to make it part of the default stack.
- **Successful jobs drop their uploaded replays** as soon as the artifact exists, instead of holding both for `API_RESULT_TTL` — otherwise a steady stream of jobs occupies roughly three times the disk, and submitted replays linger longer than they need to.
- **New metric label values** `api_render`, `api_dual`, `api_stats` on the existing `command` dimension, so API renders show up in the Grafana dashboard alongside Discord ones. Same recording contract as the cog: `record_render` when the future resolves, `record_delivery` afterwards, exactly one `finish_render`.
- **`API_TOKEN` / `API_PORT` / `API_MAX_PENDING` / `API_RESULT_TTL` / `CLOUDFLARE_TUNNEL_TOKEN`** config, with `API_TOKEN` rejected below 16 characters — it is the only credential in front of a publicly tunnelled render endpoint.
- **A busy API port disables the API, it does not stop the bot.** `_start_api` runs inside `setup_hook`, where a raised exception aborts startup entirely, so it follows `start_metrics_server`'s rule and returns `False` after logging instead.
- **Every multipart part is size-bounded, including the non-file ones.** `client_max_size` is only applied by `Request.read()`/`.post()`, never to parts pulled off `request.multipart()`, so a text field went through `part.text()` and buffered without limit: a 200 MB `theme` field grew the process by ~200 MB, and OOMing it also kills the Discord bot it shares. Option fields are now capped at 4 KB (`413`), verified at 2 MB growth for the same 200 MB payload.
- **Uploads are written under server-chosen names.** Deriving both on-disk names from client input allowed a collision — `replay` named `b_x.wowsreplay` plus `replay_b` named `x.wowsreplay` landed on the same path — so a `render_dual` job silently merged one perspective with itself and still returned `202`.
- **Artifact names are reduced to `[A-Za-z0-9._-]`, length-capped, with a fallback.** The upload's name reaches a `Content-Disposition` header; aiohttp's client sanitizes CRLF and quotes but a hand-rolled one need not, so the server no longer trusts it. Real replay filenames pass through unchanged.
- **A failure between job creation and job start no longer leaks a pending slot.** Such a job stayed `queued` forever — counted against `API_MAX_PENDING`, but with no `finished_at` for the sweeper to expire — so repeated `500`s would wedge submissions at `429` until restart.
- **`aiohttp` is now a declared dependency.** It was already installed transitively via discord.py; the API imports it directly, so it no longer relies on that.
- Known limitation, accepted: the job registry is in memory, so restarting the bot drops queued jobs and any result not yet downloaded.

#### Prometheus metrics + Grafana dashboard
- **`bot/metrics.py`** — the per-phase `timings` dict that previously only reached a `[TIMING]` log line is now also exported as Prometheus metrics: render counts by command/preset/outcome, per-phase latency histograms (resolve/parse/setup/render/encode/upload), end-to-end duration, frames encoded, output size, per-layer init cost, worker peak RSS, pool rebuilds, gamedata cache populates, and event-loop lag.
- **`/metrics` endpoint** on port 9108 (`METRICS_PORT`, disable with `METRICS_ENABLED=false`), served from a daemon thread in the bot process. No `prometheus_client` multiprocess mode: render workers return their timings to the parent, so a plain single-process registry is correct — and `fork`/`spawn` never carry the listening socket into a worker.
- **`monitoring/`** — Prometheus scrape config plus a provisioned Grafana dashboard (14 panels: throughput by outcome, phase p95, in-flight vs `MAX_WORKERS`, worker RSS vs the 4.5 GB cap, output size vs Discord's 25 MB limit, loop lag, slowest layers, derived queue wait).
- **`prometheus` + `grafana` services** in `docker-compose.yml`, capped at 512 MB / 256 MB. Prometheus is unpublished; Grafana binds to `127.0.0.1:3000` only (reach it over an SSH tunnel). Set `GRAFANA_ADMIN_PASSWORD` before exposing it anywhere.
- **`oversize` and `upload_failed` render outcomes** — a render that succeeds but exceeds Discord's attachment limit, or that fails to reach Discord at all, is counted separately from `success`, since the user receives no video either way. `upload_failed` is load-bearing: a hung upload raises `TimeoutError`, the same exception type as the render deadline, so without it a Discord outage would be recorded as renderer timeouts.
- **Phase timings are recorded before delivery is attempted**, so a render that completed is never erased from the histograms by a subsequent upload failure. End-to-end is measured *after* the upload and therefore includes it, which is what makes the derived queue-wait panel (e2e minus the phase sum) correct rather than negative.
- **Phase histogram buckets tuned against measured renders**, not guesses — dense at 0.1-1s (encode lands at 0.2-0.7s; with only 0.25/1.0 around it, 44 of 47 real samples fell in one bucket and p99 reported 0.99s against an observed max of 0.7s) and at 15-120s (where the render phase lives).
- **Worker peak RSS** reported through the `timings` dict (`_peak_rss_bytes`), leaving the worker's 8-tuple return contract unchanged. Note `ru_maxrss` is a per-process high-water mark, so with worker recycling disabled it spans a worker's whole lifetime, not one render.
- **`_loop_lag_bg`** — 1s event-loop responsiveness sampling, complementing the 30s liveness heartbeat which only catches a fully wedged loop.

#### Statistics Board
- **Statistics button** on render results — posts a post-battle stats board
  covering every player in the match, rendered from the `0x22` BattleResults
  packet. Hidden when a replay ends before that packet arrives. Respects the
  `anonymize` flag and the theme dropdown.
- **Two board layouts.** *Compact* (default) is 11 columns plus a per-player
  ribbon strip drawn with the game's own ribbon art. On the same match it is
  1656px against 1967px detailed at 14 players (311px saved), and 2003px
  against 2077px at 24 (74px). The width saving shrinks with player count —
  the point is density and legibility, not width. *Detailed* keeps all 29
  numeric columns and no ribbons; it needs no gamedata, so it is also the
  automatic fallback whenever ribbons can't be drawn: the replay's build
  predates 15.3 (results rows are 503 elements, and the parser's
  `ribbon_counts()` has no bounds guard of its own), or no icon loaded at
  all. A single corrupt icon among good ones is skipped, not a fallback.
- Ribbon icon paths are resolved worker-side and shipped on `MatchStats` as
  plain filesystem paths, so `stats_board.py` keeps its no-parser,
  no-gamedata import boundary while still drawing real ribbon art.

### Changed
- **`/render_dual` is now available on every server** — removed the `AUTHORIZED_GUILD_IDS` gate. A dedicated 10-min per-user cooldown now applies on all guilds (the previously shared `_batch_cooldown` only rate-limited authorized guilds, which would have left dual renders uncapped everywhere else). `/render_batch` remains gated.
- Render workers return a `RenderResult` dataclass instead of an 8-tuple.
- Statistics board `Killed by` shows the killer's name without the weapon
  label, which had made it the widest column on the sheet. The consequence
  is that a fire/flood/terrain death renders `—` like a survivor, separated
  only by the HP column.
- Statistics board `Caps`/`Rst` read `cp_capture_points` /
  `cp_dropped_points`. The previously-used `capture_points` /
  `dropped_capture_points` are dead in the schema — zero for every player on
  both build 15.2 and 15.6 — so those two columns were always blank.

## [0.3.0] — 2026-05-21

### Added

#### Achievement Overlay
- **AchievementLayer** — recording player's earned achievements render as persistent icons directly under the ribbon block on the right panel. Icons appear in first-appearance order, accumulate for the rest of the match, and wrap to additional rows when the panel runs out of width. Row consumes zero vertical space until the first achievement is earned. Filters to the recording player by `relation == 0 → account_id`. Unknown achievement IDs fall back to `gui/achievements/default.png`.
- **achievements.json gamedata extraction** — new GameParams extractor maps achievement `id → uiName` (the icon-filename suffix). Written into the per-version cache during `ensure_version_cache`. `VersionedGamedata.achievements` cached_property reads it; old caches that pre-date the feature backfill on first read and write the JSON for next time, with an INFO log so the latency has a paper trail.
- **`RibbonLayer.panel_bottom`** — exposed so the new layer can anchor under it, mirroring the same pattern used by `PlayerHeaderLayer` / `DamageStatsLayer`. Set in all three render exit paths.
- **`RightPanelLayer(show_achievements=True)`** — new constructor flag; `AchievementLayer` wires both `_ribbons_ref` and `_dmg_stats_ref` so it stays anchored even when ribbons or damage stats are disabled.

#### Other Renderer Improvements
- **`/render flags` slash-command param** — anonymize toggle for the rendered video (hides player names/identifiers on request).
- **Patrol-fighter zones** — distinct render in `capture_points.py` (visually separated from regular cap zones).
- **Attribution watermark** — Rias_prpr credit watermark in the bottom-right corner.
- **Right-panel text wrapping** — kill feed and chat entries wrap to fit the panel width instead of overflowing.
- **`DamageStatsLayer.panel_bottom`** — propagated even when there's no data to render, so downstream layers (ribbons, achievements) anchor correctly.

#### Dependencies
- Bump Python base image: 3.12-slim → 3.14-slim (#10).
- Bump cairosvg ≥2.9.0 (#12), pillow ≥12.2.0 (#14), click ≥8.3.3 (#11), ruff ≥0.15.12 (#15).

### Fixed
- **`AchievementLayer` fallback when `show_ribbons=False`** — previously anchored at `hud_height + 10` and would overlap the damage-stats widget. Now chains through `_dmg_stats_ref` the same way ribbons does.

### Docs
- **CLAUDE.md** — updated layer count from 16 → 18, added `achievements.py` to the file tree, numbered list, dual-perspective exclusion list, and the Layer System code example.
- **Spec + plan** — `docs/superpowers/specs/2026-05-21-achievement-overlay-design.md` and `docs/superpowers/plans/2026-05-21-achievement-overlay.md` document the feature's design, implementation tasks, and the empirical verification that the icon-filename suffix lives in each GameParams Achievement entry's `uiName` field (98.4% on-disk match).

## [0.2.0] — 2026-05-04

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
