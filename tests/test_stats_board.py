# tests/test_stats_board.py
"""Statistics board rendering.

The board is pure presentation: given a MatchStats it produces PNG bytes
with no parser, gamedata or Discord involvement. These tests hold that
boundary and guard the layout against regressions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from renderer.stats_export import MatchStats, PlayerStats

pytest.importorskip("cairo")


def _player(name: str, team: int, damage: int, **kw) -> PlayerStats:
    base = dict(
        name=name, clan_tag="CL0", ship_name="Marseille", ship_class="CA",
        team=team, is_self=False, damage=damage, received=50_000,
        spotting=6_336, potential=1_077_000, kills=1, hits=46, shots=120,
        accuracy=38.3, fires=2, floods=5, citadels=1, penetrations=13,
        overpens=3, shatters=11, crits=5, major_crits=2, module_breaks=1,
        caps=12, caps_reset=4, first_spots=3, torps_spotted=7,
        planes_killed=14, aa_damage=9_000, distance_km=54.0, xp=1_805,
        hp_remaining=0, max_health=75_050, life_time_sec=635,
        killed_by="Player09", killer_weapon="HE",
    )
    base.update(kw)
    return PlayerStats(**base)


def _match(n: int = 6) -> MatchStats:
    rows = [
        _player(f"Player{i:02d}", i % 2, 150_000 - i * 10_000)
        for i in range(n)
    ]
    rows.sort(key=lambda r: (r.team, -r.damage))
    return MatchStats(
        players=tuple(rows), map_name="Tierra del Fuego",
        game_type="ClanBattle", duration_sec=581, winner_team=0,
        neutral_perspective=False,
    )


def test_returns_valid_png_bytes():
    from renderer.stats_board import render_stats_board

    data = render_stats_board(_match())
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_empty_match_does_not_crash():
    """A results packet with no usable rows must render a header-only
    sheet rather than raising out of the button handler."""
    from renderer.stats_board import render_stats_board

    empty = MatchStats(
        players=(), map_name="", game_type="Unknown", duration_sec=0,
        winner_team=-1, neutral_perspective=False,
    )
    assert render_stats_board(empty)[:8] == b"\x89PNG\r\n\x1a\n"


def test_accuracy_none_renders_without_error():
    """Submarines have accuracy=None; the formatter must not choke."""
    from renderer.stats_board import render_stats_board

    m = _match()
    rows = list(m.players)
    rows[0] = _player("Sub", 0, 99_000, accuracy=None, shots=0, hits=0)
    import dataclasses
    assert render_stats_board(dataclasses.replace(m, players=tuple(rows)))


def test_board_module_has_no_parser_or_discord_import():
    """The presentation boundary is the point of the split — hold it.

    Scoped to import lines on purpose: a raw substring scan would trip on
    the module docstring, which names the very things it does not import.
    """
    src = Path("renderer/stats_board.py").read_text()
    imports = "\n".join(
        line for line in src.splitlines()
        if line.startswith(("import ", "from "))
    )
    assert "wows_replay_parser" not in imports
    assert "discord" not in imports
    assert "gamedata" not in imports


def test_theme_changes_output():
    """Team colours must come from THEMES, not be hardcoded."""
    from renderer.stats_board import render_stats_board

    assert render_stats_board(_match(), theme="default") != \
        render_stats_board(_match(), theme="brandon")


def test_long_name_is_capped_and_ellipsised():
    """A long clan tag + player name (and a long ship name) must not widen
    the sheet past MAX_TEXT_CELL_W, and the overflow must be shortened
    with a trailing ellipsis rather than bleeding into the next column.

    None of the other fixtures use content long enough to reach the cap,
    so without this test the cap/ellipsis mechanism has zero coverage.
    """
    import cairo

    from renderer.stats_board import (
        COL_GAP,
        COLUMNS,
        FONT,
        FONT_SIZE,
        MAX_TEXT_CELL_W,
        _ellipsize,
        _measure,
        render_stats_board,
    )

    long_player = _player(
        "SuperLongPlayerNameThatIsDefinitelyLongEnoughToOverflowTheColumn",
        0, 100_000,
        clan_tag="REALLYLONGCLANTAG12345",
        ship_name="AnExtremelyLongShipNameForTestingColumnOverflowBehaviour",
    )
    m = MatchStats(
        players=(long_player,), map_name="Map", game_type="Random",
        duration_sec=100, winner_team=0, neutral_perspective=False,
    )

    name_idx = next(i for i, c in enumerate(COLUMNS) if c.key == "name")
    ship_idx = next(i for i, c in enumerate(COLUMNS) if c.key == "ship_name")

    # The cap keeps these columns from growing with unbounded content —
    # this is the property that keeps one long name from widening the
    # whole sheet. Asserted against the constant, not a hardcoded 278.0,
    # so a deliberate future change to MAX_TEXT_CELL_W doesn't break this.
    widths = _measure(m)
    assert widths[name_idx] == pytest.approx(MAX_TEXT_CELL_W + COL_GAP)
    assert widths[ship_idx] == pytest.approx(MAX_TEXT_CELL_W + COL_GAP)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    cr = cairo.Context(surface)
    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(FONT_SIZE)

    full_text = COLUMNS[name_idx].fmt(long_player)
    # Sanity-check the fixture actually overflows the cap; otherwise the
    # assertions below would pass trivially without exercising anything.
    assert cr.text_extents(full_text).width > MAX_TEXT_CELL_W

    truncated = _ellipsize(cr, full_text, MAX_TEXT_CELL_W)
    assert truncated != full_text
    assert truncated.endswith("…")
    assert cr.text_extents(truncated).width <= MAX_TEXT_CELL_W

    # And the whole thing still renders to a valid PNG with no crash.
    assert render_stats_board(m)[:8] == b"\x89PNG\r\n\x1a\n"


def test_killed_by_column_renders_the_name_only():
    """Superseded behaviour, pinned deliberately.

    An earlier round rendered "Player09 (HE)" here, following the design
    spec's "who killed them and with what". Seen on a real board that made
    Killed by the widest column on the sheet, so the weapon was dropped.
    `killer_weapon` is still carried on PlayerStats for any future consumer.

    The accepted cost: a fire/flood/terrain death (killed_by="", weapon set)
    renders "—" exactly like a survivor, and only the HP column separates
    them. That is why this asserts the empty case explicitly rather than
    leaving it implied.
    """
    from renderer.stats_board import COLUMNS

    fmt = next(c.fmt for c in COLUMNS if c.key == "killed_by")

    survivor = _player("Survivor", 0, 1_000, killed_by="", killer_weapon="", hp_remaining=1_000)
    assert fmt(survivor) == "—"

    burned = _player("Burned", 0, 1_000, killed_by="", killer_weapon="FIRE", hp_remaining=0)
    assert fmt(burned) == "—"

    named_kill = _player("Sunk", 0, 1_000, killed_by="Player09", killer_weapon="HE", hp_remaining=0)
    assert fmt(named_kill) == "Player09"


def test_golden_image(tmp_path):
    from renderer.stats_board import render_stats_board
    from tests.golden_image import compare_images, load_reference

    if load_reference("stats_board") is None:
        pytest.skip("no baseline yet — generate with UPDATE_GOLDEN=1")

    out = tmp_path / "stats_board.png"
    out.write_bytes(render_stats_board(_match()))
    passed, mse = compare_images(out, "stats_board")
    assert passed, f"stats board drifted from baseline (mse={mse:.5f})"


def _fake_icon(tmp_path, rid: int):
    """A 1x1 PNG standing in for a ribbon, so layout tests need no gamedata."""
    import cairo as _c

    p = tmp_path / f"ribbon_{rid}.png"
    surf = _c.ImageSurface(_c.FORMAT_ARGB32, 4, 4)
    surf.write_to_png(str(p))
    return (rid, str(p))


def _match_with_ribbons(tmp_path, n=6):
    """A match whose ribbons are readable and whose icons exist on disk."""
    import dataclasses

    from renderer.stats_board import STRIP_RIBBON_IDS

    m = _match(n)
    players = tuple(
        dataclasses.replace(p, ribbons=((5, 2), (8, 3), (0, 41)))
        for p in m.players
    )
    return dataclasses.replace(
        m, players=players, ribbons_available=True,
        ribbon_icons=tuple(_fake_icon(tmp_path, r) for r in STRIP_RIBBON_IDS),
    )


def test_compact_layout_is_narrower_than_detailed(tmp_path):
    """The compact board trades 18 columns for a ribbon strip; if it were
    not actually narrower the trade would be pointless."""
    from PIL import Image

    from renderer.stats_board import render_stats_board

    m = _match_with_ribbons(tmp_path)
    compact = tmp_path / "c.png"
    detailed = tmp_path / "d.png"
    compact.write_bytes(render_stats_board(m, layout="compact"))
    detailed.write_bytes(render_stats_board(m, layout="detailed"))
    assert Image.open(compact).width < Image.open(detailed).width


def test_falls_back_to_detailed_when_ribbons_unreadable(tmp_path):
    """Pre-15.3 replays: rather than draw an empty strip — indistinguishable
    from a player who earned nothing — show the full column set, which needs
    no gamedata at all."""
    import dataclasses

    from renderer.stats_board import render_stats_board

    m = _match_with_ribbons(tmp_path)
    unreadable = dataclasses.replace(m, ribbons_available=False)
    assert render_stats_board(unreadable, layout="compact") == \
        render_stats_board(unreadable, layout="detailed")


def test_falls_back_to_detailed_when_no_icons_resolved(tmp_path):
    """Same fallback when gamedata is present but the icons are not."""
    import dataclasses

    from renderer.stats_board import render_stats_board

    m = dataclasses.replace(_match_with_ribbons(tmp_path), ribbon_icons=())
    assert render_stats_board(m, layout="compact") == \
        render_stats_board(m, layout="detailed")


def test_unreadable_icon_file_does_not_break_the_render(tmp_path):
    """A corrupt PNG must degrade the strip, not raise out of the button."""
    import dataclasses

    from renderer.stats_board import render_stats_board

    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    m = dataclasses.replace(_match_with_ribbons(tmp_path),
                            ribbon_icons=((5, str(bad)),))
    assert render_stats_board(m, layout="compact")[:8] == b"\x89PNG\r\n\x1a\n"


def test_unknown_layout_is_rejected(tmp_path):
    from renderer.stats_board import render_stats_board

    with pytest.raises(ValueError, match="unknown layout"):
        render_stats_board(_match_with_ribbons(tmp_path), layout="sideways")


def test_strip_excludes_sub_ribbons():
    """Pen/overpen/shatter/ricochet carry the largest counts and dominated
    the strip's width; they were dropped on purpose."""
    from renderer.stats_board import STRIP_RIBBON_IDS

    sub_ribbon_ids = {14, 15, 16, 17, 20, 21, 22, 23, 25, 26, 28, 29, 30, 34, 35}
    assert not (set(STRIP_RIBBON_IDS) & sub_ribbon_ids)


