# Renderer Traps (aus Landaire's wows-toolkit Analyse)

Fallstricke die den Minimap-Renderer betreffen. Aus der Analyse von `wows-toolkit/crates/minimap-renderer/src/`.

---

## Trap 1: Coordinate Mapping — space_size, nicht Game-Meter

Die Replay-Positionen sind in BigWorld Space-Units. Die Formel:
```python
scaling = 760.0 / space_size
pixel_x = round(pos_x * scaling + 380)
pixel_y = round(-pos_z * scaling + 380)  # Z-Achse invertiert
```

`space_size` pro Map kommt aus `manifest.json` / `space.settings`. Typische Werte: 800, 1000, 1200, 1400, 1600.

**NICHT verwechseln mit Game-Metern (24000, 30000, 42000, 48000).** Wenn Game-Meter als Map-Size benutzt werden, sind alle Schiffe auf einem Punkt in der Mitte geclustert. **Bounty-kritisch.**

---

## Trap 2: NormalizedPos → World → Pixel (drei Schritte)

MinimapVisionInfo-Positionen müssen ERST zu World-Koordinaten konvertiert werden, DANN durch die normale `world_to_minimap()`:

```python
# Schritt 1: Stored → Raw 11-bit
raw_x = (stored_x + 1.5) * 512.0
raw_y = (stored_y + 1.5) * 512.0

# Schritt 2: Raw → World
world_x = raw_x / 2047.0 * 5000.0 - 2500.0
world_z = raw_y / 2047.0 * 5000.0 - 2500.0

# Schritt 3: World → Pixel (gleiche Formel wie Position-Pakete)
scaling = 760.0 / space_size
pixel_x = round(world_x * scaling + 380)
pixel_y = round(-world_z * scaling + 380)
```

Wenn dieser Pfad nicht korrekt implementiert ist, stehen MinimapVisionInfo-Schiffe an komplett falschen Positionen. **Bounty-kritisch.**

---

## Trap 3: Radius-Konvertierung — Cap vs Consumable

**Cap-Punkt-Radius und Smoke-Radius sind in Space-Units:**
```python
px_radius = radius / space_size * minimap_size  # Kein / 30!
```

**Waffenreichweiten und Consumable-Radien sind in Metern:**
```python
px_radius = radius_meters / 30.0 / space_size * minimap_size
px_radius = radius_km * 1000.0 / 30.0 / space_size * minimap_size
```

Verwechseln = Faktor 30 falsch. Cap-Kreise die den halben Bildschirm füllen oder unsichtbar klein sind. **Bounty-kritisch.**

---

## Trap 4: Yaw/Heading Konvertierung

Minimap-Heading (aus `updateMinimapVisionInfo`) ist in Grad, Compass-Format (0=Nord, CW positiv). Für Screen-Rendering:
```python
screen_yaw = π/2 - heading_radians
# Oder: screen_yaw = math.pi/2 - math.radians(heading_degrees)
```

World-Yaw (aus Position-Paketen) ist ANDERS formatiert. **Bevorzuge Minimap-Heading** für Schiff-Icon-Rotation wenn verfügbar — es ist genauer.

Wenn heading falsch konvertiert wird, schauen alle Schiffe in die falsche Richtung. **Bounty-kritisch.**

---

## Trap 5: Self-Team-ID und Perspektive-Swap

Die Replay-Datei ist aus EINER Perspektive. `team_id` ist 0 oder 1, aber welches Team der Aufnehmende hat, variiert.

1. Finde den eigenen Spieler via `relation == Self` (oder `relation == 0`)
2. Lies dessen `team_id` aus dem Vehicle-Entity
3. Wenn `self_team_id == 1`: ALLES swappen

Was geswappt werden muss:
- Score-Bar: friendly links, enemy rechts
- Cap-Point-Farben: relativ zum eigenen Team
- Ship-Farben: Grün = eigenes Team, Rot = Gegner
- Team Advantage (P2)

**Ohne Swap sind Farben in ~50% der Replays falsch.** **Bounty-kritisch.**

---

## Trap 6: Detected vs Undetected Ships

Landaire rendert Schiffe unterschiedlich je nach Status:

| Status | Darstellung |
|--------|-------------|
| Detected (minimap.visible = true) | Volle Opacity, Name, HP-Bar |
| Undetected (minimap.visible = false) | 40% Opacity, kein Name, kein HP |
| Dead | X-Marker an letzter Position |

**Kritisch:** Undetected Ships werden an ihrer LETZTEN BEKANNTEN Minimap-Position gezeigt, nicht ausgeblendet. Das ist Gameplay-relevant — ein KOTS-Referee will sehen wo ein Schiff zuletzt war.

