"""Direct unit tests for the encoder wrapper in renderer/video.py.

Validates that PyAVPipe produces a decodable mp4 at the configured
dimensions and frame count, without depending on the full render pipeline.
The end-to-end render path is covered by tests/test_smoke.py and the
golden-image suite — those will catch any regression in core.py wiring.
"""
from __future__ import annotations

from pathlib import Path

import av
import numpy as np

from renderer.video import PyAVPipe


def test_pyav_pipe_writes_decodable_mp4(tmp_path: Path) -> None:
    """Feed N solid-colour BGRA frames in, decode N frames out at correct size."""
    width, height, fps, n_frames = 320, 240, 20, 5
    output = tmp_path / "test.mp4"

    # Solid red BGRA frame (B=0, G=0, R=255, A=255)
    frame = np.zeros((height, width, 4), dtype=np.uint8)
    frame[..., 2] = 255  # R channel (BGRA in memory)
    frame[..., 3] = 255  # A channel
    frame_bytes = frame.tobytes()

    with PyAVPipe(output, width, height, fps=fps) as pipe:
        for _ in range(n_frames):
            pipe.write_frame(frame_bytes)

    assert output.exists()
    assert output.stat().st_size > 0

    container = av.open(str(output))
    try:
        stream = container.streams.video[0]
        assert stream.width == width
        assert stream.height == height
        assert stream.codec_context.name == "h264"
        decoded = list(container.decode(stream))
        assert len(decoded) == n_frames
    finally:
        container.close()
