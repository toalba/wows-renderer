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
    """Citadels/pens/overpens/shatters come from raw[481 + ribbon_id],
    not from summing wire events."""
    from renderer.stats_export import ribbon_columns

    totals = [ribbon_columns(p) for p in battle_results.players.values()]
    # The sample is a clan battle; at least one player landed citadels
    # and at least one landed penetrations.
    assert any(t[0] > 0 for t in totals), "expected some citadels"
    assert any(t[1] > 0 for t in totals), "expected some penetrations"


def test_ribbon_columns_zero_out_on_short_rows():
    """If a patch moves the tail offset the row shortens; report zeros
    rather than whatever integer happens to sit at that index."""
    from wows_replay_parser.battle_results import PlayerBattleResult

    from renderer.stats_export import ribbon_columns

    truncated = PlayerBattleResult(db_id=1, stats={}, extra={}, raw=[0] * 400)
    assert ribbon_columns(truncated) == (0, 0, 0, 0)
