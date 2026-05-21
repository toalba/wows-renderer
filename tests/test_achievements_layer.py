"""Unit tests for AchievementLayer.

Avoids Cairo entirely by stopping after initialize() — we just inspect the
internal timeline and seen-order state. End-to-end render coverage lives in
the smoke / golden-image suites.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from wows_replay_parser.events.models import AchievementEvent

from renderer.layers.achievements import AchievementLayer


@dataclass
class _StubPlayer:
    account_id: int
    relation: int
    team_id: int = 0
    name: str = "stub"


@dataclass
class _StubReplay:
    events: list = field(default_factory=list)


@dataclass
class _StubConfig:
    effective_gamedata_path: Path
    versioned_gamedata: object | None
    minimap_size: int = 760
    hud_height: int = 24


@dataclass
class _StubVGD:
    achievements: dict
    version_dir: Path = field(default_factory=lambda: Path("/nonexistent"))


@dataclass
class _StubCtx:
    config: _StubConfig
    replay: _StubReplay
    player_lookup: dict

    @property
    def scale(self) -> float:
        return 1.0


def _make_ctx(
    tmp_path: Path,
    events: list,
    players: dict[int, _StubPlayer],
    achievements: dict[int, str] | None = None,
) -> _StubCtx:
    (tmp_path / "gui" / "achievements").mkdir(parents=True)
    vgd = _StubVGD(achievements=achievements or {})
    cfg = _StubConfig(
        effective_gamedata_path=tmp_path,
        versioned_gamedata=vgd,
    )
    return _StubCtx(
        config=cfg,
        replay=_StubReplay(events=events),
        player_lookup=players,
    )


def test_initialize_filters_to_recording_player(tmp_path: Path):
    self_player = _StubPlayer(account_id=111, relation=0)
    other = _StubPlayer(account_id=222, relation=1)
    events = [
        AchievementEvent(timestamp=10.0, entity_id=1, player_id=111, achievement_id=1),
        AchievementEvent(timestamp=20.0, entity_id=1, player_id=222, achievement_id=2),
        AchievementEvent(timestamp=30.0, entity_id=1, player_id=111, achievement_id=3),
    ]
    ctx = _make_ctx(tmp_path, events, {1: self_player, 2: other})
    layer = AchievementLayer()
    layer.initialize(ctx)
    assert layer._timeline == [(10.0, 1), (30.0, 3)]


def test_initialize_keeps_duplicates_in_timeline(tmp_path: Path):
    self_player = _StubPlayer(account_id=111, relation=0)
    events = [
        AchievementEvent(timestamp=10.0, entity_id=1, player_id=111, achievement_id=1),
        AchievementEvent(timestamp=20.0, entity_id=1, player_id=111, achievement_id=1),
    ]
    ctx = _make_ctx(tmp_path, events, {1: self_player})
    layer = AchievementLayer()
    layer.initialize(ctx)
    # Timeline keeps both entries — de-dup happens during render walk
    # so timestamps remain available for analysis.
    assert layer._timeline == [(10.0, 1), (20.0, 1)]


def test_initialize_with_no_self_player_gives_empty_timeline(tmp_path: Path):
    other = _StubPlayer(account_id=222, relation=1)
    events = [
        AchievementEvent(timestamp=10.0, entity_id=1, player_id=222, achievement_id=2),
    ]
    ctx = _make_ctx(tmp_path, events, {2: other})
    layer = AchievementLayer()
    layer.initialize(ctx)
    assert layer._timeline == []


def test_initialize_handles_missing_versioned_gamedata(tmp_path: Path):
    """When versioned_gamedata is None we don't crash; achievement_names stays empty."""
    self_player = _StubPlayer(account_id=111, relation=0)
    events = [AchievementEvent(timestamp=10.0, entity_id=1, player_id=111, achievement_id=1)]
    (tmp_path / "gui" / "achievements").mkdir(parents=True)
    cfg = _StubConfig(
        effective_gamedata_path=tmp_path,
        versioned_gamedata=None,
    )
    ctx = _StubCtx(
        config=cfg,
        replay=_StubReplay(events=events),
        player_lookup={1: self_player},
    )
    layer = AchievementLayer()
    layer.initialize(ctx)
    assert layer._achievement_names == {}


def test_panel_bottom_zero_before_first_render(tmp_path: Path):
    self_player = _StubPlayer(account_id=111, relation=0)
    events: list = []
    ctx = _make_ctx(tmp_path, events, {1: self_player})
    layer = AchievementLayer()
    layer.initialize(ctx)
    # Before any render call, panel_bottom is unset (class-level default).
    assert layer.panel_bottom == 0.0
