"""Shared pytest fixtures for the renderer test suite.

All integration tests skip cleanly when no real replay / gamedata fixtures are
available — see ``tests/fixtures/README.md`` for the drop-in convention.

For backwards compatibility with existing dev checkouts, the legacy
top-of-repo ``*.wowsreplay`` files are accepted as a fallback — new work
should place fixtures under ``tests/fixtures/replays/`` instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
REPLAYS_DIR = FIXTURES_DIR / "replays"
FIXTURE_GAMEDATA_ROOT = FIXTURES_DIR / "gamedata"
OUTPUTS_DIR = FIXTURES_DIR / "outputs"

_LEGACY_GAMEDATA_ROOT = PROJECT_ROOT / "wows-gamedata" / "data"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _gamedata_data_root(root: Path) -> Path | None:
    """Return the data root if *root* looks like a valid gamedata tree.

    The renderer wants the top-level ``data/`` directory (it uses
    ``spaces/``, ``gui/``, ``ships.json``, etc.), not the narrow
    ``entity_defs`` subtree. Accept either a ``data/`` dir directly or a
    dir containing a ``scripts_entity`` subdir.
    """
    if not root.exists():
        return None
    if (root / "scripts_entity").is_dir():
        return root
    return None


def _find_fixture_replay() -> Path | None:
    if REPLAYS_DIR.is_dir():
        candidates = sorted(REPLAYS_DIR.glob("*.wowsreplay"))
        if candidates:
            return candidates[0]
    # Legacy fallback — any top-level .wowsreplay at the repo root.
    legacy = sorted(PROJECT_ROOT.glob("*.wowsreplay"))
    if legacy:
        return legacy[0]
    return None


def _find_fixture_gamedata() -> Path | None:
    hit = _gamedata_data_root(FIXTURE_GAMEDATA_ROOT)
    if hit is not None:
        return hit
    hit = _gamedata_data_root(_LEGACY_GAMEDATA_ROOT)
    if hit is not None:
        return hit
    return None


def _find_paired_replays() -> tuple[Path, Path] | None:
    if not REPLAYS_DIR.is_dir():
        return None
    paired = sorted(REPLAYS_DIR.glob("paired_*.wowsreplay"))
    if len(paired) >= 2:
        return paired[0], paired[1]
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fixture_replay_path() -> Path:
    """First sanitised replay found under ``tests/fixtures/replays/``."""
    replay = _find_fixture_replay()
    if replay is None:
        pytest.skip(
            "No replay fixture available — drop a sanitised .wowsreplay into "
            "tests/fixtures/replays/ (see tests/fixtures/README.md).",
        )
    return replay


@pytest.fixture(scope="session")
def fixture_gamedata_path() -> Path:
    """Full gamedata ``data/`` directory for the renderer."""
    gamedata = _find_fixture_gamedata()
    if gamedata is None:
        pytest.skip(
            "No gamedata fixture available — symlink tests/fixtures/gamedata "
            "to your wows-gamedata/data checkout (see tests/fixtures/README.md).",
        )
    return gamedata


@pytest.fixture
def paired_fixture_paths() -> tuple[Path, Path]:
    """Two ``paired_*.wowsreplay`` files from the same match, for dual-render tests."""
    paired = _find_paired_replays()
    if paired is None:
        pytest.skip(
            "No paired replay fixtures — drop two paired_*.wowsreplay files "
            "from the same match into tests/fixtures/replays/.",
        )
    return paired


@pytest.fixture
def fixture_rendered_output(
    tmp_path: Path,
    fixture_replay_path: Path,
    fixture_gamedata_path: Path,
) -> Path:
    """Produce a low-cost mp4 render of the fixture replay.

    Used by the smoke and future golden-image tests. The render config is
    intentionally cheap: small minimap, high speed, coarse fps, short
    duration — enough to exercise the full pipeline without waiting long.
    """
    from renderer.config import RenderConfig
    from renderer.core import MinimapRenderer
    from renderer.layers.hud import HudLayer
    from renderer.layers.map_bg import MapBackgroundLayer
    from renderer.layers.ships import ShipLayer

    output_path = tmp_path / "smoke.mp4"

    config = RenderConfig(
        gamedata_path=fixture_gamedata_path,
        speed=30.0,
        fps=10,
        minimap_size=480,
        panel_width=0,
        end_time=60.0,
    )
    renderer = MinimapRenderer.from_replay_file(
        fixture_replay_path,
        config,
    )
    # Minimal layer set — just enough to prove end-to-end pipeline works.
    for layer in [MapBackgroundLayer(), ShipLayer(), HudLayer()]:
        renderer.add_layer(layer)
    renderer.render(output_path=output_path)
    return output_path
