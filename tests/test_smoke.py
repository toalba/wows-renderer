"""End-to-end smoke tests.

These tests require a real sanitised replay + a full gamedata tree. When
either is missing the fixtures in ``conftest.py`` emit ``pytest.skip(...)``
and these tests disappear from the run without error. See
``tests/fixtures/README.md`` for how to populate the fixtures.

These are *smoke* tests — they prove the pipeline runs and writes a
non-empty mp4. Pixel-accurate / visual regression testing is a separate
concern (planned: golden-image tests building on top of
``fixture_rendered_output``).
"""

from __future__ import annotations

from pathlib import Path

from renderer.config import RenderConfig
from renderer.core import DualMinimapRenderer, MinimapRenderer


def test_parse_via_renderer_pipeline(
    fixture_replay_path: Path,
    fixture_gamedata_path: Path,
) -> None:
    """``MinimapRenderer.from_replay_file`` loads the fixture end-to-end."""
    config = RenderConfig(
        gamedata_path=fixture_gamedata_path,
        minimap_size=480,
        panel_width=0,
    )
    renderer = MinimapRenderer.from_replay_file(fixture_replay_path, config)
    assert renderer.replay is not None
    assert renderer.replay.duration > 0
    assert renderer.replay.map_name


def test_render_produces_output(fixture_rendered_output: Path) -> None:
    """A full smoke render produces a non-empty mp4."""
    assert fixture_rendered_output.exists()
    assert fixture_rendered_output.suffix == ".mp4"
    assert fixture_rendered_output.stat().st_size > 0


def test_dual_render_smoke(
    tmp_path: Path,
    paired_fixture_paths: tuple[Path, Path],
    fixture_gamedata_path: Path,
) -> None:
    """End-to-end dual render produces a non-empty mp4.

    Skips unless two ``paired_*.wowsreplay`` files are available.
    """
    from renderer.layers.hud import HudLayer
    from renderer.layers.map_bg import MapBackgroundLayer
    from renderer.layers.ships import ShipLayer

    replay_a, replay_b = paired_fixture_paths
    output_path = tmp_path / "dual_smoke.mp4"

    config = RenderConfig(
        gamedata_path=fixture_gamedata_path,
        speed=30.0,
        fps=10,
        minimap_size=480,
        panel_width=0,
        end_time=60.0,
    )
    renderer = DualMinimapRenderer.from_replay_files(
        replay_a,
        replay_b,
        config,
    )
    for layer in [MapBackgroundLayer(), ShipLayer(), HudLayer()]:
        renderer.add_layer(layer)
    renderer.render(output_path=output_path)

    assert output_path.exists()
    assert output_path.stat().st_size > 0
