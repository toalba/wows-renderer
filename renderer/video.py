from __future__ import annotations

import queue
import subprocess
import threading
from pathlib import Path


class FrameWriter:
    """Async frame writer that offloads pipe I/O to a background thread.

    The main thread calls write_frame() which copies the frame data and
    enqueues it. A background thread drains the queue into ffmpeg's stdin,
    so the main thread never blocks on pipe I/O.
    """

    def __init__(self, pipe: FFmpegPipe, maxsize: int = 8) -> None:
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
        """Copy frame data and enqueue for background writing."""
        if self._error:
            raise self._error
        self._queue.put(bytes(frame_data))

    def finish(self) -> None:
        """Signal the writer thread to stop and wait for it."""
        self._queue.put(None)
        self._thread.join()
        if self._error:
            raise self._error


class FFmpegPipe:
    """Pipes raw BGRA frames directly to ffmpeg's stdin for encoding.

    Cairo ARGB32 is BGRA in memory (little-endian). We feed this directly
    to ffmpeg — zero conversion, zero disk I/O.
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
        self.frame_count = 0

        cmd = [
            "ffmpeg",
            "-y",                        # Overwrite output
            "-f", "rawvideo",            # Raw input format
            "-pix_fmt", "bgra",          # Input pixel format (cairo ARGB32 = BGRA on LE)
            "-s", f"{width}x{height}",   # Frame size
            "-r", str(fps),              # Frame rate
            "-i", "pipe:0",             # Read from stdin
            "-c:v", codec,               # Video codec
            "-preset", "fast",           # Good speed/compression balance for flat graphics
            "-tune", "animation",        # Optimized for flat graphics / few colors
            "-threads", "0",             # Use all available cores
            "-crf", str(crf),            # Quality
            "-pix_fmt", "yuv420p",       # Output pixel format (Discord/browser compat)
            "-movflags", "+faststart",   # Web-optimized mp4
            str(output_path),
        ]

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def write_frame(self, frame_data: bytes | memoryview) -> None:
        """Write one raw BGRA frame to ffmpeg.

        Accepts bytes or memoryview (from cairo surface.get_data()).
        """
        assert self.proc.stdin is not None
        self.proc.stdin.write(frame_data)
        self.frame_count += 1

    def close(self) -> None:
        """Finalize the video file."""
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()
        if self.proc.returncode != 0:
            stderr = self.proc.stderr.read() if self.proc.stderr else b""
            raise RuntimeError(
                f"ffmpeg exited with code {self.proc.returncode}: "
                f"{stderr.decode(errors='replace')}"
            )

    def __enter__(self) -> FFmpegPipe:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
