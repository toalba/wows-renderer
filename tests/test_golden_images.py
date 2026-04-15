"""Golden-image regression tests for the minimap renderer.

Rendering is non-trivial state — coordinate math, layer ordering, cairo text,
asset lookup all silently break in ways that unit tests miss. These tests
render a frame at a fixed timestamp against a baseline PNG committed to the
repo and flag any drift.

Fixture contract (provided by ``tests/conftest.py`` in parallel):

- ``fixture_replay_path`` — Path to a real ``.wowsreplay`` file or None/skip.
- ``fixture_gamedata_path`` — Path to resolved ``data/`` dir or None/skip.
- ``paired_fixture_paths`` (optional) — tuple[Path, Path] for dual render.

If any fixture is missing, tests skip cleanly with instructions on how to
generate reference images locally.
"""
from __future__ import annotations
import os
from pathlib import Path

import pytest

from tests.golden_image import (
    compare_images,
    load_reference,
    update_reference,
)


def _require_fixture(request: pytest.FixtureRequest, name: str):
    """Fetch a fixture from conftest; skip cleanly if it isn't defined."""
    try:
        value = request.getfixturevalue(name)
    except pytest.FixtureLookupError:
        pytest.skip(
            f"Fixture '{name}' not available — add a .wowsreplay + gamedata "
            f"under tests/fixtures/ (see tests/conftest.py)."
        )
    if value is None:
        pytest.skip(f"Fixture '{name}' resolved to None.")
    return value


def _build_config(gamedata_path: Path):
    from renderer.config import RenderConfig
    return RenderConfig(
        minimap_size=540,
        panel_width=200,
        fps=20,
        speed=10.0,
        gamedata_path=Path(gamedata_path),
    )


def _build_default_layers():
    """A representative, stable layer stack. Omits layers that depend on
    rarely-populated state (weather, trails) to keep comparison deterministic.
    """
    from renderer.layers.map_bg import MapBackgroundLayer
    from renderer.layers.team_roster import TeamRosterLayer
    from renderer.layers.capture_points import CapturePointLayer
    from renderer.layers.ships import ShipLayer
    from renderer.layers.health_bars import HealthBarLayer
    from renderer.layers.hud import HudLayer
    return [
        MapBackgroundLayer(),
        TeamRosterLayer(),
        CapturePointLayer(),
        ShipLayer(),
        HealthBarLayer(),
        HudLayer(),
    ]


def _parse_replay(replay_path: Path, gamedata_path: Path):
    from wows_replay_parser import parse_replay
    entity_defs = Path(gamedata_path) / "scripts_entity" / "entity_defs"
    return parse_replay(str(replay_path), str(entity_defs))


def _render_and_compare(replay, config, timestamp, name, layers, tmp_path):
    from renderer.frame_dump import render_frame_to_png
    actual = tmp_path / f"{name}.png"
    render_frame_to_png(replay, config, timestamp, actual, layers)

    if load_reference(name) is None:
        if os.environ.get("UPDATE_GOLDEN") == "1":
            update_reference(actual, name)
            pytest.skip(f"Wrote new reference: {name}.png")
        pytest.skip(
            f"No reference for '{name}'. Generate with:\n"
            f"  UPDATE_GOLDEN=1 pytest tests/test_golden_images.py -v"
        )

    passed, mse = compare_images(actual, name, threshold=0.005)
    assert passed, f"Golden-image drift for '{name}': mse={mse:.6f} (threshold 0.005)"


@pytest.mark.parametrize(
    ("timestamp", "name"),
    [
        (30.0, "single_t30"),
        (150.0, "single_t150"),
        (250.0, "single_t250"),
    ],
)
def test_single_render_golden(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    timestamp: float,
    name: str,
) -> None:
    replay_path = _require_fixture(request, "fixture_replay_path")
    gamedata_path = _require_fixture(request, "fixture_gamedata_path")

    replay = _parse_replay(Path(replay_path), Path(gamedata_path))
    if replay.duration < timestamp:
        pytest.skip(f"Replay duration {replay.duration:.1f}s < {timestamp}s")

    config = _build_config(Path(gamedata_path))
    layers = _build_default_layers()
    _render_and_compare(replay, config, timestamp, name, layers, tmp_path)


def test_dual_render_golden(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    paired = _require_fixture(request, "paired_fixture_paths")
    gamedata_path = _require_fixture(request, "fixture_gamedata_path")

    from wows_replay_parser import parse_replay
    from wows_replay_parser.merge import merge_replays
    from renderer.frame_dump import render_dual_frame_to_png
    from renderer.layers.map_bg import MapBackgroundLayer
    from renderer.layers.capture_points import CapturePointLayer
    from renderer.layers.ships import ShipLayer
    from renderer.layers.health_bars import HealthBarLayer
    from renderer.layers.hud import HudLayer

    replay_a_path, replay_b_path = paired
    entity_defs = Path(gamedata_path) / "scripts_entity" / "entity_defs"
    a = parse_replay(str(replay_a_path), str(entity_defs))
    b = parse_replay(str(replay_b_path), str(entity_defs))
    merged = merge_replays(a, b)

    config = _build_config(Path(gamedata_path))
    layers = [
        MapBackgroundLayer(),
        CapturePointLayer(),
        ShipLayer(),
        HealthBarLayer(),
        HudLayer(),
    ]
    name = "dual_t90"
    timestamp = 90.0

    if merged.duration < timestamp:
        pytest.skip(f"Merged replay duration {merged.duration:.1f}s < {timestamp}s")

    actual = tmp_path / f"{name}.png"
    render_dual_frame_to_png(merged, config, timestamp, actual, layers)

    if load_reference(name) is None:
        if os.environ.get("UPDATE_GOLDEN") == "1":
            update_reference(actual, name)
            pytest.skip(f"Wrote new reference: {name}.png")
        pytest.skip(
            f"No reference for '{name}'. Generate with:\n"
            f"  UPDATE_GOLDEN=1 pytest tests/test_golden_images.py -v"
        )

    passed, mse = compare_images(actual, name, threshold=0.005)
    assert passed, f"Golden-image drift for '{name}': mse={mse:.6f} (threshold 0.005)"
