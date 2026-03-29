# Minimap Renderer — Design Guidelines

## Purpose

Visual design rules for the replay minimap video renderer. Optimized for readability in a fixed-resolution timelapse video (760px minimap, 20fps, 10-20x speed). Based on general minimap design principles, filtered for what matters in a non-interactive video output.

---

## 1. Typography

### Font Stack

| Role | Font | Weight | Fallback |
|---|---|---|---|
| Player names | Barlow | 500 (Medium) | sans-serif |
| Ship names | Barlow | 400 (Regular) | sans-serif |
| Zone labels (A, B, C) | Barlow Condensed | 700 (Bold) | sans-serif |
| Timer, scores | JetBrains Mono | 600 (SemiBold) | monospace |

**Status: TODO (later)** — Custom font loading requires FreeType bindings in Cairo. Currently using system `sans-serif`. The visual improvement is real (Barlow has tall x-height, open counters) and JetBrains Mono prevents digit jitter. Implement when polishing.

### Font Sizes (at 760px minimap)

| Element | Size | Current | Notes |
|---|---|---|---|
| Player name | 9px | 9px | Halo compensates, no size bump needed |
| Ship name | 7.5px | 7px | Slight bump, neutral color does the heavy lifting |
| Zone label | 18px | 14px | Bumped for visibility |
| Timer | 16px | current | Check current value |
| Score numbers | 18px | current | Check current value |

### Text Rendering — Dark Halo (CRITICAL)

**Every text element must have a dark stroke halo.** This is the single most important readability rule. The current 1px shadow offset is too weak.

Implementation in Cairo:
```python
# 1. Dark stroke outline
cr.set_source_rgba(0, 0, 0, 0.9)
cr.set_line_width(3.0)
cr.set_line_join(cairo.LINE_JOIN_ROUND)
cr.move_to(tx, ty)
cr.text_path(text)
cr.stroke()

# 2. Fill on top
cr.set_source_rgba(r, g, b, alpha)
cr.move_to(tx, ty)
cr.show_text(text)
```

**Status: TODO** — Replace all `_draw_name` / text rendering with stroke halo approach.

---

## 2. Color Palette

### Base Colors

| Token | Hex | Current | Status |
|---|---|---|---|
| `sea-bg` | `#0D1520` | `#0D1426` | Close enough, keep |
| `label-primary` | `#E8E4D9` | `#FFFFFF` | TODO: swap to off-white |
| `label-secondary` | `#9BA4AB` | team-colored | TODO: ship names must use this neutral color |
| `island-fill` | `#1E2A30` | from minimap PNG | N/A (game asset) |

### Team Colors

| Token | Hex | Current | Status |
|---|---|---|---|
| `friendly` | `#5DE682` | `(0.33, 0.85, 0.33)` = `#54D954` | TODO: swap |
| `enemy` | `#FF6B6B` | `(0.90, 0.25, 0.25)` = `#E64040` | TODO: swap |
| `self` | `#FFFFFF` | white icon | Keep |
| `contested` | `#FFC83C` | `(1.0, 0.85, 0.0)` yellow | TODO: swap to amber |

**Why these specific greens and reds?** They differ in luminance — `#5DE682` is noticeably brighter than `#FF6B6B` — so even under deuteranopia (red-green colorblindness) they remain distinguishable.

### Ship Name Color — Critical Rule

Ship names must use neutral `label-secondary` (`#9BA4AB`), NOT dim team-colored text. Team identity is already communicated by the ship icon and player name color. Dim team-colored text on dark backgrounds is the #1 readability killer.

**Status: TODO** — Change ship name rendering from team-colored to `#9BA4AB`.

---

## 3. Visual Hierarchy

Four depth layers, rendered bottom to top:

| Layer | Content | Status |
|---|---|---|
| 1. Background | Sea fill, minimap image | Done |
| 2. Objectives | Capture zones, progress arcs | Done |
| 3. Trails | Ship movement history | Done |
| 4. Ships & Labels | Icons, names, HP bars | Done (needs halo fix) |
| 5. HUD | Timer, scores, ship counts | Done (needs gradient) |

**Rule: no element from a lower layer should ever obscure a higher layer.** The text halo guarantees this.

---

## 4. Capture Zones

### Static Zone (held by a team)
```
Fill:   team color at 8% opacity        (current: 15% — reduce)
Ring:   team color at 40% opacity, 2.5px (current: 50%, 2.0px — adjust)
Label:  team color, bold 18px            (current: 14px — bump)
```

### Contested Zone (being captured)
Everything from static, plus:
```
Progress arc:   invader team color, 4px stroke    (current: done)
Inner wedge:    invader color at 12% opacity      (current: done)
Outer ring:     dashed amber (#FFC83C) at ~25%    (current: yellow dashed — swap color)
```

**TODO: Contested zone pulse animation**
```
Outer ring radius: oscillates ±8% over ~2.5s cycle (sine wave)
Ring opacity:      breathes between 15% and 35%
```
This communicates "something is happening here" without adding text. Subtle enough for peripheral vision, works well in timelapse. Want to try this.

### Neutral Zone (uncaptured)
```
Fill:   white at 4% opacity
Ring:   white at 18% opacity
Label:  white at 70% opacity
```
**Status:** Currently using gray (`0.7, 0.7, 0.7`). Close enough.

---

## 5. HUD Overlay

### Current: Hard dark bar at top
### Target: Gradient fade

```
Background: linear gradient from rgba(0,0,0,0.7) at top → transparent, ~38px tall
Scores:     team colors, 18px, monospace
Timer:      white at 60% opacity, 16px, monospace
```

**Status: TODO** — Replace solid dark background with gradient. Looks more polished and blends into the map.

---

## 6. Implementation Priority

### Phase 1 — Quick wins (do now)
1. Text halos (stroke outline on all text) — biggest readability win
2. Ship name color → neutral `#9BA4AB`
3. Team color swap → `#5DE682` / `#FF6B6B`
4. Font size bump (player name 11px, ship name 9px, zone label 18px)
5. Player name label color → off-white `#E8E4D9`

### Phase 2 — Polish
6. HUD gradient fade
7. Capture zone opacity adjustments (fill 8%, ring 40%)
8. Contested zone pulse animation (sine wave on radius/opacity)

### Phase 3 — Nice-to-have
9. Custom fonts (Barlow + JetBrains Mono) — needs FreeType/font loading
10. Cap zone letter label improvements (condensed bold font)

### Not applicable (skip)
- **Label collision / leader lines** — for interactive minimaps, not fixed video
- **Zoom-dependent detail** — fixed zoom in video
- **Label fade in/out animations** — at 20x timelapse speed, too fast to notice
- **Particle effects, bloom, glow** — no

---

## 7. Accessibility Checklist

- [ ] All text has dark stroke halo (no exceptions)
- [ ] All text meets 4.5:1 contrast ratio against background (WCAG AA)
- [ ] Team identification uses luminance difference, not just hue
- [ ] Ship names use neutral color, not dim team color
- [ ] Numerical displays use monospaced/tabular figures
- [ ] Contested zone state communicated via animation + dashed border, not only color
