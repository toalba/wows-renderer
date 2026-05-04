from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cairo

from renderer.layers.base import FONT_FAMILY, BaseRenderContext, Layer, _font_for_text

# DEATH_REASON enum from battle.xml → (label, icon_frag filename)
_DEATH_REASON: dict[int, tuple[str, str]] = {
    0: ("", ""),                           # NONE
    1: ("ARTILLERY", "icon_frag_main_caliber"),  # ARTILLERY (generic)
    2: ("SEC", "icon_frag_atba"),          # ATBA
    3: ("TORP", "icon_frag_torpedo"),      # TORPEDO
    4: ("BOMB", "icon_frag_bomb"),         # BOMB
    5: ("TORP", "icon_frag_torpedo"),      # TBOMB (torpedo bomber)
    6: ("FIRE", "icon_frag_burning"),      # BURNING
    7: ("RAM", "icon_frag_ram"),           # RAM
    8: ("TERRAIN", ""),                    # TERRAIN
    9: ("FLOOD", "icon_frag_flood"),       # FLOOD
    10: ("MIRROR", ""),                    # MIRROR
    11: ("MINE", "icon_frag_naval_mine"),  # SEA_MINE
    12: ("", ""),                          # SPECIAL
    13: ("DBOMB", "icon_frag_depthbomb"),  # DBOMB
    14: ("ROCKET", "icon_frag_rocket"),    # ROCKET
    15: ("DETONATE", "icon_frag_detonate"),# DETONATE
    16: ("", ""),                          # HEALTH
    17: ("AP", "icon_frag_main_caliber"),  # AP_SHELL
    18: ("HE", "icon_frag_main_caliber"), # HE_SHELL
    19: ("SAP", "icon_frag_main_caliber"),# CS_SHELL
    20: ("FEL", "icon_frag_fel"),          # FEL
    21: ("PORTAL", "icon_frag_portal"),    # PORTAL
    22: ("SKIP", "icon_frag_skip"),        # SKIP_BOMB
    23: ("WAVE", "icon_frag_wave"),        # SECTOR_WAVE
    24: ("ACID", "icon_frag_acid"),        # ACID
    25: ("LASER", "icon_frag_laser"),      # LASER
    26: ("MATCH", "icon_frag_octagon"),    # MATCH
    28: ("DBOMB", "icon_frag_depthbomb"),  # ADBOMB
    35: ("MISSILE", "icon_frag_missile"),  # MISSILE
}

# Chat channel → display color (r, g, b)
_CHANNEL_COLORS: dict[str, tuple[float, float, float]] = {
    "battle_common": (1.0, 1.0, 1.0),       # white — all chat
    "battle_team": (0.6, 0.9, 1.0),         # light blue — team chat
    "battle_prebattle": (0.8, 0.8, 0.6),    # muted yellow — pre-battle
}


@dataclass
class _FeedEntry:
    """A single entry in the kill+chat feed."""
    timestamp: float
    kind: str  # "kill" or "chat"
    # Kill fields
    victim_id: int = 0
    killer_id: int = 0
    death_reason: int = 0
    # Chat fields
    sender_name: str = ""
    sender_team: int = -1  # display team (0=ally, 1=enemy, -1=unknown)
    channel: str = ""
    message: str = ""


