from __future__ import annotations
import subprocess
from pathlib import Path


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

    def write_frame(self, bgra_bytes: bytes) -> None:
        """Write one raw BGRA frame to ffmpeg."""
        expected = self.width * self.height * 4
        if len(bgra_bytes) != expected:
            raise ValueError(
                f"Frame size mismatch: got {len(bgra_bytes)} bytes, "
                f"expected {expected} ({self.width}x{self.height}x4)"
            )
        assert self.proc.stdin is not None
        self.proc.stdin.write(bgra_bytes)
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
