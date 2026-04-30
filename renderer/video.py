from __future__ import annotations

import os
import queue
import threading
from fractions import Fraction
from pathlib import Path

import av


class FrameWriter:
    """Async frame writer that offloads encoding to a background thread.

    Caller passes (memoryview, release_event) per frame. The view points
    into a caller-owned buffer (typically a Cairo surface). The writer
    thread reads the view into the encoder, then sets release_event so
    the caller knows the buffer is safe to reuse.

    This zero-copy handoff requires the caller to manage a small pool of
    buffers (typically 2) and only paint to a buffer once its release
    event has fired. See BaseMinimapRenderer._render_frames for the
    double-buffered Cairo-surface pattern.
    """

    def __init__(self, pipe: PyAVPipe, maxsize: int = 4) -> None:
        self._pipe = pipe
        self._queue: queue.Queue[
            tuple[memoryview | bytes, threading.Event] | None
        ] = queue.Queue(maxsize=maxsize)
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()

    def _writer_loop(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                frame_data, release_event = item
                self._pipe.write_frame(frame_data)
                release_event.set()
        except Exception as e:
            self._error = e

    def write_frame(
        self,
        frame_data: memoryview | bytes,
        release_event: threading.Event | None = None,
    ) -> None:
        """Hand a frame to the writer thread.

        With release_event=None (legacy, used by profile_frames.py) the
        bytes are copied so the caller can immediately reuse the source
        buffer. With release_event provided (zero-copy fast path) the
        caller MUST wait on release_event before reusing the buffer.
        """
        if self._error:
            raise self._error
        if release_event is None:
            release_event = threading.Event()
            frame_data = bytes(frame_data)
        self._queue.put((frame_data, release_event))

    def finish(self) -> None:
        self._queue.put(None)
        self._thread.join()
        if self._error:
            raise self._error


class PyAVPipe:
    """In-process H.264 encoder via PyAV.

    Cairo ARGB32 (BGRA in memory on little-endian) is fed directly to
    libswscale via VideoFrame.from_ndarray — no rawvideo pipe, no
    subprocess, no stderr drainer.

    Quality-affecting x264 settings match the previous FFmpegPipe
    invocation exactly: preset=fast, tune=animation, crf=23, yuv420p
    output, +faststart for web playback.
    """

    def __init__(
        self,
        output_path: str | Path,
        width: int,
        height: int,
        fps: int = 20,
        crf: int = 23,
        codec: str = "libx264",
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_count = 0

        self._container = av.open(
            str(output_path),
            mode="w",
            format="mp4",
            options={"movflags": "+faststart"},
        )
        self._stream = self._container.add_stream(codec, rate=fps)
        self._stream.width = width
        self._stream.height = height
        self._stream.pix_fmt = "yuv420p"
        self._stream.options = {
            "preset": "fast",
            "tune": "animation",
            "crf": str(crf),
            "threads": os.environ.get("PYAV_X264_THREADS", "0"),
        }
        self._time_base = Fraction(1, fps)
        # Reusable VideoFrame — skip per-frame allocation that
        # av.VideoFrame.from_ndarray would do. We update plane 0 in place.
        self._frame = av.VideoFrame(width, height, "bgra")
        self._frame.time_base = self._time_base

    def write_frame(self, frame_data: bytes | memoryview) -> None:
        """Encode one raw BGRA frame.

        Accepts bytes or memoryview from cairo surface.get_data().
        """
        self._frame.planes[0].update(frame_data)
        self._frame.pts = self.frame_count
        for packet in self._stream.encode(self._frame):
            self._container.mux(packet)
        self.frame_count += 1

    def close(self) -> None:
        """Flush encoder lookahead queue and finalise mp4."""
        try:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
        finally:
            self._container.close()

    def __enter__(self) -> PyAVPipe:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
