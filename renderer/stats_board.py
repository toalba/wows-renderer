"""Post-battle statistics board — Cairo table renderer.

Pure presentation. Given a MatchStats it produces PNG bytes; it knows
nothing about replays, gamedata or Discord, which is what lets it run in
the bot process on a button click rather than in the render worker.
"""
from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass

import cairo

from renderer.stats_export import MatchStats, PlayerStats
from renderer.themes import THEMES

BG = (0x0D / 255, 0x15 / 255, 0x20 / 255)
LABEL_PRIMARY = (0xE8 / 255, 0xE4 / 255, 0xD9 / 255)
LABEL_SECONDARY = (0x9B / 255, 0xA4 / 255, 0xAB / 255)
ACCENT_FIRE = (1.0, 0.55, 0.2)
ACCENT_FLOOD = (0.35, 0.65, 1.0)
ACCENT_CIT = (1.0, 0.35, 0.35)
DIM = 0.35            # alpha for zero-valued accent cells

ROW_H = 30
HEADER_H = 34
TITLE_H = 56
PAD_X = 14
COL_GAP = 18
FONT = "sans-serif"
FONT_SIZE = 15

# Cap on the measured width of left-aligned text columns (Player, Ship,
# Killed by). Numeric columns are bounded by their formatter output, but a
# clan tag + a long player name has no natural ceiling — without a cap one
# outlier row would stretch every column and blow out the sheet width.
# Content past the cap is ellipsised at draw time, never left to overflow
# into the next column.
MAX_TEXT_CELL_W = 260.0

# Vertical breathing room inserted at each team transition, on top of the
# per-row height. render_stats_board sizes the surface for however many
# transitions _team_gaps() actually counts, so this isn't limited to
# exactly one gap.
TEAM_GAP = 10.0


@dataclass(frozen=True)
class Column:
    key: str                                  # PlayerStats attribute
    label: str
    fmt: Callable[[PlayerStats], str]
    align: str = "right"                      # "right" | "left"
    accent: tuple[float, float, float] | None = None   # dim when zero


def _thousands(n: int) -> str:
    return f"{n:,}"