---

## Trap 7: Dead Ship Positions

Tote Schiffe brauchen ihre letzte Position. Der Renderer bekommt:
- `dead.position` — World-Pos zum Todeszeitpunkt (bevorzugt)
- `dead.minimap_position` — Minimap-Pos als Fallback
- Letzter bekannter Heading für Icon-Rotation

Wenn Dead Ships einfach verschwinden statt ein X zu zeigen, fehlt dem Referee kritische Info. **Bounty-kritisch.**

---

## Trap 8: Shell Tracer Animation

Shells werden als animierte Tracer gezeichnet, NICHT als statische Linien. Pro Frame:
```python
elapsed = current_time - fired_at
flight_duration = distance(origin, target) / speed
frac = elapsed / flight_duration

if 0 <= frac <= 1:
    head = lerp(origin, target, frac)
    tail = lerp(origin, target, max(0, frac - 0.12))  # 12% trail length
    draw_line(tail, head, team_color)
```

Ohne Flugzeit-Interpolation sieht man entweder nichts (Shells existieren nur 1 Frame) oder statische Linien die das ganze Match sichtbar sind. **Bounty-kritisch.**

---

## Trap 9: Torpedo Position Interpolation

Torpedos haben KEINEN Zielpunkt. Position wird berechnet:

**Gerade Torps:**
```python
pos = origin + direction * elapsed
```

**S-Turn Torps (maneuverDump != None):**
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
    # Gerade Linie ab Kurvenende
    ...
```

**Boundary Check:** Torpedos die außerhalb der Karte sind (|x| > space_size/2 || |z| > space_size/2) nicht zeichnen. **Bounty-kritisch.**

---

## Trap 10: Timer — BattleStage ist invertiert

```
BattleStage "Battle"  (raw 1) = PRE-BATTLE COUNTDOWN → Countdown-Timer anzeigen
BattleStage "Waiting" (raw 0) = BATTLE ACTIVE        → Elapsed/Remaining Timer
```

Für Elapsed-Time: `elapsed = clock - battle_start_clock`. Der `battle_start_clock` muss vom Parser geliefert werden. **Bounty-kritisch.**

---

## Trap 11: Ship Colors

| Relation | Farbe |
|----------|-------|
| Self | Weiß `(255, 255, 255)` |
| Division-Mate | Gold `(255, 215, 0)` |
| Ally | Grün `(76, 232, 170)` |
| Enemy | Rot `(254, 77, 42)` |

Division-Mates in **Clan Battles NICHT** markieren (ganzes Team ist eine Division). P2 Feature.

---

## Trap 12: HP Bar Farben nach Prozent

```python
if fraction > 0.66: color = (0, 255, 0)      # Grün
elif fraction > 0.33: color = (255, 255, 0)   # Gelb
else: color = (255, 0, 0)                      # Rot
```

Background: `(50, 50, 50)` mit 70% Alpha. **Bounty-relevant** weil HP-Bars ein Core Requirement sind.

---

## Trap 13: Minimap-Position für Rendering bevorzugen

Landaire nutzt MinimapVisionInfo-Position als authoritative Quelle für die Minimap-Darstellung bei detected Ships, NICHT die World-Position aus Position-Paketen. Grund: World-Position kann stale sein (letztes Position-Update), MinimapVisionInfo wird vom Server speziell für die Minimap geschickt.

Für Trails hingegen: World-Position wenn verfügbar, MinimapVisionInfo als Fallback.

---

## Zusammenfassung Renderer-Traps

| # | Trap | Impact | Bounty-kritisch? |
|---|------|--------|-------------------|
| 1 | space_size nicht Game-Meter | Alles falsch positioniert | JA |
| 2 | NormalizedPos 3-Schritt Konvertierung | MinimapVision-Pos falsch | JA |
| 3 | Radius Cap vs Consumable (÷30) | Radien Faktor 30 falsch | JA |
| 4 | Yaw/Heading Konvertierung | Schiffe falsch gedreht | JA |
| 5 | Self-Team-ID Swap | Farben 50% falsch | JA |
| 6 | Detected vs Undetected | Darstellung inkorrekt | JA |
| 7 | Dead Ship Positions | Tote verschwinden | JA |
| 8 | Shell Tracer Animation | Keine/falsche Shells | JA |
| 9 | Torpedo Interpolation + S-Turn | Torps falsch | JA |
| 10 | BattleStage invertiert | Timer kaputt | JA |
| 11 | Ship Colors | Kosmetik-Fehler | MITTEL |
| 12 | HP Bar Farben | Bounty-Requirement | JA |
| 13 | MinimapPos bevorzugen | Stale Positionen | MITTEL |