def test_killed_by_shows_the_name_without_the_weapon():
    """The weapon label made this the widest column on the board."""
    from renderer.stats_board import _killed_by_text

    p = _player("x", 0, 1, killed_by="Rammer", killer_weapon="ARTILLERY")
    assert _killed_by_text(p) == "Rammer"

    import dataclasses
    assert _killed_by_text(dataclasses.replace(p, killed_by="")) == "—"


def test_compact_columns_are_a_subset_of_detailed():
    """COMPACT_COLUMNS selects from COLUMNS rather than redefining them, so
    the detailed layout stays available and the two can't drift."""
    from renderer.stats_board import COLUMNS, COMPACT_COLUMNS

    assert len(COMPACT_COLUMNS) == 11
    assert all(c in COLUMNS for c in COMPACT_COLUMNS)


def test_ribbons_header_is_not_clipped_on_a_sparse_match(tmp_path):
    """Regression: the "Ribbons" header is drawn at FONT_SIZE but was
    measured at STRIP_COUNT_FONT, because _strip_width leaves the probe
    context at the smaller size. When every player's strip is narrower than
    the label — a match where nobody earned a tracked ribbon — that label
    is what sizes the canvas, so it was cut off at the right edge.

    Asserts on rendered pixels rather than on the measurement, so it fails
    for a clipped header however the clipping arises.
    """
    import dataclasses

    from PIL import Image

    from renderer.stats_board import STRIP_RIBBON_IDS, render_stats_board

    # id 54 (Assist) is real but not in STRIP_RIBBON_IDS, so every strip
    # renders empty and the header alone determines the width.
    m = _match_with_ribbons(tmp_path)
    m = dataclasses.replace(
        m, players=tuple(dataclasses.replace(p, ribbons=((54, 1),)) for p in m.players),
    )
    assert 54 not in STRIP_RIBBON_IDS

    out = tmp_path / "sparse.png"
    out.write_bytes(render_stats_board(m, layout="compact"))
    img = Image.open(out).convert("RGBA")

    # The rightmost column must be untouched background: any ink there means
    # content ran off the edge.
    bg = img.getpixel((img.width - 1, 0))
    edge = [img.getpixel((img.width - 1, y)) for y in range(img.height)]
    assert all(px == bg for px in edge), "content touches the right edge"


def test_golden_image_compact(tmp_path):
    """Pixel baseline for the compact layout — the detailed golden never
    exercises the strip, which is how the header-clipping bug above reached
    a green suite of 127 tests."""
    from renderer.stats_board import render_stats_board
    from tests.golden_image import compare_images, load_reference

    if load_reference("stats_board_compact") is None:
        pytest.skip("no baseline yet — generate with UPDATE_GOLDEN=1")

    out = tmp_path / "compact.png"
    out.write_bytes(render_stats_board(_match_with_ribbons(tmp_path), layout="compact"))
    passed, mse = compare_images(out, "stats_board_compact")
    assert passed, f"compact board drifted from baseline (mse={mse:.5f})"
