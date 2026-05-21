# Achievement Overlay Under Ribbons — Design

**Date:** 2026-05-21
**Status:** Design approved pending user review

## Goal

Display the recording player's earned achievements as a row of icons on the
right panel, directly below the existing ribbon counter. New icons append as
the player earns them and remain visible for the rest of the match. The row
takes no vertical space until the first achievement is earned; once at least
one icon is present, the achievement row pushes the killfeed's available area
upward (killfeed is bottom-anchored, so this manifests as fewer visible feed
lines, never as overlap-into-ribbons).

This mirrors how ribbons are already accumulated and displayed
([wows-renderer/renderer/layers/ribbons.py:84](wows-renderer/renderer/layers/ribbons.py#L84)),
applied to achievement events instead.

## Non-goals

- No achievements for non-recording players. `Avatar.onAchievementEarned`
  only fires reliably for the recording player.
- No animation, fade-in/out, or toast popup. Persistent accumulator only.
- No name labels, no tooltips, no localization lookup (`global.mo`).
- No count badges. Achievements are unique per battle; if a duplicate event
  arrives we de-duplicate by `achievement_id`.
- No CLI changes. No parser API changes. The parser already exposes
  `AchievementEvent` via `replay.events`
  ([wows-replay-parser/src/wows_replay_parser/events/models.py:250](wows-replay-parser/src/wows_replay_parser/events/models.py#L250)).
- No changes to `render_dual.py`. Dual perspective stays self-layer-free
  (no `RightPanelLayer`, hence no achievements layer).

## User-visible behaviour

- Before any achievement is earned: right panel looks identical to today.
- Frame on which the first `AchievementEvent` for the recording player
  fires: a 24px-scaled icon appears below the last ribbon row, left-aligned
  to the ribbon column, with 6px padding above.
- Each subsequent unique achievement appends to the right in the same row.
- When the row hits the right-panel edge, it wraps to a new row 3px below
  (same gap as ribbons).
- An achievement icon, once shown, never disappears for the rest of the
  render.
- Unknown `achievement_id` (no GameParams entry, or missing icon file)
  falls back to `gui/achievements/default.png` so the slot is still drawn.
  This keeps the row stable across patch shifts without silently dropping
  data.

## Architecture

### New layer: `AchievementLayer`

File: `wows-renderer/renderer/layers/achievements.py`. Subclass of `Layer`,
follows the same shape as `RibbonLayer`:

- **`initialize(ctx)`**:
  - Build `_timeline: list[tuple[float, int]]` from
    `ctx.replay.events_of_type(AchievementEvent)`, filtered to events where
    `event.player_id == own_account_id`. Resolve `own_account_id` from
    `ctx.player_lookup`: find the `PlayerInfo` whose `relation == 0`
    (self) and use its `account_id`.
  - Load `id → ui_name` map from `ctx.versioned_gamedata.achievements`
    (new property, see below).
  - For each unique `ui_name`, try to load
    `<gamedata>/gui/achievements/icon_achievement_<UI_NAME>.png`. Cache
    the loaded `cairo.ImageSurface` in `self._icons: dict[int, ImageSurface]`
    keyed by achievement id.
  - Load fallback `gui/achievements/default.png` into
    `self._default_icon: ImageSurface | None`.
- **`render(cr, state, timestamp)`**:
  - Walk `_timeline` forward from `_tl_idx`. For each entry with
    `t <= timestamp`, if `achievement_id not in self._seen_order`,
    append it. Advance `_tl_idx`.
  - If `self._seen_order` is empty, return early — zero vertical space
    consumed, `panel_bottom` left at the inherited y_start (read from
    `_ribbons_ref.panel_bottom`) so downstream layers see no growth.
  - Resolve `y_start` from `_ribbons_ref.panel_bottom + 6 * scale`. If
    `_ribbons_ref` is missing or its `panel_bottom == 0`, fall back to
    `config.hud_height + self.Y_START * scale`.
  - Set up the same clip rect as `RibbonLayer` (right panel column).
  - For each achievement id in `_seen_order`, draw the icon scaled to
    height `MAIN_HEIGHT * scale`, left-to-right, wrapping on
    panel-width overflow with the same gap rules as `RibbonLayer`.
  - Track the bottom Y of the last drawn row, store as `self.panel_bottom`.
- **Constants:** `MAIN_HEIGHT = 24`, `GAP = 3`, `Y_PAD = 6`. Matches
  `RibbonLayer` for visual continuity.

### `RibbonLayer.panel_bottom`

`RibbonLayer` currently doesn't expose `panel_bottom`
([wows-renderer/renderer/layers/ribbons.py:116-201](wows-renderer/renderer/layers/ribbons.py#L116-L201)).
Add a `panel_bottom: float = 0.0` field and, at the end of `render`, set
`self.panel_bottom = y_row + row_max_h` (the bottom of the last drawn
ribbon row). When there are no ribbons yet, set `panel_bottom` to the
computed `y_start` so achievements still anchor correctly under the empty
ribbon slot. Mirrors the `panel_bottom` pattern already used by
`PlayerHeaderLayer` and `DamageStatsLayer`
([wows-renderer/renderer/layers/damage_stats.py:79](wows-renderer/renderer/layers/damage_stats.py#L79),
[wows-renderer/renderer/layers/player_header.py:52](wows-renderer/renderer/layers/player_header.py#L52)).

### `RightPanelLayer` wiring

In `wows-renderer/renderer/layers/right_panel.py`:

1. Add constructor flag `show_achievements: bool = True`.
2. Instantiate `self._achievements = AchievementLayer() if show_achievements
   else None`.
3. Wire `self._achievements._ribbons_ref = self._ribbons` (only if both
   exist), following the existing `_dmg_stats_ref` pattern.
4. Add to `_sub_layers()` (so `initialize` is forwarded).
5. Render between ribbons and killfeed inside the existing clipped block.

No new clip layer needed — `RightPanelLayer` already clips to the right
panel rect for all sub-layers.

### Gamedata cache: `achievements.json`

Achievement IDs in `AchievementEvent.achievement_id` come from
`onAchievementEarned` (`UINT32`) and match the `id` field on GameParams
entries of `typeinfo.type == "Achievement"`. **Empirically verified**
against the cached v12506899 GameParams: 426 Achievement entries, ids in
u32 range `[3254969264, 4293059504]`. Entry names are `PCH<n>_<TitleCase>`
(e.g. `PCH034_ScienceOfWinning1`) — NOT `Achievement_<UI_NAME>`. The icon
filename suffix lives in the entry's `uiName` field (e.g.
`uiName='SCIENCE_OF_WINNING_ARSONIST'` → `icon_achievement_SCIENCE_OF_WINNING_ARSONIST.png`).
98.4% of entries match an existing icon by this rule.

Mirror the existing `aircraft_icons.json` extraction pattern in
`wows-renderer/renderer/gamedata_cache.py`:

1. Add `_extract_achievement_map(gp: dict) -> dict[str, str]`:
   ```
   for name, obj in gp.items():
       ti = obj.get("typeinfo")
       if ti and ti.get("type") == "Achievement":
           aid = obj.get("id")
           ui_name = obj.get("uiName")
           if aid is not None and ui_name:
               result[str(aid)] = ui_name
   ```
   Entries missing `uiName` (2/426 in v12506899) are skipped — they have
   no icon to render. Entries whose `uiName` doesn't match an on-disk icon
   file (≤2% on current data) fall back to `default.png` at render time.
2. In `ensure_version_cache`, after `ship_consumables.json` is written
   (~line 645), write `achievements.json`:
   ```
   achievements = _extract_achievement_map(gp)
   (data_dir / "achievements.json").write_text(
       json.dumps(achievements, separators=(",", ":"))
   )
   ```
3. On `VersionedGamedata`, add:
   ```python
   @cached_property
   def achievements(self) -> dict[int, str]:
       """Achievement id → ui_name (icon filename suffix)."""
       path = self.version_dir / "data" / "achievements.json"
       if not path.exists():
           return {}
       raw = json.loads(path.read_text())
       return {int(k): v for k, v in raw.items()}
   ```

Existing version caches under `~/.cache/wows-gamedata/v*/` will be rebuilt
on next render for that version, because the missing `achievements.json`
triggers no rebuild on its own — but it does mean the achievement layer
will silently render zero icons on old cache versions until the cache is
rebuilt. **Mitigation:** bump a cache-format sentinel, OR add a one-shot
backfill in `VersionedGamedata.achievements` that generates the JSON from
the lazy-loaded GameParams pickle on first miss and writes it next to the
pickle. The backfill is simpler and avoids invalidating every cached
version on disk; we'll go with backfill.

### `RenderContext.versioned_gamedata`

`SingleRenderContext` already carries `config.versioned_gamedata` via
`config: RenderConfig`
([wows-renderer/renderer/config.py — `versioned_gamedata` field](wows-renderer/renderer/config.py)).
`AchievementLayer.initialize` reads it as
`ctx.config.versioned_gamedata.achievements`. If `versioned_gamedata` is
`None` (cold-load path without git resolution), the layer logs once and
disables itself — `_timeline` stays empty.

## Data flow

```
.wowsreplay
   |
   v
parse_replay()
   |
   |  Avatar.onAchievementEarned → AchievementEvent(player_id, achievement_id)
   |
   v
ParsedReplay.events
   |
   v
AchievementLayer.initialize:
   - filter events by player_id == own_account_id
   - de-dupe + first-appearance order via _seen_order
   - resolve id → ui_name from versioned_gamedata.achievements
   - load icon PNGs (with default.png fallback)
   |
   v
AchievementLayer.render(t):
   - advance _tl_idx, append new ids to _seen_order
   - if no ids: return (no vertical growth)
   - else: anchor under _ribbons_ref.panel_bottom + 6*s
   - draw icons left→right with wrap, set panel_bottom
   |
   v
KillfeedLayer (bottom-anchored, unchanged)
```

## Edge cases

- **Replay with no achievements:** `_timeline` empty; layer renders
  nothing every frame; `panel_bottom` reported as inherited y_start so
  no downstream layer sees a phantom growth.
- **Recording player not found in `player_lookup`:** rare but possible
  for malformed replays. Log warning, set `_timeline = []`, no rows.
- **Achievement id has no GameParams entry** (new patch, old cache):
  use `default.png`. The backfill will eventually generate a complete
  `achievements.json` on first GameParams load.
- **`gui/achievements/icon_achievement_<NAME>.png` missing on disk:**
  use `default.png`. Log at debug level — not an error.
- **`default.png` itself missing:** drop that icon's slot
  (`self._icons[id]` stays unset, render-loop skips it). Should never
  happen — verified present in 421-file gamedata snapshot.
- **Duplicate event for the same `achievement_id`:** de-duplicated by
  `if achievement_id not in self._seen_order`.
- **Row wraps past `_killfeed`'s top:** killfeed is bottom-anchored and
  shrinks visible-line count when the panel runs out of space — no
  layout fix needed beyond what the existing layer already does. If we
  ever see overlap in practice, the fix is to teach `KillfeedLayer` to
  clamp `y_start >= achievements.panel_bottom + 4 * s`; out of scope for
  this design but called out for the implementation plan to keep in mind.
- **Dual-perspective renders:** `render_dual.py` doesn't add a
  `RightPanelLayer`, so achievements are correctly excluded automatically.

## Testing

Functional checks (manual, during implementation):

1. Render a replay where the recording player earned at least 3 achievements
   — confirm icons appear under ribbons, in chronological order, at the
   correct timestamps.
2. Render a replay where the player earned **zero** achievements — confirm
   the right panel is visually identical to the pre-change baseline.
3. Render a replay containing an `achievement_id` not in `achievements.json`
   — confirm `default.png` is shown.
4. Quick visual regression on the existing `render_dual.py` paired-replay
   case — confirm no right-panel rendering at all (no spurious achievements).

Unit-level checks worth adding:

- `_extract_achievement_map` on a tiny synthetic GameParams dict — verify
  id-key + name-prefix-strip handling.
- `AchievementLayer.initialize` with a `ParsedReplay` stub: confirms
  player-id filtering and de-duplication. No Cairo dependency for this
  test (icons set to empty dict).

## Risks

- **Icon filename convention** — verified empirically against
  v12506899: 98.4% icon match using the entry's `uiName` field. The
  remaining 1.6% (5 entries: SHADOW, SILENT_KILLER, DEFAULT,
  OBT_PARTICIPANT, UNHARMED) appear to be retired achievements with no
  on-disk icon — they fall back to `default.png`.
- **No coverage for other Achievement-emitting paths.** The
  `AchievementsComponent.def` on `Account` defines dev/admin methods
  (`dev_earnAchievement`, `sendAchievements`) — not used in production
  replays. `Avatar.onAchievementEarned` is the only wire source.
- **Right-panel vertical budget** is finite. On replays with 8+
  achievements + dense ribbons, the achievement row may wrap to 2-3
  rows and reduce visible killfeed lines noticeably. Acceptable per
  user decision; revisit only if it becomes a complaint.
