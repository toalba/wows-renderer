# tests/test_stats_export.py
"""Statistics extraction — the numeric formulas behind the stats board.

The fixture is a real 14-player BattleResults payload with identities
scrubbed and every stat preserved, so these assertions are regression
baselines against live game data rather than invented numbers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from wows_replay_parser.battle_results import BattleResults, _decode

FIXTURE = Path(__file__).parent / "fixtures" / "battle_results_sample.json"


@pytest.fixture(scope="module")
def battle_results() -> BattleResults:
    return _decode(json.loads(FIXTURE.read_text()))


def test_fixture_has_full_width_rows(battle_results):
    """Ribbon columns read raw[481 + ribbon_id]; a short row means the
    schema moved and the ribbon assertions below are meaningless."""
    assert len(battle_results.players) == 14
    for player in battle_results.players.values():
        assert len(player.raw) == 538


def test_damage_field_is_authoritative_not_the_sum(battle_results):
    """Trap A: `Σ damage_*` also catches damage_airdefense and
    damage_planes_by_plane, which are not ship damage. Five of the
    fourteen sample players diverge; assert on the widest gap."""
    from renderer.stats_export import ship_damage

    worst = max(
        battle_results.players.values(),
        key=lambda p: sum(
            v for k, v in p.stats.items()
            if k.startswith("damage_") and isinstance(v, (int, float))
        ) - (p.stat("damage") or 0),
    )
    naive = sum(
        v for k, v in worst.stats.items()
        if k.startswith("damage_") and isinstance(v, (int, float))
    )
    assert ship_damage(worst.stats) == worst.stat("damage")
    assert ship_damage(worst.stats) < naive


def test_received_damage_is_the_sum_not_hp_lost(battle_results):
    """Trap B: max_health - remained_hp under-reports any ship that
    healed. At least one sample player received more than they lost."""
    from renderer.stats_export import total_received_damage

    healed = [
        p for p in battle_results.players.values()
        if total_received_damage(p.stats)
        > (p.stat("max_health") or 0) - (p.stat("remained_hp") or 0)
    ]
    assert healed, "fixture should contain at least one player who healed"

    p = healed[0]
    expected = sum(
        v for k, v in p.stats.items()
        if k.startswith("received_damage_") and isinstance(v, (int, float))
    )
    assert total_received_damage(p.stats) == int(expected)


def test_received_damage_excludes_hits_and_module_fields(battle_results):
    """The prefix must not widen to `received_*`, which would fold in
    received_hits_* and received_module_* counts as if they were damage."""
    from renderer.stats_export import total_received_damage

    p = next(iter(battle_results.players.values()))
    stats = dict(p.stats)
    stats["received_hits_main_ap"] = 10_000_000
    stats["received_module_crits_engine"] = 10_000_000
    assert total_received_damage(stats) == total_received_damage(p.stats)


def test_potential_damage_sums_the_four_agro_fields(battle_results):
    from renderer.stats_export import potential_damage

    p = next(iter(battle_results.players.values()))
    expected = sum(p.stat(f"agro_{x}") or 0 for x in ("art", "tpd", "air", "dbomb"))
    assert potential_damage(p.stats) == int(expected)


def test_accuracy_is_none_when_main_guns_never_fired(battle_results):
    """Submarines and pure torpedo boats fire no main battery. Accuracy
    must be None (renders as a dash), never 0.0 or a ZeroDivisionError."""
    from renderer.stats_export import accuracy

    p = next(iter(battle_results.players.values()))
    stats = {k: 0 for k in p.stats}
    assert accuracy(stats) is None


def test_accuracy_matches_hits_over_shots(battle_results):
    from renderer.stats_export import accuracy, main_battery_hits, main_battery_shots

    shooters = [
        p for p in battle_results.players.values()
        if main_battery_shots(p.stats) > 0
    ]
    assert shooters, "fixture should contain at least one gunship"

    p = shooters[0]
    expected = 100.0 * main_battery_hits(p.stats) / main_battery_shots(p.stats)
    assert accuracy(p.stats) == pytest.approx(expected)


def test_ribbon_columns_read_the_tail_slots(battle_results):
    """Tuple order is (citadels, penetrations, overpens, shatters), pinned by
    exact assertion. Reads come from raw[481 + ribbon_id], not wire events."""
    from renderer.stats_export import ribbon_columns

    by_id = {p.db_id: ribbon_columns(p) for p in battle_results.players.values()}
    # All four values distinct and non-zero; any pairwise column swap fails here.
    assert by_id[100006] == (3, 25, 17, 1)  # (citadels, pens, overpens, shatters)
    # Cheap smoke check: at least one player landed citadels and penetrations.
    totals = list(by_id.values())
    assert any(t[0] > 0 for t in totals), "expected some citadels"
    assert any(t[1] > 0 for t in totals), "expected some penetrations"


def test_ribbon_columns_zero_out_on_short_rows():
    """If a patch moves the tail offset the row shortens; report zeros
    rather than whatever integer happens to sit at that index.

    The guard checks len(raw) >= BR_ROW_LEN to catch schema changes
    where the indices would be out of bounds or partially readable."""
    from wows_replay_parser.battle_results import PlayerBattleResult

    from renderer.stats_export import ribbon_columns

    # All indices out of bounds — still returns zeros with or without guard.
    truncated = PlayerBattleResult(db_id=1, stats={}, extra={}, raw=[0] * 400)
    assert ribbon_columns(truncated) == (0, 0, 0, 0)

    # Indices 489 in bounds, but 495/496/497 out of bounds. Without the guard
    # this would return (7, 0, 0, 0) — a partial mix. The guard ensures all-or-nothing.
    partial = PlayerBattleResult(db_id=2, stats={}, extra={}, raw=[7] * 492)
    assert ribbon_columns(partial) == (0, 0, 0, 0)


def _build(battle_results, **kw):
    from renderer.stats_export import build_match_stats

    defaults = dict(
        results=battle_results,
        ships_db={},
        self_team_id=0,
        meta={"map_name": "Tierra del Fuego", "game_type": "ClanBattle", "duration_sec": 581},
        flags=frozenset(),
    )
    defaults.update(kw)
    return build_match_stats(**defaults)


def test_players_are_grouped_by_display_team_then_damage_desc(battle_results):
    stats = _build(battle_results)
    teams = [p.team for p in stats.players]
    assert teams == sorted(teams), "team 0 block must precede team 1 block"

    for team in (0, 1):
        block = [p.damage for p in stats.players if p.team == team]
        assert block == sorted(block, reverse=True)


def test_self_team_becomes_display_team_zero(battle_results):
    """Trap 5: the recorder's raw team id is 0 or 1 depending on the
    replay. After the swap their team always renders as 0."""
    swapped = _build(battle_results, self_team_id=1)
    raw_team_1_names = {
        p.stat("name") for p in battle_results.players.values() if p.team_id == 1
    }
    display_0_names = {p.name for p in swapped.players if p.team == 0}
    assert display_0_names == raw_team_1_names


def test_anonymize_replaces_names_and_drops_clan_tags(battle_results):
    stats = _build(battle_results, flags=frozenset({"anonymize"}))
    assert all(p.name.startswith("Player ") for p in stats.players)
    assert all(p.clan_tag == "" for p in stats.players)
    assert len({p.name for p in stats.players}) == len(stats.players)


def test_killed_by_resolves_to_a_name_and_weapon(battle_results):
    """killer_db_id joins back to another row in the same payload."""
    stats = _build(battle_results)
    killed = [p for p in stats.players if p.killed_by]
    assert killed, "fixture should contain at least one dead player"
    assert all(p.killer_weapon for p in killed)


def test_survivors_have_no_killer(battle_results):
    stats = _build(battle_results)
    for p in stats.players:
        if p.hp_remaining > 0:
            assert p.killed_by == ""
            assert p.killer_weapon == ""


def test_unknown_ship_id_falls_back_to_the_raw_index(battle_results):
    stats = _build(battle_results, ships_db={})
    assert all(p.ship_name for p in stats.players)
    assert all(p.ship_class == "" for p in stats.players)


def test_ship_name_and_class_resolve_from_ships_db(battle_results):
    sample = next(iter(battle_results.players.values()))
    ship_id = int(sample.stat("vehicle_type_id"))
    db = {ship_id: {"name": "PFSC210_Marseille", "short_name": "Marseille",
                    "species": "Cruiser", "level": 10, "index": "PFSC210"}}
    stats = _build(battle_results, ships_db=db)
    hit = [p for p in stats.players if p.ship_name == "Marseille"]
    assert hit and hit[0].ship_class == "CA"


def test_extract_returns_none_without_a_results_packet():
    """Incomplete or crashed replays carry no 0x22 packet. The button
    hides itself on None, so this path must not raise."""
    from renderer.stats_export import extract_match_stats

    class _NoResults:
        def battle_results(self):
            return None

    assert extract_match_stats(_NoResults(), vgd=None) is None
