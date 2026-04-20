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
            "-loglevel", "error",        # Silence banner + stream info; only print real errors
            "-nostats",                  # Suppress per-second progress lines on stderr
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
        # ffmpeg writes progress/status to stderr continuously. If we don't
        # drain it, the kernel pipe buffer (~64 KB) fills and ffmpeg blocks
        # on write, unable to exit — the whole worker then deadlocks in
        # self.proc.wait() below. Drain on a daemon thread so stderr never
        # stalls the encoder.
        self._stderr_chunks: list[bytes] = []
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True,
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        if self.proc.stderr is None:
            return
        for line in iter(self.proc.stderr.readline, b""):
            self._stderr_chunks.append(line)

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
        # Wait for the stderr drainer to finish reading all output (the pipe
        # closes when ffmpeg exits, so this should return very quickly).
        self._stderr_thread.join(timeout=10)
        if self.proc.returncode != 0:
            stderr = b"".join(self._stderr_chunks)
            raise RuntimeError(
                f"ffmpeg exited with code {self.proc.returncode}: "
                f"{stderr.decode(errors='replace')}"
            )

    def __enter__(self) -> FFmpegPipe:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
