"""The death-reason table is shared by the killfeed layer and the stats
board. These tests pin the ids that both consumers depend on."""
from __future__ import annotations

from renderer.death_reasons import DEATH_REASON, death_reason_label


def test_known_ids_have_labels():
    # 18 = HE_SHELL. The sample replay's killer_weapon field uses it.
    assert death_reason_label(18) == "HE"
    assert death_reason_label(3) == "TORP"
    assert death_reason_label(6) == "FIRE"
    assert death_reason_label(7) == "RAM"


def test_unknown_id_returns_empty_string():
    assert death_reason_label(9999) == ""
    assert death_reason_label(0) == ""


def test_killfeed_uses_the_shared_table():
    """Guard against the table being re-forked into killfeed.py later."""
    from renderer.layers import killfeed

    assert killfeed._DEATH_REASON is DEATH_REASON
