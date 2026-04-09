from __future__ import annotations

import math
import cairo

from renderer.assets import CONSUMABLE_TYPE_ID_MAP, CONSUMABLE_TYPE_TO_ICONS, CONSUMABLE_TYPE_TO_CATEGORY
from renderer.layers.base import Layer, RenderContext, FONT_FAMILY, _font_for_text

_SPECIES_TO_ICON: dict[str, str] = {
    "Destroyer": "destroyer",
    "Cruiser": "cruiser",
    "Battleship": "battleship",
    "AirCarrier": "aircarrier",
    "Submarine": "submarine",
    "Auxiliary": "auxiliary",
}

# Consumable display order (most important first)
_CONS_ORDER = [
    "crashCrew", "rls", "sonar", "hydrophone", "smokeGenerator",
    "speedBoosters", "artilleryBoosters", "scout", "airDefenseDisp",
    "torpedoReloader", "hangarBooster", "submarineLocator",
]


class TeamRosterLayer(Layer):
    """Draws both team rosters in the left panel.

    Each row: class icon | player name / ship name | dmg / kills | HP bar
                         consumable icons with active timer / cooldown below
    """

    HEADER_HEIGHT = 18
    ROW_HEIGHT = 50          # taller to fit consumable line
    ICON_SIZE = 18
    NAME_FONT_SIZE = 13.0
    SHIP_FONT_SIZE = 11.0
    STAT_FONT_SIZE = 13.0
    CONS_ICON_SIZE = 13
    CONS_FONT_SIZE = 10.0
    HP_BAR_WIDTH = 60
    HP_BAR_HEIGHT = 4
    PAD_X = 8

    def initialize(self, ctx: RenderContext) -> None:
        super().initialize(ctx)

        # Load damage widget icons
        gp = ctx.config.effective_gamedata_path
        icon_dir = gp / "gui" / "battle_hud" / "damage_widget"
        self._stat_icons: dict[str, cairo.ImageSurface] = {}
        for key, filename in [
            ("damage",   "icon_counter_caused_damage"),
            ("assisted", "icon_counter_assisted_damage"),
            ("blocked",  "icon_counter_blocked_damage"),
            ("aviation", "icon_counter_caused_avia_damage"),
        ]:
            path = icon_dir / f"{filename}.png"
            if path.exists():
                try:
                    self._stat_icons[key] = cairo.ImageSurface.create_from_png(str(path))
                except Exception:
                    pass

        # Load frags (kills) icon
        frags_path = gp / "gui" / "fla" / "battle_loading" / "frags.png"
        if frags_path.exists():
            try:
                self._stat_icons["frags"] = cairo.ImageSurface.create_from_png(str(frags_path))
            except Exception:
                pass

        # Load consumable icons (same logic as ConsumableLayer)
        from renderer.assets import load_consumable_icons
        all_icons = load_consumable_icons(gp)
        self._cons_icons: dict[int, cairo.ImageSurface] = {}  # cons_id → icon
        for type_id, type_name in CONSUMABLE_TYPE_ID_MAP.items():
            candidates = CONSUMABLE_TYPE_TO_ICONS.get(type_name, [])
            for icon_name in candidates:
                if icon_name in all_icons:
                    self._cons_icons[type_id] = all_icons[icon_name]
                    break

        # Build per-entity effective reload + initial charges lookup
        from wows_replay_parser.consumable_calc import SPECIES_INDEX
        ship_db = ctx.ship_db or {}
        tracker = ctx.replay.tracker
        vgd = ctx.config.versioned_gamedata

        self._entity_reload: dict[int, dict[int, float]] = {}  # entity_id → {cons_id: reload_s}
        self._entity_charges: dict[int, dict] = {}  # entity_id → {cons_id: ConsumableChargeInfo}
        for entity_id, player in ctx.player_lookup.items():
            if not player.ship_id or not player.ship_config:
                continue
            species = ship_db.get(player.ship_id, {}).get("species", "")
            species_idx = SPECIES_INDEX.get(species, -1)

            # Get learned skills from crewModifiersCompactParams
            learned: list[int] = []
            if tracker and species_idx >= 0:
                crew_props = tracker.get_entity_props(entity_id).get("crewModifiersCompactParams")
                if crew_props:
                    ls = getattr(crew_props, "learnedSkills", None)
                    if ls and species_idx < len(ls):
                        learned = list(ls[species_idx])

            if vgd is not None:
                from wows_replay_parser.consumable_calc import (
                    compute_effective_reloads_from_data,
                    compute_initial_charges_from_data,
                )
                reloads = compute_effective_reloads_from_data(
                    ship_consumables=vgd.ship_consumables,
                    modernizations=vgd.modernizations,
                    crews=vgd.crews,
                    ship_id=player.ship_id,
                    ship_species=species,
                    modernization_ids=player.ship_config.modernizations,
                    exterior_ids=player.ship_config.exteriors,
                    learned_skill_ids=learned,
                    crew_id=player.crew_id,
                )
                charges = compute_initial_charges_from_data(
                    gameparams=vgd.gameparams,
                    modernizations=vgd.modernizations,
                    crews=vgd.crews,
                    ship_id=player.ship_id,
                    consumable_ids=player.ship_config.consumables,
                    modernization_ids=player.ship_config.modernizations,
                    learned_skill_ids=learned,
                    crew_id=player.crew_id,
                )
                self._entity_charges[entity_id] = charges
            else:
                from wows_replay_parser.consumable_calc import compute_effective_reloads
                reloads = compute_effective_reloads(
                    ship_id=player.ship_id,
                    ship_species=species,
                    modernization_ids=player.ship_config.modernizations,
                    exterior_ids=player.ship_config.exteriors,
                    learned_skill_ids=learned,
                    crew_id=player.crew_id,
                    gamedata_path=gp / "scripts_entity" / "entity_defs",
                )
            if reloads:
                self._entity_reload[entity_id] = reloads

        # Build per-entity consumable timeline with cooldowns.
        # For consecutive uses: cooldown_end = next activation time.
        # For last use: cooldown_end = active_end + effective reload.
        self._cons_timeline: dict[int, list[tuple[float, int, float, float]]] = {}
        for entity_id in ctx.player_lookup:
            acts = tracker.get_consumable_activations(entity_id) if tracker else []
            by_cons: dict[int, list[tuple[float, float]]] = {}
            for activated_at, cons_id, duration in acts:
                by_cons.setdefault(cons_id, []).append((activated_at, duration))

            entity_reloads = self._entity_reload.get(entity_id, {})
            timeline: list[tuple[float, int, float, float]] = []
            for cons_id, uses in by_cons.items():
                uses_sorted = sorted(uses, key=lambda x: x[0])
                base_reload = entity_reloads.get(cons_id, 0)
                for i, (activated_at, duration) in enumerate(uses_sorted):
                    active_end = activated_at + duration
                    if i + 1 < len(uses_sorted):
                        gap_end = uses_sorted[i + 1][0]
                        # Use the gap if shorter than base reload (player used it ASAP),
                        # otherwise cap at base reload (player waited)
                        if base_reload > 0:
                            cooldown_end = min(gap_end, active_end + base_reload)
                        else:
                            cooldown_end = gap_end
                    else:
                        if base_reload > 0:
                            cooldown_end = active_end + base_reload
                        else:
                            cooldown_end = float("inf")
                    timeline.append((activated_at, cons_id, active_end, cooldown_end))
            self._cons_timeline[entity_id] = sorted(timeline, key=lambda x: x[0])

        self._entity_species: dict[int, str] = {}
        self._entity_ship_name: dict[int, str] = {}

        for entity_id, player in ctx.player_lookup.items():
            if not player.ship_id:
                continue
            entry = ship_db.get(player.ship_id, {})
            species = entry.get("species", "")
            icon_key = _SPECIES_TO_ICON.get(species)
            if icon_key:
                self._entity_species[entity_id] = icon_key
            short = entry.get("short_name", "")
            if short:
                self._entity_ship_name[entity_id] = short
            else:
                raw_name = entry.get("name", "")
                if raw_name:
                    parts = raw_name.split("_", 1)
                    display = parts[1] if len(parts) > 1 else parts[0]
                    self._entity_ship_name[entity_id] = display.replace("_", " ")

        # Team groupings
        self._teams: dict[int, list[int]] = {0: [], 1: []}
        for entity_id, player in ctx.player_lookup.items():
            display_team = ctx.raw_to_display_team(player.team_id)
            if display_team in self._teams:
                self._teams[display_team].append(entity_id)
        for team in self._teams.values():
            team.sort(key=lambda eid: (ctx.player_lookup[eid].name or "").lower())

        # Kills from DeathEvents (timestamp-sorted for incremental accumulation)
        self._kill_events: list[tuple[float, int]] = []  # (timestamp, killer_id)
        seen_deaths: set = set()
        for event in ctx.replay.events:
            if type(event).__name__ == "DeathEvent":
                if event.entity_id != event.victim_id:
                    continue
                key = (round(event.timestamp, 1), event.victim_id)
                if key not in seen_deaths:
                    seen_deaths.add(key)
                    killer_id = event.killer_id
                    if killer_id and killer_id != event.victim_id:
                        self._kill_events.append((event.timestamp, killer_id))
        self._kill_events.sort(key=lambda x: x[0])
        self._kills: dict[int, int] = {}
        self._kill_idx: int = 0

        # Damage dealt from DamageEvents (attacker-attributed)
        # For the self player, use server-authoritative receiveDamageStat
        # instead of receiveDamagesOnShip (which undercounts fire/flood/DoT).
        self._self_vehicle_eid: int | None = None
        for p in ctx.player_lookup.values():
            if p.relation == 0:
                self._self_vehicle_eid = p.entity_id
                break

        self._damage_events: list[tuple[float, int, float]] = []
        for event in ctx.replay.events:
            if type(event).__name__ == "DamageEvent" and event.entity_id == event.target_id:
                attacker_id = event.raw_data.get("vehicleID")
                if attacker_id and attacker_id != event.entity_id and event.damage > 0:
                    # Skip self-player damage from this source (replaced below)
                    if attacker_id == self._self_vehicle_eid:
                        continue
                    self._damage_events.append((event.timestamp, attacker_id, event.damage))

        # Self-player damage from receiveDamageStat (authoritative, includes fire/flood)
        if self._self_vehicle_eid is not None:
            for event in ctx.replay.events:
                if (type(event).__name__ == "DamageReceivedStatEvent"
                        and event.stat_type == "ENEMY"
                        and event.delta_total > 0):
                    self._damage_events.append(
                        (event.timestamp, self._self_vehicle_eid, event.delta_total))

        self._damage_events.sort(key=lambda x: x[0])
        self._damage: dict[int, int] = {}
        self._dmg_idx: int = 0

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        panel_w = config.left_panel
        total_h = config.minimap_size

        # Accumulate kills
        while self._kill_idx < len(self._kill_events):
            t, killer_id = self._kill_events[self._kill_idx]
            if t > timestamp:
                break
            self._kills[killer_id] = self._kills.get(killer_id, 0) + 1
            self._kill_idx += 1

        # Accumulate damage dealt
        while self._dmg_idx < len(self._damage_events):
            t, attacker_id, dmg = self._damage_events[self._dmg_idx]
            if t > timestamp:
                break
            self._damage[attacker_id] = self._damage.get(attacker_id, 0) + int(dmg)
            self._dmg_idx += 1

        hud_h = self.ctx.config.hud_height
        cr.set_source_rgba(0.05, 0.08, 0.15, 0.72)
        cr.rectangle(0, hud_h, panel_w, total_h - hud_h)
        cr.fill()

        ship_states = state.ships
        total_players = len(self._teams[0]) + len(self._teams[1])
        if total_players == 0:
            return

        available_h = (total_h - hud_h) - 2 * self.HEADER_HEIGHT
        row_h = min(self.ROW_HEIGHT, available_h // max(total_players, 1))

        y = hud_h
        for display_team in (0, 1):
            players = self._teams[display_team]
            if not players:
                continue
            tc = config.team_colors.get(display_team, (1, 1, 1, 1))
            tr, tg, tb, _ = tc
            label = "ALLY" if display_team == 0 else "ENEMY"
            y = self._draw_header(cr, y, panel_w, label, tr, tg, tb)

            alive_players, dead_players = [], []
            for eid in players:
                ship = ship_states.get(eid)
                (alive_players if ship and ship.is_alive else dead_players).append(eid)

            for eid in alive_players + dead_players:
                ship = ship_states.get(eid)
                is_alive = ship.is_alive if ship else False
                hp_frac = max(0.0, min(1.0, ship.health / ship.max_health)) if (ship and ship.max_health > 0) else 0.0
                kills = self._kills.get(eid, 0)
                damage = self._damage.get(eid, 0)
                cons_status = self._get_cons_status(eid, timestamp)
                y = self._draw_row(cr, y, panel_w, row_h, eid, is_alive, hp_frac,
                                   tr, tg, tb, kills, damage, cons_status,
                                   display_team=display_team)

    def _get_cons_status(self, entity_id: int, timestamp: float) -> list[tuple[int, str, float, int]]:
        """Return list of (cons_id, state, seconds, remaining_charges) for all equipped consumables.

        state: 'active' | 'cooldown' | 'ready'
        seconds: time remaining in current state (0 for ready)
        remaining_charges: charges left (-1 = unlimited, -2 = time-based show remaining capacity)
        """
        # Count uses per consumable up to this timestamp
        use_counts: dict[int, int] = {}
        active_time: dict[int, float] = {}  # total active seconds per cons (for time-based)
        current_state: dict[int, tuple[str, float]] = {}  # cons_id → (state, seconds_left)

        for activated_at, cons_id, active_end, cooldown_end in self._cons_timeline.get(entity_id, []):
            if timestamp < activated_at:
                continue
            use_counts[cons_id] = use_counts.get(cons_id, 0) + 1
            # Track active time for time-based consumables
            if timestamp >= active_end:
                active_time[cons_id] = active_time.get(cons_id, 0) + (active_end - activated_at)
            else:
                active_time[cons_id] = active_time.get(cons_id, 0) + (timestamp - activated_at)

            if activated_at <= timestamp < active_end:
                current_state[cons_id] = ("active", active_end - timestamp)
            elif active_end <= timestamp < cooldown_end:
                current_state[cons_id] = ("cooldown", cooldown_end - timestamp)

        # Build result for all equipped consumables
        charge_info = self._entity_charges.get(entity_id, {})
        result: list[tuple[int, str, float, int]] = []

        if not charge_info:
            # Fallback: no charge data, show only active/cooldown (old behavior)
            for cons_id, (state, secs) in current_state.items():
                result.append((cons_id, state, secs, -1))
            return result

        for cons_id, info in charge_info.items():
            state_tuple = current_state.get(cons_id)

            if info.time_based:
                # Time-based: remaining capacity = max - total active time used
                remaining_cap = max(0, info.max_capacity - active_time.get(cons_id, 0))

                if state_tuple:
                    state, secs = state_tuple
                    result.append((cons_id, state, secs, -2))
                elif remaining_cap > 0:
                    result.append((cons_id, "ready", remaining_cap, -2))
                else:
                    result.append((cons_id, "depleted", 0, 0))
            else:
                # Charge-based
                uses = use_counts.get(cons_id, 0)
                if info.charges == -1:
                    remaining = -1  # unlimited
                else:
                    remaining = max(0, info.charges - uses)

                if state_tuple:
                    state, secs = state_tuple
                    result.append((cons_id, state, secs, remaining))
                else:
                    result.append((cons_id, "ready", 0, remaining))

        return result

    def _draw_header(self, cr, y, panel_w, label, r, g, b) -> float:
        h = self.HEADER_HEIGHT
        cr.set_source_rgba(r, g, b, 0.18)
        cr.rectangle(0, y, panel_w, h)
        cr.fill()
        cr.set_source_rgba(r, g, b, 0.9)
        cr.rectangle(0, y, 3, h)
        cr.fill()
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(10.0)
        ext = cr.text_extents(label)
        cr.set_source_rgba(r, g, b, 1.0)
        cr.move_to(self.PAD_X + 4, y + (h + ext.height) / 2)
        cr.show_text(label)
        return y + h

    def _draw_row(self, cr, y, panel_w, row_h, entity_id,
                  is_alive, hp_frac, tr, tg, tb, kills, damage, cons_status,
                  *, display_team: int = 0) -> float:
        player = self.ctx.player_lookup.get(entity_id)
        if not player:
            return y + row_h

        alpha = 1.0 if is_alive else 0.35

        # Two-line layout:
        #   line1_y  — player name (left)        |  kills (right)
        #   line2_y  — ship name (left) | cons … |  damage (right)
        #   hp strip — thin bar at very bottom of row
        line1_y = y + row_h * 0.35
        line2_y = y + row_h * 0.72
        hp_y    = y + row_h - 3

        # --- Class icon (vertically centred across all three lines) ---
        icon_key = self._entity_species.get(entity_id)
        icons = self.ctx.ship_icons or {}
        if icon_key and icon_key in icons:
            if display_team == 0:
                relation_key = "ally"
            else:
                relation_key = "enemy"
            if not is_alive:
                relation_key = "sunk"
            icon_surf = icons[icon_key].get(relation_key)
            if icon_surf:
                icon_size = self.ICON_SIZE
                iw, ih = icon_surf.get_width(), icon_surf.get_height()
                scale = icon_size / max(iw, ih)
                cr.save()
                # SVG icons point up (north); rotate 90° CW for horizontal display
                cx = self.PAD_X + icon_size / 2
                cy = y + row_h / 2
                cr.translate(cx, cy)
                cr.rotate(math.pi / 2)
                cr.scale(scale, scale)
                cr.set_source_surface(icon_surf, -iw / 2, -ih / 2)
                cr.paint_with_alpha(alpha)
                cr.restore()

        text_x = self.PAD_X + self.ICON_SIZE + 5
        stat_x = panel_w - self.PAD_X - self.HP_BAR_WIDTH - 6  # leave room for HP bar on right
        stat_icon_size = 11

        # --- Line 1: player name (left) | kills (right) ---
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(self.STAT_FONT_SIZE)
        if kills > 0:
            kills_text = str(kills)
            kills_ext = cr.text_extents(kills_text)
            kills_block_x = stat_x - kills_ext.width
            frags_icon = self._stat_icons.get("frags")
            if frags_icon:
                fiw, fih = frags_icon.get_width(), frags_icon.get_height()
                fscale = stat_icon_size / max(fiw, fih)
                cr.save()
                cr.translate(kills_block_x - stat_icon_size - 2, line1_y - stat_icon_size + 1)
                cr.scale(fscale, fscale)
                cr.set_source_surface(frags_icon, 0, 0)
                cr.paint_with_alpha(alpha * 0.85)
                cr.restore()
            cr.set_source_rgba(tr, tg, tb, alpha)
            cr.move_to(kills_block_x, line1_y)
            cr.show_text(kills_text)

        name = player.name or "?"
        kills_w = (cr.text_extents(str(kills)).width + stat_icon_size + 4) if kills > 0 else 0
        max_name_w = stat_x - kills_w - 6 - text_x
        truncated_name = _truncate(cr, name, max_name_w, self.NAME_FONT_SIZE)
        # Division mates get gold name highlighting
        if entity_id in self.ctx.division_mates:
            nr, ng, nb, _ = self.ctx.config.division_color
        else:
            nr, ng, nb = tr, tg, tb
        self.draw_cached_text(cr, text_x, line1_y, truncated_name, nr, ng, nb,
                              alpha=alpha, font_size=self.NAME_FONT_SIZE, bold=True)

        # --- Line 2: ship name (left) | consumables (center) | damage (right) ---
        dmg_text = _fmt_damage(damage)
        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(self.STAT_FONT_SIZE)
        dmg_ext = cr.text_extents(dmg_text)

        # Damage icon + number right-aligned
        dmg_icon = self._stat_icons.get("damage")
        dmg_block_x = stat_x - dmg_ext.width
        if dmg_icon:
            iw, ih = dmg_icon.get_width(), dmg_icon.get_height()
            iscale = stat_icon_size / max(iw, ih)
            cr.save()
            cr.translate(dmg_block_x - stat_icon_size - 2, line2_y - stat_icon_size + 1)
            cr.scale(iscale, iscale)
            cr.set_source_surface(dmg_icon, 0, 0)
            cr.paint_with_alpha(alpha * 0.85)
            cr.restore()
        cr.set_source_rgba(*(  (0.85, 0.78, 0.55, alpha) if damage > 0 else (0.4, 0.4, 0.4, alpha)  ))
        cr.move_to(dmg_block_x, line2_y)
        cr.show_text(dmg_text)

        # Ship name on the left of line 2
        ship_name = self._entity_ship_name.get(entity_id, "")
        if ship_name:
            ship_w = self.draw_cached_text(cr, text_x, line2_y, ship_name, 0.72, 0.72, 0.72,
                                           alpha=alpha, font_size=self.SHIP_FONT_SIZE, bold=False)
            ship_end_x = text_x + ship_w + 6
        else:
            ship_end_x = text_x

        # Consumables: fixed gap after ship name, clipped before damage block
        cons_max_x = dmg_block_x - (stat_icon_size + 4 if dmg_icon else 0) - 6
        if cons_status and is_alive:
            self._draw_cons_line(cr, ship_end_x + 6, line2_y, cons_status, cons_max_x)

        # --- HP bar to the right of kills/damage, against panel edge ---
        hp_w = self.HP_BAR_WIDTH
        hp_h = self.HP_BAR_HEIGHT
        hp_bar_x = panel_w - self.PAD_X - hp_w
        hp_bar_y = y + row_h / 2 - hp_h / 2  # vertically centered in row

        cr.set_source_rgba(0.12, 0.12, 0.12, 0.9)
        cr.rectangle(hp_bar_x, hp_bar_y, hp_w, hp_h)
        cr.fill()
        if hp_frac > 0:
            hr, hg, hb = (0.2, 0.9, 0.2) if hp_frac > 0.66 else ((1.0, 0.85, 0.0) if hp_frac > 0.33 else (1.0, 0.2, 0.2))
            cr.set_source_rgba(hr, hg, hb, alpha)
            cr.rectangle(hp_bar_x, hp_bar_y, hp_w * hp_frac, hp_h)
            cr.fill()

        return y + row_h

    def _draw_cons_line(self, cr, x, y, cons_status, max_x) -> None:
        """Draw consumable icons with state indicators on one line.

        States:
            active   — green icon, time remaining label
            cooldown — gray icon, time remaining label
            ready    — white icon, charge count (or remaining seconds for time-based)
            depleted — dark icon, no label
        """
        icon_size = self.CONS_ICON_SIZE
        font_size = self.CONS_FONT_SIZE
        gap = 4

        cx = x
        for cons_id, state, seconds, remaining in cons_status:
            if cx >= max_x:
                break

            icon = self._cons_icons.get(cons_id)

            # Icon alpha/tint by state
            if state == "active":
                icon_alpha = 1.0
            elif state == "cooldown":
                icon_alpha = 0.35
            elif state == "depleted":
                icon_alpha = 0.15
            else:  # ready
                icon_alpha = 0.7

            # Draw icon
            if icon:
                iw, ih = icon.get_width(), icon.get_height()
                scale = icon_size / max(iw, ih)
                cr.save()
                cr.translate(cx, y - icon_size)
                cr.scale(scale, scale)
                cr.set_source_surface(icon, 0, 0)
                cr.paint_with_alpha(icon_alpha)
                cr.restore()

                if state == "active":
                    self._draw_pie_timer(cr, cx + icon_size / 2, y - icon_size / 2, icon_size / 2 - 1, seconds)

            cx += icon_size + 1

            # Label
            cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(font_size)

            label = ""
            if state == "active":
                cr.set_source_rgba(0.3, 1.0, 0.5, 1.0)  # green
                label = _fmt_seconds(seconds)
            elif state == "cooldown":
                cr.set_source_rgba(0.6, 0.6, 0.6, 0.8)  # gray
                label = _fmt_seconds(seconds)
            elif state == "ready":
                if remaining == -2:
                    # Time-based: show remaining capacity as seconds
                    cr.set_source_rgba(0.9, 0.9, 0.9, 0.8)  # white
                    label = _fmt_seconds(seconds) if seconds > 0 else ""
                elif remaining == -1:
                    # Unlimited charges — no label needed
                    label = ""
                elif remaining > 0:
                    cr.set_source_rgba(0.9, 0.9, 0.9, 0.8)  # white
                    label = str(remaining)
                else:
                    # 0 charges left
                    label = "0"
                    cr.set_source_rgba(0.5, 0.5, 0.5, 0.6)

            if label:
                ext = cr.text_extents(label)
                cr.move_to(cx, y)
                cr.show_text(label)
                cx += ext.width + gap
            else:
                cx += gap

    def _draw_pie_timer(self, cr, cx, cy, radius, remaining_sec) -> None:
        """Draw a semi-transparent dark pie wedge showing time consumed."""
        # We need total duration to compute fraction — but we only have remaining.
        # Use the wedge as a visual "elapsed" indicator based on remaining seconds.
        # Cap display at 300s (5 min) for the visual — longer = full circle.
        MAX_DISPLAY = 300.0
        elapsed_frac = 1.0 - min(remaining_sec / MAX_DISPLAY, 1.0)
        if elapsed_frac <= 0:
            return

        start_angle = -math.pi / 2
        end_angle = start_angle + 2 * math.pi * elapsed_frac

        cr.save()
        cr.new_sub_path()
        cr.move_to(cx, cy)
        cr.arc(cx, cy, radius, start_angle, end_angle)
        cr.close_path()
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.fill()
        cr.restore()


def _fmt_damage(dmg: int) -> str:
    if dmg >= 1_000_000:
        return f"{dmg / 1_000_000:.1f}M"
    if dmg >= 1_000:
        return f"{dmg / 1_000:.1f}k"
    return str(dmg) if dmg > 0 else "0"


def _fmt_seconds(sec: float) -> str:
    if sec == float("inf") or sec > 3600:
        return "?"
    s = int(sec)
    if s >= 60:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s}s"


def _truncate(cr: cairo.Context, text: str, max_w: float, font_size: float = 0) -> str:
    if font_size > 0:
        cr.select_font_face(_font_for_text(text), cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font_size)
    ext = cr.text_extents(text)
    if ext.width <= max_w:
        return text
    while len(text) > 1:
        text = text[:-1]
        if cr.text_extents(text + "…").width <= max_w:
            return text + "…"
    return "…"
