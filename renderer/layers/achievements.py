"""Achievement icon overlay for the recording player.

Reads ``AchievementEvent`` entries from the parsed replay, filters to the
recording player, and renders the corresponding icons in first-appearance
order directly below the ribbon block on the right panel.

Persistent accumulator — once an icon appears it stays for the rest of the
match. Mirrors the layout rules of :class:`RibbonLayer` (24px icons, 3px
gap, panel-width wrap). Renders nothing and consumes no vertical space
until at least one achievement has been earned.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cairo

from renderer.layers.base import Layer, SingleRenderContext

log = logging.getLogger(__name__)


class AchievementLayer(Layer):
    """Persistent achievement-icon row under the ribbon block."""

    MAIN_HEIGHT = 24        # icon display height (matches RibbonLayer.MAIN_HEIGHT)
    GAP = 3                 # gap between icons / wrap rows
    Y_PAD = 6               # gap above the row (below ribbons.panel_bottom)
    FALLBACK_FILENAME = "default.png"

    panel_bottom: float = 0.0

    def initialize(self, ctx: SingleRenderContext) -> None:
        super().initialize(ctx)

        self._timeline: list[tuple[float, int]] = []
        self._tl_idx: int = 0
        self._seen_order: list[int] = []
        self._icons: dict[int, cairo.ImageSurface] = {}
        self._fallback_icon: cairo.ImageSurface | None = None
        self._achievement_names: dict[int, str] = {}

        # 1. Find recording player's account_id (relation == 0).
        own_account_id: int | None = None
        for player in ctx.player_lookup.values():
            if getattr(player, "relation", None) == 0:
                own_account_id = player.account_id
                break
        if own_account_id is None:
            log.debug("No self-player found in player_lookup; achievement layer idle")
            return

        # 2. Build timeline from achievement events for the recording player.
        from wows_replay_parser.events.models import AchievementEvent
        for event in ctx.replay.events:
            if not isinstance(event, AchievementEvent):
                continue
            if event.player_id != own_account_id:
                continue
            self._timeline.append((event.timestamp, event.achievement_id))

        # 3. Resolve id -> ui_name via versioned_gamedata.
        vgd = getattr(ctx.config, "versioned_gamedata", None)
        if vgd is None:
            log.debug("No versioned_gamedata; achievement icons unavailable")
            return
        try:
            self._achievement_names = dict(vgd.achievements)
        except Exception:
            log.debug("Failed to read versioned_gamedata.achievements", exc_info=True)
            return

        # 4. Preload icons we expect to use (unique achievement_ids in timeline).
        gui_dir = Path(ctx.config.effective_gamedata_path) / "gui" / "achievements"
        seen_ids: set[int] = set()
        for _, aid in self._timeline:
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            self._try_load_icon(gui_dir, aid)

        # Always try the fallback so we can draw something for unknown IDs.
        fb_path = gui_dir / self.FALLBACK_FILENAME
        if fb_path.exists():
            try:
                self._fallback_icon = cairo.ImageSurface.create_from_png(str(fb_path))
            except Exception:
                log.debug("Failed to load fallback icon %s", fb_path, exc_info=True)

    def _try_load_icon(self, gui_dir: Path, achievement_id: int) -> None:
        ui_name = self._achievement_names.get(achievement_id)
        if ui_name is None:
            return
        path = gui_dir / f"icon_achievement_{ui_name}.png"
        if not path.exists():
            return
        try:
            self._icons[achievement_id] = cairo.ImageSurface.create_from_png(str(path))
        except Exception:
            log.debug("Failed to load icon %s", path, exc_info=True)

    def render(self, cr: cairo.Context, state: object, timestamp: float) -> None:
        # Advance timeline up to `timestamp`, appending new unique IDs.
        while self._tl_idx < len(self._timeline):
            t, aid = self._timeline[self._tl_idx]
            if t > timestamp:
                break
            if aid not in self._seen_order:
                self._seen_order.append(aid)
            self._tl_idx += 1

        # Anchor under ribbons, else damage stats, else fall back to the
        # top of the right panel. Mirrors RibbonLayer's own chain so the
        # layer behaves sanely when show_ribbons=False is set.
        s = self.ctx.scale
        ribbons_ref = getattr(self, "_ribbons_ref", None)
        if ribbons_ref is not None and ribbons_ref.panel_bottom > 0:
            y_start = ribbons_ref.panel_bottom + self.Y_PAD * s
        else:
            dmg_ref = getattr(self, "_dmg_stats_ref", None)
            if dmg_ref is not None and dmg_ref.panel_bottom > 0:
                y_start = dmg_ref.panel_bottom + self.Y_PAD * s
            else:
                y_start = self.ctx.config.hud_height + 10

        if not self._seen_order:
            # No rows drawn — expose y_start so any future downstream
            # layer can anchor at the same position we would have used.
            self.panel_bottom = y_start
            return

        config = self.ctx.config
        icon_h = self.MAIN_HEIGHT * s
        gap = self.GAP * s
        x_start = config.left_panel + config.minimap_size + 8
        max_x = config.total_width - 4

        cr.save()
        clip_x = config.left_panel + config.minimap_size
        cr.rectangle(clip_x, 0, config.right_panel, config.total_height)
        cr.clip()

        x = x_start
        y_row = y_start
        for aid in self._seen_order:
            icon = self._icons.get(aid, self._fallback_icon)
            if icon is None:
                continue
            iw = icon.get_width()
            ih = icon.get_height()
            scale = icon_h / ih
            draw_w = iw * scale
            if x + draw_w > max_x and x > x_start:
                x = x_start
                y_row += icon_h + gap
            cr.save()
            cr.translate(x, y_row)
            cr.scale(scale, scale)
            cr.set_source_surface(icon, 0, 0)
            cr.paint()
            cr.restore()
            x += draw_w + gap

        self.panel_bottom = y_row + icon_h
        cr.restore()