def _mmss(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


COLUMNS: tuple[Column, ...] = (
    Column("name", "Player", lambda p: (f"[{p.clan_tag}] " if p.clan_tag else "") + p.name, "left"),
    Column("ship_name", "Ship", lambda p: (f"{p.ship_class} " if p.ship_class else "") + p.ship_name, "left"),
    Column("damage", "Dmg", lambda p: _thousands(p.damage)),
    Column("received", "Recv", lambda p: _thousands(p.received)),
    Column("spotting", "Spot", lambda p: _thousands(p.spotting)),
    Column("potential", "Potential", lambda p: _thousands(p.potential)),
    Column("kills", "K", lambda p: str(p.kills)),
    Column("hits", "Hits", lambda p: str(p.hits)),
    Column("accuracy", "Acc", lambda p: "—" if p.accuracy is None else f"{p.accuracy:.0f}%"),
    Column("fires", "Fire", lambda p: str(p.fires), accent=ACCENT_FIRE),
    Column("floods", "Flood", lambda p: str(p.floods), accent=ACCENT_FLOOD),
    Column("citadels", "Cit", lambda p: str(p.citadels), accent=ACCENT_CIT),
    Column("penetrations", "Pen", lambda p: str(p.penetrations)),
    Column("overpens", "OvP", lambda p: str(p.overpens)),
    Column("shatters", "Shtr", lambda p: str(p.shatters)),
    Column("crits", "Crit", lambda p: str(p.crits)),
    Column("major_crits", "Maj", lambda p: str(p.major_crits)),
    Column("module_breaks", "Brk", lambda p: str(p.module_breaks)),
    Column("caps", "Caps", lambda p: str(p.caps)),
    Column("caps_reset", "Rst", lambda p: str(p.caps_reset)),
    Column("first_spots", "1st", lambda p: str(p.first_spots)),
    Column("torps_spotted", "TpdSp", lambda p: str(p.torps_spotted)),
    Column("planes_killed", "Planes", lambda p: str(p.planes_killed)),
    Column("aa_damage", "AA", lambda p: _thousands(p.aa_damage)),
    Column("distance_km", "Dist", lambda p: f"{p.distance_km:.1f}km"),
    Column("xp", "XP", lambda p: _thousands(p.xp)),
    Column("hp_remaining", "HP", lambda p: "—" if not p.max_health
           else f"{100 * p.hp_remaining // p.max_health}%"),
    Column("life_time_sec", "Time", lambda p: _mmss(p.life_time_sec)),
    Column("killed_by", "Killed by", lambda p: p.killed_by or "—", "left"),
)


def _measure(stats: MatchStats) -> list[float]:
    """Column widths from rendered content: max(label, every cell) + gap.

    Left-aligned text columns are capped at MAX_TEXT_CELL_W so one long
    name can't stretch the whole sheet; the draw side ellipsises to match.
    """
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    cr = cairo.Context(surface)
    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(FONT_SIZE)

    widths = []
    for col in COLUMNS:
        content_w = cr.text_extents(col.label).width
        for p in stats.players:
            w = cr.text_extents(col.fmt(p)).width
            if col.align == "left":
                w = min(w, MAX_TEXT_CELL_W)
            content_w = max(content_w, w)
        widths.append(content_w + COL_GAP)
    return widths


def _column_x(widths: list[float]) -> list[float]:
    """Left edge x for each column, starting at the PAD_X margin."""
    xs: list[float] = []
    x: float = PAD_X
    for w in widths:
        xs.append(x)
        x += w
    return xs


def _ellipsize(cr: cairo.Context, text: str, max_width: float) -> str:
    """Truncate text with a trailing ellipsis so it fits max_width.

    Returns the input unchanged when it already fits — the common case,
    since column widths are measured from this same content.
    """
    if max_width <= 0:
        return ""
    if cr.text_extents(text).width <= max_width:
        return text
    ellipsis = "…"
    for i in range(len(text) - 1, 0, -1):
        candidate = text[:i].rstrip() + ellipsis
        if cr.text_extents(candidate).width <= max_width:
            return candidate
    return ellipsis


def _row_fill(player: PlayerStats, theme: str) -> tuple[float, float, float, float]:
    """Team-tinted row background: brighter for the recording player's row."""
    r, g, b, _a = THEMES[theme].team_colors[player.team]
    alpha = 0.22 if player.is_self else 0.10
    return (r, g, b, alpha)


def _cell_style(col: Column, player: PlayerStats) -> tuple[tuple[float, float, float], float]:
    """(rgb, alpha) for one cell's text.

    Accent columns (Fire/Flood/Cit) carry their own colour when non-zero
    and fall back to dimmed secondary grey at zero — that contrast is
    what makes the sheet scannable. Ship name and killer are secondary
    detail text; everything else (including the player name) is primary.
    """
    if col.accent is not None:
        if getattr(player, col.key):
            return col.accent, 1.0
        return LABEL_SECONDARY, DIM
    if col.key in ("ship_name", "killed_by"):
        return LABEL_SECONDARY, 1.0
    return LABEL_PRIMARY, 1.0


def _baseline_in(cr: cairo.Context, top: float, box_h: float) -> float:
    """Baseline y that vertically centers the current font in a box."""
    ascent, descent, *_ = cr.font_extents()
    return top + (box_h + ascent - descent) / 2


def _cell_x(x: float, w: float, text_w: float, align: str) -> float:
    """Text origin x for one cell: flush right (minus half the inter-column
    gap) for right-aligned columns, flush left (plus half the gap) for
    left-aligned ones. Shared by the header and every row so the two never
    drift apart."""
    if align == "right":
        return x + w - COL_GAP / 2 - text_w
    return x + COL_GAP / 2


def _team_gaps(players: tuple[PlayerStats, ...]) -> int:
    """Number of team transitions between consecutive rows.

    Players arrive pre-sorted by team, so in the expected case this is 0 or
    1 (one gap between the two team blocks). Counting transitions directly,
    rather than assuming "at most one", means the height formula stays
    correct even if that sort contract were ever violated or a third team
    value appeared — instead of silently clipping the last row by
    (n_gaps - 1) * TEAM_GAP.
    """
    return sum(1 for a, b in zip(players, players[1:], strict=False) if a.team != b.team)


def _outcome_text(stats: MatchStats) -> str:
    """Victory/Defeat is recorder-relative and meaningless without a
    recorder — dual/merged renders have none, so they get a neutral,
    team-numbered phrasing instead."""
    if stats.neutral_perspective:
        if stats.winner_team == 0:
            return "Team 1 wins"
        if stats.winner_team == 1:
            return "Team 2 wins"
        return "Draw"
    if stats.winner_team == 0:
        return "Victory"
    if stats.winner_team == 1:
        return "Defeat"
    return "Draw"


def _draw_title(cr: cairo.Context, stats: MatchStats, width: float, theme: str) -> None:
    outcome = _outcome_text(stats)
    if stats.winner_team in (0, 1):
        r, g, b, _a = THEMES[theme].team_colors[stats.winner_team]
    else:
        r, g, b = LABEL_PRIMARY

    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(FONT_SIZE * 1.6)
    cr.set_source_rgb(r, g, b)
    cr.move_to(PAD_X, _baseline_in(cr, 0.0, TITLE_H * 0.62))
    cr.show_text(outcome)

    subtitle = f"{stats.map_name}   •   {stats.game_type}   •   {_mmss(stats.duration_sec)}"
    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(FONT_SIZE)
    cr.set_source_rgb(*LABEL_SECONDARY)
    cr.move_to(PAD_X, _baseline_in(cr, TITLE_H * 0.58, TITLE_H * 0.42))
    cr.show_text(subtitle)

    cr.set_source_rgba(*LABEL_SECONDARY, 0.25)
    cr.set_line_width(1)
    cr.move_to(PAD_X, TITLE_H - 0.5)
    cr.line_to(width - PAD_X, TITLE_H - 0.5)
    cr.stroke()


def _draw_header(cr: cairo.Context, widths: list[float], xs: list[float], y: float) -> None:
    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(FONT_SIZE)
    baseline = _baseline_in(cr, y, HEADER_H)
    cr.set_source_rgb(*LABEL_SECONDARY)

    for col, x, w in zip(COLUMNS, xs, widths, strict=True):
        text = col.label
        tw = cr.text_extents(text).width
        tx = _cell_x(x, w, tw, col.align)
        cr.move_to(tx, baseline)
        cr.show_text(text)

    cr.set_source_rgba(*LABEL_SECONDARY, 0.4)
    cr.set_line_width(1)
    sep_y = y + HEADER_H - 0.5
    cr.move_to(PAD_X, sep_y)
    cr.line_to(PAD_X + sum(widths), sep_y)
    cr.stroke()


def _draw_row(
    cr: cairo.Context, player: PlayerStats, widths: list[float], xs: list[float], y: float, theme: str,
) -> None:
    table_w = sum(widths)

    r, g, b, a = _row_fill(player, theme)
    cr.set_source_rgba(r, g, b, a)
    cr.rectangle(PAD_X, y, table_w, ROW_H)
    cr.fill()

    if player.is_self:
        tr, tg, tb, _ta = THEMES[theme].team_colors[player.team]
        cr.set_source_rgba(tr, tg, tb, 0.9)
        cr.rectangle(PAD_X, y, 3, ROW_H)
        cr.fill()

    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(FONT_SIZE)
    baseline = _baseline_in(cr, y, ROW_H)

    for col, x, w in zip(COLUMNS, xs, widths, strict=True):
        text = col.fmt(player)
        color, alpha = _cell_style(col, player)
        if col.align == "left":
            text = _ellipsize(cr, text, w - COL_GAP)
        tw = cr.text_extents(text).width
        tx = _cell_x(x, w, tw, col.align)
        cr.set_source_rgba(color[0], color[1], color[2], alpha)
        cr.move_to(tx, baseline)
        cr.show_text(text)


def render_stats_board(stats: MatchStats, theme: str = "default") -> bytes:
    """Render the post-battle statistics board and return PNG bytes."""
    widths = _measure(stats)
    xs = _column_x(widths)
    width = sum(widths) + 2 * PAD_X
    height = (
        TITLE_H + HEADER_H + len(stats.players) * ROW_H
        + _team_gaps(stats.players) * TEAM_GAP + PAD_X
    )

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, round(width)), max(1, round(height)))
    cr = cairo.Context(surface)
    cr.set_source_rgb(*BG)
    cr.paint()

    _draw_title(cr, stats, width, theme)
    _draw_header(cr, widths, xs, TITLE_H)

    y: float = TITLE_H + HEADER_H
    prev_team: int | None = None
    for player in stats.players:
        if prev_team is not None and player.team != prev_team:
            y += TEAM_GAP
        _draw_row(cr, player, widths, xs, y, theme)
        y += ROW_H
        prev_team = player.team

    surface.flush()
    buf = io.BytesIO()
    surface.write_to_png(buf)
    return buf.getvalue()
