from __future__ import annotations

import queue
import threading
from fractions import Fraction
from pathlib import Path

import av
import numpy as np


class FrameWriter:
    """Async frame writer that offloads encoding to a background thread.

    The main thread calls write_frame() which copies the frame data and
    enqueues it. A background thread drains the queue into the encoder, so
    the main thread never blocks on the (synchronous) PyAV encode call.

    The bytes() copy on enqueue is load-bearing: it detaches from the
    Cairo surface buffer that the main thread will overwrite on the next
    frame.
    """

    def __init__(self, pipe: PyAVPipe, maxsize: int = 8) -> None:
        self._pipe = pipe
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=maxsize)
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()

    def _writer_loop(self) -> None:
        try:
            while True:
                frame = self._queue.get()
                if frame is None:
                    break
                self._pipe.write_frame(frame)
        except Exception as e:
            self._error = e

    def write_frame(self, frame_data: bytes | memoryview) -> None:
        if self._error:
            raise self._error
        self._queue.put(bytes(frame_data))

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
            "threads": "0",
        }
        self._time_base = Fraction(1, fps)

    def write_frame(self, frame_data: bytes | memoryview) -> None:
        """Encode one raw BGRA frame.

        Accepts bytes or memoryview from cairo surface.get_data().
        """
        arr = np.frombuffer(frame_data, dtype=np.uint8).reshape(
            self.height, self.width, 4,
        )
        frame = av.VideoFrame.from_ndarray(arr, format="bgra")
        frame.pts = self.frame_count
        frame.time_base = self._time_base
        for packet in self._stream.encode(frame):
            self._container.mux(packet)
        self.frame_count += 1

    def close(self) -> None:
        """Flush encoder lookahead queue and finalise mp4."""
        for packet in self._stream.encode(None):
            self._container.mux(packet)
        self._container.close()

    def __enter__(self) -> PyAVPipe:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
