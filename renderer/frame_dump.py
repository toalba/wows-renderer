"""Single-frame PNG dump helpers for the minimap renderer.

Bypasses the FFmpeg pipe and the full frame loop — renders exactly one
cairo surface at a given timestamp and writes it to PNG. Used by golden-image
regression tests and can be used for Discord embed thumbnails.
"""
from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

import cairo

from renderer.core import DualMinimapRenderer, MinimapRenderer

if TYPE_CHECKING:
    from renderer.config import RenderConfig
    from renderer.layers.base import Layer
    from wows_replay_parser.interfaces import ReplaySource
    from wows_replay_parser.merge import MergedReplay


def _render_once(
    renderer: MinimapRenderer | DualMinimapRenderer,
    replay: "ReplaySource",
    timestamp: float,
    output_path: Path,
    layers: list["Layer"],
) -> Path:
    config = renderer.config
    for layer in layers:
        renderer.add_layer(layer)

    gp = config.effective_gamedata_path
    render_ctx = renderer._build_context(replay, gp)
    for layer in renderer.layers:
        layer.initialize(render_ctx)

    width = config.total_width
    height = config.total_height
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)

    state = replay.state_at(timestamp)

    cr.save()
    cr.set_operator(cairo.OPERATOR_CLEAR)
    cr.paint()
    cr.restore()

    for layer in renderer.layers:
        cr.save()
        layer.render(cr, state, timestamp)
        cr.restore()

    surface.flush()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    surface.write_to_png(str(output_path))
    return output_path


def render_frame_to_png(
    replay: "ReplaySource",
    config: "RenderConfig",
    timestamp: float,
    output_path: str | Path,
    layers: list["Layer"],
) -> Path:
    """Render one frame at ``timestamp`` and write it to ``output_path`` as PNG."""
    renderer = MinimapRenderer(config)
    return _render_once(renderer, replay, timestamp, Path(output_path), layers)


def render_dual_frame_to_png(
    merged: "MergedReplay",
    config: "RenderConfig",
    timestamp: float,
    output_path: str | Path,
    layers: list["Layer"],
) -> Path:
    """Dual-render variant — one frame from a merged replay to PNG."""
    renderer = DualMinimapRenderer(config)
    return _render_once(renderer, merged, timestamp, Path(output_path), layers)
