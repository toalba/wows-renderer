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


def test_golden_image(tmp_path):
    from renderer.stats_board import render_stats_board
    from tests.golden_image import compare_images, load_reference

    if load_reference("stats_board") is None:
        pytest.skip("no baseline yet — generate with UPDATE_GOLDEN=1")

    out = tmp_path / "stats_board.png"
    out.write_bytes(render_stats_board(_match()))
    passed, mse = compare_images(out, "stats_board")
    assert passed, f"stats board drifted from baseline (mse={mse:.5f})"