class KillfeedLayer(Layer):
    """Displays recent kills and chat messages as a feed on the right panel."""

    DISPLAY_DURATION = 120.0  # game-seconds (at 20x speed = 6s of video)
    CHAT_DISPLAY_DURATION = 200.0  # chat stays longer
    FONT_SIZE = 13
    LINE_HEIGHT = 20
    MAX_VISIBLE = 10
    ICON_SIZE = 16

    def initialize(self, ctx: BaseRenderContext) -> None:
        super().initialize(ctx)

        # Load frag icons
        icon_dir = Path(ctx.config.effective_gamedata_path) / "gui" / "battle_hud" / "icon_frag"
        self._icons: dict[str, cairo.ImageSurface] = {}
        for _, (_, icon_name) in _DEATH_REASON.items():
            if icon_name and icon_name not in self._icons:
                path = icon_dir / f"{icon_name}.png"
                if path.exists():
                    try:
                        self._icons[icon_name] = cairo.ImageSurface.create_from_png(str(path))
                    except Exception:
                        pass

        # Build account_id → (name, display_team) lookup for chat
        account_lookup: dict[int, tuple[str, int]] = {}
        for entity_id, player in ctx.player_lookup.items():
            display_team = ctx.raw_to_display_team(player.team_id)
            account_lookup[player.account_id] = (player.name, display_team)

        # Build unified feed from kills + chat
        entries: list[_FeedEntry] = []

        # Kills
        seen_kills: set[tuple[float, int]] = set()
        for event in ctx.replay.events:
            if type(event).__name__ == "DeathEvent":
                if event.entity_id != event.victim_id:
                    continue
                key = (round(event.timestamp, 1), event.victim_id)
                if key not in seen_kills:
                    seen_kills.add(key)
                    reason = event.raw_data.get("arg1", 0)
                    entries.append(_FeedEntry(
                        timestamp=event.timestamp,
                        kind="kill",
                        victim_id=event.victim_id,
                        killer_id=event.killer_id,
                        death_reason=reason,
                    ))

        # Chat messages
        from wows_replay_parser.events.models import ChatEvent
        for event in ctx.replay.events:
            if not isinstance(event, ChatEvent):
                continue
            if not event.message:
                continue
            sender_info = account_lookup.get(event.sender_id)
            if sender_info:
                name, display_team = sender_info
            else:
                name = "Player"
                display_team = -1
            entries.append(_FeedEntry(
                timestamp=event.timestamp,
                kind="chat",
                sender_name=name,
                sender_team=display_team,
                channel=event.channel,
                message=event.message,
            ))

        entries.sort(key=lambda e: e.timestamp)
        self._entries = entries

        # Build entity_id → ship display name lookup
        ship_db = ctx.ship_db or {}
        self._ship_names: dict[int, str] = {}
        for entity_id, player in ctx.player_lookup.items():
            if not player.ship_id:
                continue
            entry = ship_db.get(player.ship_id, {})
            short = entry.get("short_name", "")
            if short:
                self._ship_names[entity_id] = short
            else:
                raw = entry.get("name", "")
                if raw:
                    parts = raw.split("_", 1)
                    self._ship_names[entity_id] = (parts[1] if len(parts) > 1 else parts[0]).replace("_", " ")

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        config = self.ctx.config
        player_lookup = self.ctx.player_lookup

        visible: list[tuple[float, _FeedEntry]] = []
        for entry in self._entries:
            age = timestamp - entry.timestamp
            if age < 0:
                break
            max_age = self.CHAT_DISPLAY_DURATION if entry.kind == "chat" else self.DISPLAY_DURATION
            if age <= max_age:
                visible.append((age, entry))

        if not visible:
            return

        visible = visible[-self.MAX_VISIBLE:]

        x_base = config.left_panel + config.minimap_size + 8
        s = self.ctx.scale
        font_size = self.FONT_SIZE * s
        line_h = self.LINE_HEIGHT * s
        icon_size = self.ICON_SIZE * s
        # 4px right padding so wrapped text doesn't touch the panel edge
        max_x = config.left_panel + config.minimap_size + config.right_panel - 4

        # Pre-compute wrapped layout per entry so multi-line entries consume
        # multiple line slots (chat: word-wrapped message; kill: victim wraps
        # to a second line when killer+icon+victim won't fit).
        layouts: list[tuple[int, object]] = []
        for _age, entry in visible:
            if entry.kind == "chat":
                chat_layout = self._chat_layout(cr, entry, x_base, max_x, font_size)
                layouts.append((max(1, len(chat_layout[1])), chat_layout))
            else:
                kill_lines = self._kill_layout(cr, entry, x_base, max_x, font_size, icon_size)
                layouts.append((kill_lines, None))
        total_slots = sum(n for n, _ in layouts)

        # Anchor from bottom of minimap area, grow upward
        y_bottom = config.hud_height + config.minimap_size - 10
        y_start = y_bottom - total_slots * line_h

        # Clip to right panel area
        cr.save()
        clip_x = config.left_panel + config.minimap_size
        clip_w = config.right_panel
        cr.rectangle(clip_x, 0, clip_w, config.total_height)
        cr.clip()

        slots_used = 0
        for (age, entry), (n_lines, chat_layout) in zip(visible, layouts, strict=True):
            y = y_start + slots_used * line_h

            if entry.kind == "kill":
                max_age = self.DISPLAY_DURATION
                alpha = min(1.0, (max_age - age) / 20.0)
                self._render_kill(cr, x_base, y, alpha, entry, font_size, icon_size,
                                  line_h, max_x, n_lines, player_lookup)
            else:
                max_age = self.CHAT_DISPLAY_DURATION
                alpha = min(1.0, (max_age - age) / 30.0)
                self._render_chat(cr, x_base, y, alpha, entry, font_size, line_h, chat_layout)

            slots_used += n_lines

        cr.restore()  # end clip

    def _kill_components(self, entry: _FeedEntry) -> dict:
        """Resolve names, colors, ships, and death icon/label for a kill entry.
        Shared between layout (measure-only) and render passes."""
        config = self.ctx.config
        player_lookup = self.ctx.player_lookup
        killer = player_lookup.get(entry.killer_id)
        victim = player_lookup.get(entry.victim_id)

        killer_name = killer.name if killer else "?"
        victim_name = victim.name if victim else "?"

        if killer and hasattr(killer, "team_id"):
            kr, kg, kb, _ = config.team_colors.get(
                self.ctx.raw_to_display_team(killer.team_id), (1, 1, 1, 1))
        else:
            kr, kg, kb = 1, 1, 1
        if victim and hasattr(victim, "team_id"):
            vr, vg, vb, _ = config.team_colors.get(
                self.ctx.raw_to_display_team(victim.team_id), (1, 1, 1, 1))
        else:
            vr, vg, vb = 1, 1, 1

        label, icon_name = _DEATH_REASON.get(entry.death_reason, ("", ""))
        icon_surface = self._icons.get(icon_name) if icon_name else None

        return {
            "killer_name": killer_name,
            "killer_color": (kr, kg, kb),
            "killer_ship": self._ship_names.get(entry.killer_id, ""),
            "victim_name": victim_name,
            "victim_color": (vr, vg, vb),
            "victim_ship": self._ship_names.get(entry.victim_id, ""),
            "label": label,
            "icon_surface": icon_surface,
        }

    def _kill_layout(
        self, cr: cairo.Context, entry: _FeedEntry, x_base: float, max_x: float,
        font_size: float, icon_size: float,
    ) -> int:
        """Return number of line slots needed for a kill entry (1 or 2).

        Wraps to 2 lines when killer + ship + icon + victim + ship overflows
        the right panel; the wrap point is between icon and victim, matching
        the natural visual break in '<killer> ICON <victim>'.
        """
        comps = self._kill_components(entry)
        kr, kg, kb = comps["killer_color"]
        vr, vg, vb = comps["victim_color"]

        _, kw, _ = self.get_cached_text(cr, comps["killer_name"], font_size, True, kr, kg, kb)
        ksw = 0.0
        if comps["killer_ship"]:
            _, ksw, _ = self.get_cached_text(
                cr, f" ({comps['killer_ship']}) ", font_size * 0.85, False, 0.85, 0.85, 0.85)

        if comps["icon_surface"]:
            icon_w = icon_size + 4
        elif comps["label"]:
            _, icon_w, _ = self.get_cached_text(
                cr, f" [{comps['label']}] ", font_size * 0.85, False, 0.8, 0.8, 0.8)
        else:
            _, icon_w, _ = self.get_cached_text(cr, " \u2715 ", font_size, False, 0.8, 0.8, 0.8)

        _, vw, _ = self.get_cached_text(cr, comps["victim_name"], font_size, True, vr, vg, vb)
        vsw = 0.0
        if comps["victim_ship"]:
            _, vsw, _ = self.get_cached_text(
                cr, f" ({comps['victim_ship']})", font_size * 0.85, False, 0.85, 0.85, 0.85)

        total = x_base + kw + ksw + 4 + icon_w + vw + vsw
        return 2 if total > max_x else 1

    def _render_kill(
        self, cr: cairo.Context, x_base: float, y: float, alpha: float,
        entry: _FeedEntry, font_size: float, icon_size: float,
        line_h: float, max_x: float, n_lines: int,
        player_lookup: dict,
    ) -> None:
        comps = self._kill_components(entry)
        kr, kg, kb = comps["killer_color"]
        vr, vg, vb = comps["victim_color"]
        killer_ship = comps["killer_ship"]
        victim_ship = comps["victim_ship"]
        label = comps["label"]
        icon_surface = comps["icon_surface"]

        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font_size)

        # Killer name + ship on line 1
        ext_k_w = self.draw_cached_text(cr, x_base, y, comps["killer_name"], kr, kg, kb,
                                        alpha=alpha, font_size=font_size, bold=True)
        if killer_ship:
            ship_text = f" ({killer_ship}) "
            ext_ks_w = self.draw_cached_text(cr, x_base + ext_k_w, y, ship_text, 0.85, 0.85, 0.85,
                                             alpha=alpha * 0.7, font_size=font_size * 0.85, bold=False)
            icon_x = x_base + ext_k_w + ext_ks_w + 4
        else:
            icon_x = x_base + ext_k_w + 4

        # Death reason icon or text
        if icon_surface:
            iw = icon_surface.get_width()
            ih = icon_surface.get_height()
            icon_scale = icon_size / max(iw, ih)
            cr.save()
            cr.translate(icon_x, y - icon_size + 2)
            cr.scale(icon_scale, icon_scale)
            cr.set_source_surface(icon_surface, 0, 0)
            cr.paint_with_alpha(alpha)
            cr.restore()
            after_icon_x = icon_x + icon_size + 4
        elif label:
            cause_text = f" [{label}] "
            ext_c_w = self.draw_cached_text(cr, icon_x, y, cause_text, 0.8, 0.8, 0.8,
                                            alpha=alpha * 0.7, font_size=font_size * 0.85, bold=False)
            after_icon_x = icon_x + ext_c_w
        else:
            ext_c_w = self.draw_cached_text(cr, icon_x, y, " \u2715 ", 0.8, 0.8, 0.8,
                                            alpha=alpha * 0.7, font_size=font_size, bold=False)
            after_icon_x = icon_x + ext_c_w

        # Victim: same line if it fits, otherwise wrap to a continuation line
        # with a leading arrow so the chain reads naturally across two lines.
        if n_lines >= 2:
            cont_x = x_base + 8 * self.ctx.scale
            y2 = y + line_h
            arrow_w = self.draw_cached_text(cr, cont_x, y2, "\u2192 ", 0.8, 0.8, 0.8,
                                            alpha=alpha * 0.7, font_size=font_size, bold=False)
            v_x = cont_x + arrow_w
            ext_v_w = self.draw_cached_text(cr, v_x, y2, comps["victim_name"], vr, vg, vb,
                                            alpha=alpha, font_size=font_size, bold=True)
            if victim_ship:
                ship_text_v = f" ({victim_ship})"
                _, ship_w, _ = self.get_cached_text(
                    cr, ship_text_v, font_size * 0.85, False, 0.85, 0.85, 0.85)
                if v_x + ext_v_w + ship_w <= max_x:
                    self.draw_cached_text(cr, v_x + ext_v_w, y2, ship_text_v, 0.85, 0.85, 0.85,
                                          alpha=alpha * 0.7, font_size=font_size * 0.85, bold=False)
        else:
            ext_v_w = self.draw_cached_text(cr, after_icon_x, y, comps["victim_name"], vr, vg, vb,
                                            alpha=alpha, font_size=font_size, bold=True)
            if victim_ship:
                ship_text_v = f" ({victim_ship})"
                self.draw_cached_text(cr, after_icon_x + ext_v_w, y, ship_text_v, 0.85, 0.85, 0.85,
                                      alpha=alpha * 0.7, font_size=font_size * 0.85, bold=False)

    def _chat_meta(self, entry: _FeedEntry) -> tuple[tuple[float, float, float], str]:
        """Return ((sender_r, sender_g, sender_b), channel_prefix) for an entry."""
        config = self.ctx.config
        if entry.sender_team >= 0:
            sr, sg, sb, _ = config.team_colors.get(entry.sender_team, (1, 1, 1, 1))
        else:
            sr, sg, sb = _CHANNEL_COLORS.get(entry.channel, (1, 1, 1))

        prefix = ""
        if entry.channel == "battle_team":
            prefix = "[T] "
        elif entry.channel == "battle_prebattle":
            prefix = "[P] "
        return (sr, sg, sb), prefix

    def _chat_layout(
        self, cr: cairo.Context, entry: _FeedEntry, x_base: float, max_x: float, font_size: float,
    ) -> tuple[float, list[tuple[float, str]]]:
        """Compute (cont_x, message_lines) for a chat entry.

        Measures the prefix+sender+": " header to find where the message starts on
        line one, then word-wraps the message; continuation lines hang under the
        message (indented past the header) for visual continuity.
        """
        sender_color, prefix = self._chat_meta(entry)
        sr, sg, sb = sender_color

        x = x_base
        if prefix:
            _, w, _ = self.get_cached_text(cr, prefix, font_size * 0.85, False, 0.7, 0.7, 0.7)
            x += w
        _, w, _ = self.get_cached_text(cr, entry.sender_name, font_size, True, sr, sg, sb)
        x += w
        _, w, _ = self.get_cached_text(cr, ": ", font_size, False, 0.7, 0.7, 0.7)
        x += w

        msg_font_size = font_size * 0.9
        cont_x = x_base + 8 * self.ctx.scale
        lines = self._wrap_message(cr, entry.message, msg_font_size, x, cont_x, max_x)
        return cont_x, lines

    @staticmethod
    def _wrap_message(
        cr: cairo.Context, message: str, font_size: float,
        first_line_x: float, cont_x: float, max_x: float,
    ) -> list[tuple[float, str]]:
        """Greedy word-wrap. Returns list of (x, text) per line.

        Long words that don't fit on a continuation line are hard-broken per
        character so URLs and unbroken strings still display.
        """
        cr.select_font_face(_font_for_text(message), cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(font_size)

        def width(s: str) -> float:
            return cr.text_extents(s).width

        lines: list[tuple[float, str]] = []
        cur_x = first_line_x
        cur_text = ""

        def flush() -> None:
            nonlocal cur_text, cur_x
            if cur_text:
                lines.append((cur_x, cur_text))
                cur_text = ""
            cur_x = cont_x

        for word in message.split(" "):
            if not word:
                continue
            candidate = f"{cur_text} {word}" if cur_text else word
            if cur_x + width(candidate) <= max_x:
                cur_text = candidate
                continue
            flush()
            if cont_x + width(word) <= max_x:
                cur_text = word
            else:
                # Word is too long even alone — hard break per character
                buf = ""
                for ch in word:
                    if cont_x + width(buf + ch) <= max_x:
                        buf += ch
                    else:
                        if buf:
                            lines.append((cont_x, buf))
                        buf = ch
                cur_text = buf
        if cur_text:
            lines.append((cur_x, cur_text))
        return lines

    def _render_chat(
        self, cr: cairo.Context, x_base: float, y: float, alpha: float,
        entry: _FeedEntry, font_size: float, line_h: float,
        chat_layout: tuple[float, list[tuple[float, str]]],
    ) -> None:
        sender_color, prefix = self._chat_meta(entry)
        sr, sg, sb = sender_color
        _cont_x, message_lines = chat_layout

        x = x_base
        if prefix:
            x += self.draw_cached_text(cr, x, y, prefix, 0.7, 0.7, 0.7,
                                       alpha=alpha * 0.6, font_size=font_size * 0.85, bold=False)

        x += self.draw_cached_text(cr, x, y, entry.sender_name, sr, sg, sb,
                                   alpha=alpha, font_size=font_size, bold=True)

        x += self.draw_cached_text(cr, x, y, ": ", 0.7, 0.7, 0.7,
                                   alpha=alpha * 0.8, font_size=font_size, bold=False)

        msg_font_size = font_size * 0.9
        for i, (lx, ltext) in enumerate(message_lines):
            self.draw_cached_text(cr, lx, y + i * line_h, ltext, 0.9, 0.9, 0.9,
                                  alpha=alpha * 0.9, font_size=msg_font_size, bold=False)
