from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cairo

from renderer.layers.base import Layer, BaseRenderContext, FONT_FAMILY


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
                name = f"Player"
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

        # Anchor from bottom of minimap area, grow upward
        y_bottom = config.hud_height + config.minimap_size - 10
        y_start = y_bottom - len(visible) * line_h

        # Clip to right panel area
        cr.save()
        clip_x = config.left_panel + config.minimap_size
        clip_w = config.right_panel
        cr.rectangle(clip_x, 0, clip_w, config.total_height)
        cr.clip()

        for i, (age, entry) in enumerate(visible):
            y = y_start + i * line_h

            if entry.kind == "kill":
                max_age = self.DISPLAY_DURATION
                alpha = min(1.0, (max_age - age) / 20.0)
                self._render_kill(cr, x_base, y, alpha, entry, font_size, icon_size, player_lookup)
            else:
                max_age = self.CHAT_DISPLAY_DURATION
                alpha = min(1.0, (max_age - age) / 30.0)
                self._render_chat(cr, x_base, y, alpha, entry, font_size)

        cr.restore()  # end clip

    def _render_kill(
        self, cr: cairo.Context, x_base: float, y: float, alpha: float,
        entry: _FeedEntry, font_size: float, icon_size: float,
        player_lookup: dict,
    ) -> None:
        config = self.ctx.config
        killer = player_lookup.get(entry.killer_id)
        victim = player_lookup.get(entry.victim_id)

        killer_name = killer.name if killer else "?"
        victim_name = victim.name if victim else "?"

        if killer and hasattr(killer, "team_id"):
            display_team = self.ctx.raw_to_display_team(killer.team_id)
            kr, kg, kb, _ = config.team_colors.get(display_team, (1, 1, 1, 1))
        else:
            kr, kg, kb = 1, 1, 1

        if victim and hasattr(victim, "team_id"):
            display_team = self.ctx.raw_to_display_team(victim.team_id)
            vr, vg, vb, _ = config.team_colors.get(display_team, (1, 1, 1, 1))
        else:
            vr, vg, vb = 1, 1, 1

        cr.select_font_face(FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font_size)

        # Killer name + ship
        killer_ship = self._ship_names.get(entry.killer_id, "")
        ext_k_w = self.draw_cached_text(cr, x_base, y, killer_name, kr, kg, kb,
                                        alpha=alpha, font_size=font_size, bold=True)
        if killer_ship:
            ship_text = f" ({killer_ship}) "
            ext_ks_w = self.draw_cached_text(cr, x_base + ext_k_w, y, ship_text, 0.85, 0.85, 0.85,
                                             alpha=alpha * 0.7, font_size=font_size * 0.85, bold=False)
            icon_x = x_base + ext_k_w + ext_ks_w + 4
        else:
            icon_x = x_base + ext_k_w + 4

        # Death reason icon or text
        label, icon_name = _DEATH_REASON.get(entry.death_reason, ("", ""))
        icon_surface = self._icons.get(icon_name) if icon_name else None

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

        # Victim name + ship
        ext_v_w = self.draw_cached_text(cr, after_icon_x, y, victim_name, vr, vg, vb,
                                        alpha=alpha, font_size=font_size, bold=True)
        victim_ship = self._ship_names.get(entry.victim_id, "")
        if victim_ship:
            ship_text_v = f" ({victim_ship})"
            self.draw_cached_text(cr, after_icon_x + ext_v_w, y, ship_text_v, 0.85, 0.85, 0.85,
                                  alpha=alpha * 0.7, font_size=font_size * 0.85, bold=False)

    def _render_chat(
        self, cr: cairo.Context, x_base: float, y: float, alpha: float,
        entry: _FeedEntry, font_size: float,
    ) -> None:
        config = self.ctx.config

        # Sender color: team-colored if known, else channel color
        if entry.sender_team >= 0:
            sr, sg, sb, _ = config.team_colors.get(entry.sender_team, (1, 1, 1, 1))
        else:
            sr, sg, sb = _CHANNEL_COLORS.get(entry.channel, (1, 1, 1))

        # Channel prefix for team chat
        prefix = ""
        if entry.channel == "battle_team":
            prefix = "[T] "
        elif entry.channel == "battle_prebattle":
            prefix = "[P] "

        # Render: [prefix] name: message
        x = x_base
        if prefix:
            x += self.draw_cached_text(cr, x, y, prefix, 0.7, 0.7, 0.7,
                                       alpha=alpha * 0.6, font_size=font_size * 0.85, bold=False)

        x += self.draw_cached_text(cr, x, y, entry.sender_name, sr, sg, sb,
                                   alpha=alpha, font_size=font_size, bold=True)

        x += self.draw_cached_text(cr, x, y, ": ", 0.7, 0.7, 0.7,
                                   alpha=alpha * 0.8, font_size=font_size, bold=False)

        # Truncate message to fit panel
        max_msg_width = config.total_width - x - 8
        msg = entry.message
        # Simple truncation — could be smarter but good enough
        self.draw_cached_text(cr, x, y, msg, 0.9, 0.9, 0.9,
                              alpha=alpha * 0.9, font_size=font_size * 0.9, bold=False)
