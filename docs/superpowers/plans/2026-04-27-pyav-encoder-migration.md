# PyAV Encoder Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `FFmpegPipe` subprocess encoder in [renderer/video.py](../../../renderer/video.py) with an in-process PyAV encoder, drop the system-level `ffmpeg` dependency, and benchmark old-vs-new across three representative replays.

**Architecture:** A new `PyAVPipe` class with the same public surface as `FFmpegPipe` is dropped in at the single call site in [renderer/core.py](../../../renderer/core.py). The async `FrameWriter` is preserved (encoding still blocks the calling thread on `stream.encode()`, so the background queue still buys parallelism). Cairo's BGRA buffer goes directly to PyAV via `VideoFrame.from_ndarray(format="bgra")` — no rawvideo pipe, no subprocess. Quality-affecting x264 settings (`preset=fast`, `tune=animation`, `crf=23`, `pix_fmt=yuv420p`, `+faststart`) are preserved exactly. The benchmark is a subprocess-per-trial driver that captures wall time, render/encode phase split, peak RSS (read by the worker via `RUSAGE_SELF`), and output size; results land in a CSV labeled by branch and a markdown comparison doc.

**Tech Stack:** Python 3.12, PyAV 17.x (already installed), numpy (added), libx264 (bundled in PyAV wheel), Cairo (unchanged), pytest (unchanged).

**Working dir:** `/home/toalba/projects/wows/wows-renderer/` on branch `PyAV`. Spec at [docs/superpowers/specs/2026-04-27-pyav-encoder-migration-design.md](../specs/2026-04-27-pyav-encoder-migration-design.md).

---

## Task 1: Add numpy as an explicit dependency

**Files:**
- Modify: `pyproject.toml` (lines 6-15, project dependencies)
- Modify: `uv.lock` (regenerated)

- [ ] **Step 1: Add `numpy>=2.0` to `[project] dependencies`**

Edit `pyproject.toml`. The dependencies block currently ends with the `av>=17.0.1` line added earlier. Append numpy:

```toml
dependencies = [
    "wows-replay-parser>=0.1.2",
    "pycairo>=1.26",
    "cairosvg>=2.7",
    "discord.py>=2.7.1",
    "click>=8.3.2",
    "rich>=15.0.0",
    "python-dotenv>=1.2.2",
    "av>=17.0.1",
    "numpy>=2.0",
]
```

- [ ] **Step 2: Resolve and update lockfile**

Run: `uv lock`
Expected: prints "Resolved N packages in <1s", `uv.lock` is updated to include numpy and any transitive deps.

- [ ] **Step 3: Sync the venv**

Run: `uv sync`
Expected: numpy installs (or "Audited" if already present transitively).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "Add numpy as explicit dependency for PyAV migration"
```

---

## Task 2: Write failing unit test for PyAVPipe

**Files:**
- Create: `tests/test_video.py`

- [ ] **Step 1: Create `tests/test_video.py`**

```python
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
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `uv run pytest tests/test_video.py -v`
Expected: FAIL with `ImportError: cannot import name 'PyAVPipe' from 'renderer.video'` (the class doesn't exist yet — `FFmpegPipe` is still there).

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_video.py
git commit -m "Add failing unit test for PyAVPipe"
```

---

## Task 3: Implement PyAVPipe in renderer/video.py

**Files:**
- Modify: `renderer/video.py` (full rewrite)
- Modify: `renderer/core.py` (line 24 import, line 114 instantiation)

- [ ] **Step 1: Replace renderer/video.py contents**

Overwrite `renderer/video.py` with:

```python
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
```

- [ ] **Step 2: Update the import in renderer/core.py**

Edit line 24 of `renderer/core.py`:

Before: `from renderer.video import FFmpegPipe, FrameWriter`
After: `from renderer.video import PyAVPipe, FrameWriter`

- [ ] **Step 3: Update the instantiation in renderer/core.py**

Edit line 114 of `renderer/core.py`:

Before: `pipe = FFmpegPipe(output_path, width, height, config.fps, config.crf, config.codec)`
After: `pipe = PyAVPipe(output_path, width, height, config.fps, config.crf, config.codec)`

- [ ] **Step 4: Run the new unit test — must pass now**

Run: `uv run pytest tests/test_video.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full test suite for regression check**

Run: `uv run pytest tests/ -v`
Expected: all tests pass or skip. The smoke and golden-image tests skip if their fixtures aren't populated — that's fine; what matters is no FAIL.

- [ ] **Step 6: Commit**

```bash
git add renderer/video.py renderer/core.py
git commit -m "Replace FFmpegPipe subprocess with in-process PyAVPipe"
```

---

## Task 4: Sanity render against a real replay

**Files:** none modified — verification only.

- [ ] **Step 1: Render a real replay end-to-end**

Pick one of the replays in `wows-renderer/` (the directory contains `.wowsreplay` files). Run:

```bash
uv run python render_quick.py 20260321_194840_PFSD508-Le-Terrible_56_AngelWings.wowsreplay /tmp/sanity.mp4
```

Expected: prints `Parsed: ..., Done: <secs>s, <size> MB → /tmp/sanity.mp4`. No stack trace.

- [ ] **Step 2: Verify the output is a valid mp4**

Run: `uv run python -c "import av; c=av.open('/tmp/sanity.mp4'); s=c.streams.video[0]; print(f'{s.width}x{s.height} h264 frames={s.frames}'); c.close()"`
Expected: prints something like `1920x1104 h264 frames=NNN` with NNN > 0.

No commit (verification only).

---

## Task 5: Drop ffmpeg from the Dockerfile runtime stage

**Files:**
- Modify: `Dockerfile` (line 33)

- [ ] **Step 1: Remove `ffmpeg` from the runtime apt-get install**

Edit `Dockerfile`, lines 30-35. Remove the `ffmpeg \` line:

Before:
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    git \
    ffmpeg \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*
```

After:
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    git \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*
```

The builder stage (lines 1-25) does not install ffmpeg, so no change there.

- [ ] **Step 2: Build the image to confirm nothing else needed ffmpeg**

Run: `DOCKER_BUILDKIT=1 docker compose build`
Expected: build completes successfully. The `uv sync` step pulls PyAV (with bundled FFmpeg libs) so the runtime has everything it needs.

If the build succeeds but the runtime image fails on first render, that means a code path imports something that needs system ffmpeg. Investigate and adjust — most likely candidate is a stale `subprocess` reference left somewhere.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "Drop ffmpeg from Docker runtime stage; PyAV bundles its own"
```

---

## Task 6: Update CLAUDE.md to reflect ffmpeg removal

**Files:**
- Modify: `CLAUDE.md` (Dependencies section, around lines 116-138)

- [ ] **Step 1: Update the dependency listing**

Edit `CLAUDE.md`. In the `[project] dependencies` code block (lines 119-127), append the new deps and update the comment:

Before:
```toml
dependencies = [
    "wows-replay-parser",        # Git dependency — the replay parser
    "pycairo>=1.26",             # Cairo vector graphics (2D rendering)
    "discord.py>=2.3",           # Discord bot
    "python-dotenv>=1.0",        # .env file loading for bot config
    "click>=8.0",                # CLI dependencies (reserved)
    "rich>=13.0",                # CLI dependencies (reserved)
]
```

After:
```toml
dependencies = [
    "wows-replay-parser",        # Git dependency — the replay parser
    "pycairo>=1.26",             # Cairo vector graphics (2D rendering)
    "av>=17.0.1",                # PyAV — in-process H.264 encoder (bundles FFmpeg libs)
    "numpy>=2.0",                # BGRA buffer view for PyAV VideoFrame.from_ndarray
    "discord.py>=2.3",           # Discord bot
    "python-dotenv>=1.0",        # .env file loading for bot config
    "click>=8.0",                # CLI dependencies (reserved)
    "rich>=13.0",                # CLI dependencies (reserved)
]
```

- [ ] **Step 2: Update the External runtime dependencies block**

In the `**External runtime dependencies:**` list (lines 135-138), remove the ffmpeg line:

Before:
```markdown
**External runtime dependencies:**
- **FFmpeg** must be on PATH (used via subprocess pipe, not a Python package)
- **Cairo** system library (pycairo is a binding, needs libcairo installed on Linux/macOS; Windows wheels include it)
- **Git** must be on PATH (used by gamedata_cache.py for `git archive` + `git tag` to extract version-specific data)
```

After:
```markdown
**External runtime dependencies:**
- **Cairo** system library (pycairo is a binding, needs libcairo installed on Linux/macOS; Windows wheels include it)
- **Git** must be on PATH (used by gamedata_cache.py for `git archive` + `git tag` to extract version-specific data)
```

- [ ] **Step 3: Update the Performance section to reflect the encoder change**

In the `### Performance` block (around lines 86-96), the line `- **FFmpeg fast preset** — 3x smaller output vs ultrafast (~5MB vs 16MB for typical match)` describes the still-current preset choice but the implementation note in `video.py` no longer references the FFmpeg subprocess. Replace that bullet with:

Before:
```markdown
- **FFmpeg fast preset** — 3x smaller output vs ultrafast (~5MB vs 16MB for typical match)
```

After:
```markdown
- **PyAV in-process encoder** — `preset=fast` + `tune=animation` + `crf=23` + `yuv420p`; ~3x smaller output vs ultrafast (~5MB vs 16MB for typical match). H.264 via libx264 bundled in the PyAV wheel — no system ffmpeg dependency.
```

The `video.py` line in the architecture tree (line 44) — `FFmpegPipe + FrameWriter (async background thread for pipe I/O)` — should also update:

Before:
```
│   ├── video.py               # FFmpegPipe + FrameWriter (async background thread for pipe I/O)
```

After:
```
│   ├── video.py               # PyAVPipe + FrameWriter (async background thread offloads stream.encode())
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Docs: PyAVPipe replaces FFmpegPipe; ffmpeg no longer a runtime dep"
```

---

## Task 7: Write the benchmark worker subprocess

**Files:**
- Create: `scripts/bench_encoder_worker.py`

- [ ] **Step 1: Create the worker script**

Create `scripts/bench_encoder_worker.py`:

```python
"""Worker subprocess for bench_encoder.py.

Renders one replay using the same configuration as render_quick.py and
prints a JSON results line on stdout, prefixed with __BENCH_RESULT__ so
the parent driver can locate it among any other output.

Designed to be invoked once per trial via subprocess so peak RSS can be
read in-process via getrusage(RUSAGE_SELF) without contamination from
prior trials in the same Python interpreter.

Usage:
  python scripts/bench_encoder_worker.py <replay.wowsreplay> <output.mp4>
"""
from __future__ import annotations

import json
import resource
import sys
from pathlib import Path

from wows_replay_parser import parse_replay

from renderer.config import RenderConfig
from renderer.core import MinimapRenderer
from renderer.gamedata_cache import VersionedGamedata, resolve_for_replay
from renderer.layers.aircraft import AircraftLayer
from renderer.layers.capture_points import CapturePointLayer
from renderer.layers.consumables import ConsumableLayer
from renderer.layers.health_bars import HealthBarLayer
from renderer.layers.hud import HudLayer
from renderer.layers.map_bg import MapBackgroundLayer
from renderer.layers.projectiles import ProjectileLayer
from renderer.layers.right_panel import RightPanelLayer
from renderer.layers.ships import ShipLayer
from renderer.layers.smoke import SmokeLayer
from renderer.layers.team_roster import TeamRosterLayer
from renderer.layers.weather import WeatherLayer

if len(sys.argv) != 3:
    sys.exit("usage: bench_encoder_worker.py <replay.wowsreplay> <output.mp4>")

REPLAY = sys.argv[1]
OUTPUT = sys.argv[2]
GAMEDATA_REPO = Path("wows-gamedata")

try:
    vgd = resolve_for_replay(REPLAY, GAMEDATA_REPO)
except RuntimeError:
    vgd = VersionedGamedata.from_gamedata_path(GAMEDATA_REPO / "data")

replay = parse_replay(REPLAY, str(vgd.entity_defs_path))

config = RenderConfig(
    gamedata_path=vgd.version_dir / "data",
    versioned_gamedata=vgd,
    speed=20.0,
    fps=20,
    minimap_size=1080,
    panel_width=420,
)
renderer = MinimapRenderer(config)
for L in [
    MapBackgroundLayer(), TeamRosterLayer(), CapturePointLayer(), WeatherLayer(),
    SmokeLayer(), ProjectileLayer(), AircraftLayer(), ShipLayer(),
    HealthBarLayer(), ConsumableLayer(), RightPanelLayer(), HudLayer(),
]:
    renderer.add_layer(L)

renderer.render(replay, Path(OUTPUT))

print("__BENCH_RESULT__", json.dumps({
    "render_phase_s": float(renderer.timings["render"]),
    "encode_phase_s": float(renderer.timings["encode"]),
    "frames": int(renderer.timings["frames"]),
    "output_bytes": Path(OUTPUT).stat().st_size,
    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
}))
```

- [ ] **Step 2: Smoke-test the worker manually**

Run from the wows-renderer directory:

```bash
uv run python scripts/bench_encoder_worker.py 20260321_194840_PFSD508-Le-Terrible_56_AngelWings.wowsreplay /tmp/bench_smoke.mp4
```

Expected output (last line):
```
__BENCH_RESULT__ {"render_phase_s": NN.NNN, "encode_phase_s": NN.NNN, "frames": NNN, "output_bytes": NNNNNNN, "peak_rss_kib": NNNNNNN}
```

- [ ] **Step 3: Commit**

```bash
git add scripts/bench_encoder_worker.py
git commit -m "Add bench_encoder_worker: per-trial render with JSON timings"
```

---

## Task 8: Write the benchmark driver

**Files:**
- Create: `scripts/bench_encoder.py`

- [ ] **Step 1: Create the driver**

Create `scripts/bench_encoder.py`:

```python
"""Benchmark driver — runs M trials × N replays in subprocess workers.

Captures wall time + render/encode phase split + peak RSS + output size.
Appends rows to a CSV; results from multiple branches accumulate in the
same file labeled by --label.

Usage:
  python scripts/bench_encoder.py --label pyav --output bench_results.csv
  # then on master branch:
  python scripts/bench_encoder.py --label master --output bench_results.csv

Each trial runs in its own Python subprocess so peak RSS (via
getrusage(RUSAGE_SELF) inside the worker) reflects that trial alone.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

DEFAULT_REPLAYS = [
    "20260327_213920_PASA020-Essex_18_NE_ice_islands.wowsreplay",
    "20260403_213640_PGSB708-Bismarck-1941_55_Seychelles.wowsreplay",
    "20260321_194840_PFSD508-Le-Terrible_56_AngelWings.wowsreplay",
]

RESULT_PREFIX = "__BENCH_RESULT__"


def run_trial(replay: Path, worker: Path) -> dict[str, float | int]:
    with tempfile.TemporaryDirectory() as td:
        output = Path(td) / "out.mp4"
        t0 = perf_counter()
        proc = subprocess.run(
            [sys.executable, str(worker), str(replay), str(output)],
            capture_output=True,
            text=True,
            check=True,
        )
        wall = perf_counter() - t0
        result_line = next(
            (ln for ln in proc.stdout.splitlines() if ln.startswith(RESULT_PREFIX)),
            None,
        )
        if result_line is None:
            raise RuntimeError(
                f"worker did not emit {RESULT_PREFIX} line.\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
            )
        result = json.loads(result_line.removeprefix(RESULT_PREFIX).strip())
    result["wall_time_s"] = wall
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True,
                        help="Branch label for the CSV (e.g. master, pyav)")
    parser.add_argument("--output", default="bench_results.csv",
                        help="CSV output path (appended to)")
    parser.add_argument("--trials", type=int, default=3,
                        help="Trials per replay")
    parser.add_argument("--replays", nargs="+", default=DEFAULT_REPLAYS,
                        help="Replay file paths")
    parser.add_argument("--worker", default="scripts/bench_encoder_worker.py",
                        help="Worker script path")
    args = parser.parse_args()

    worker = Path(args.worker)
    if not worker.exists():
        sys.exit(f"worker script not found: {worker}")

    csv_path = Path(args.output)
    file_exists = csv_path.exists()
    columns = [
        "label", "replay", "trial",
        "wall_time_s", "render_phase_s", "encode_phase_s",
        "peak_rss_kib", "output_bytes", "frames",
    ]
    with csv_path.open("a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(columns)
        for replay_str in args.replays:
            replay = Path(replay_str)
            if not replay.exists():
                print(f"SKIP: {replay} not found", file=sys.stderr)
                continue
            print(f"=== {replay.name} ({args.trials} trials) ===")
            for trial in range(1, args.trials + 1):
                print(f"  trial {trial}/{args.trials}...", end="", flush=True)
                r = run_trial(replay, worker)
                writer.writerow([
                    args.label, replay.name, trial,
                    f"{r['wall_time_s']:.3f}",
                    f"{r['render_phase_s']:.3f}",
                    f"{r['encode_phase_s']:.3f}",
                    int(r["peak_rss_kib"]),
                    int(r["output_bytes"]),
                    int(r["frames"]),
                ])
                f.flush()
                print(
                    f" wall={r['wall_time_s']:.1f}s "
                    f"render={r['render_phase_s']:.1f}s "
                    f"encode={r['encode_phase_s']:.1f}s "
                    f"rss={r['peak_rss_kib']/1024:.0f}MB",
                )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the driver with 1 trial × 1 replay**

Run from the wows-renderer directory:

```bash
uv run python scripts/bench_encoder.py --label pyav-smoke --output /tmp/smoke.csv --trials 1 --replays 20260321_194840_PFSD508-Le-Terrible_56_AngelWings.wowsreplay
```

Expected: prints one line with the trial result and writes `/tmp/smoke.csv` with header + one data row. Verify with `cat /tmp/smoke.csv`.

- [ ] **Step 3: Commit**

```bash
git add scripts/bench_encoder.py
git commit -m "Add bench_encoder driver: per-branch CSV output of trial results"
```

---

## Task 9: Write the visual parity check script

**Files:**
- Create: `scripts/bench_visual_parity.py`

- [ ] **Step 1: Create the parity script**

Create `scripts/bench_visual_parity.py`:

```python
"""Visual parity check between two encoder outputs.

Decodes a fixed timestamp from each mp4 and computes per-pixel diff in
RGB space. The two videos must have the same dimensions and a frame at
the requested timestamp. Renders are produced separately on each branch
— this script only diffs.

Pass criterion: per-pixel max-channel diff <= threshold (default 5/255).
libx264 with identical params is not byte-identical due to encoder-internal
threading non-determinism, but visually indistinguishable output should
stay well under this bound.

Usage:
  python scripts/bench_visual_parity.py master.mp4 pyav.mp4 --timestamp 60
"""
from __future__ import annotations

import argparse
import sys

import av
import numpy as np


def extract_frame_at(path: str, timestamp_s: float) -> np.ndarray:
    """Decode and return the first frame at >= timestamp_s as RGB24 ndarray."""
    container = av.open(path)
    try:
        stream = container.streams.video[0]
        target_pts = int(timestamp_s / stream.time_base)
        container.seek(target_pts, stream=stream)
        for frame in container.decode(stream):
            if frame.pts is not None and frame.pts >= target_pts:
                return frame.to_ndarray(format="rgb24")
        raise RuntimeError(f"no frame at >= {timestamp_s}s in {path}")
    finally:
        container.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("master_mp4")
    parser.add_argument("pyav_mp4")
    parser.add_argument("--timestamp", type=float, default=60.0,
                        help="Seek timestamp in seconds")
    parser.add_argument("--threshold", type=int, default=5,
                        help="Max allowed per-channel diff (0-255)")
    args = parser.parse_args()

    a = extract_frame_at(args.master_mp4, args.timestamp).astype(np.int16)
    b = extract_frame_at(args.pyav_mp4, args.timestamp).astype(np.int16)
    if a.shape != b.shape:
        sys.exit(f"shape mismatch: master={a.shape} pyav={b.shape}")

    diff = np.abs(a - b)
    per_channel_max = diff.max(axis=(0, 1)).tolist()
    per_channel_mean = [round(float(x), 3) for x in diff.mean(axis=(0, 1))]
    overall_max = int(diff.max())

    print(f"Frame extracted at t={args.timestamp}s, shape={a.shape}")
    print(f"Per-channel max diff (R, G, B): {per_channel_max}")
    print(f"Per-channel mean diff (R, G, B): {per_channel_mean}")
    print(f"Overall max diff: {overall_max}")

    if overall_max > args.threshold:
        print(f"FAIL: overall max diff {overall_max} > threshold {args.threshold}")
        sys.exit(1)
    print(f"PASS: overall max diff {overall_max} <= threshold {args.threshold}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit (no run yet — needs both mp4s, produced in next tasks)**

```bash
git add scripts/bench_visual_parity.py
git commit -m "Add bench_visual_parity: per-pixel diff between two encoder outputs"
```

---

## Task 10: Run benchmark on PyAV branch

**Files:** none modified — produces `bench_results.csv` and `/tmp/pyav_bismarck.mp4` for parity check.

- [ ] **Step 1: Stage bench scripts to /tmp so they survive the master checkout in Task 11**

The bench scripts only exist on the PyAV branch. Copy them to /tmp now so the next task can run them on master without having them in tree.

```bash
cp scripts/bench_encoder.py scripts/bench_encoder_worker.py /tmp/
ls -la /tmp/bench_encoder*.py
```

Expected: both files present in /tmp.

- [ ] **Step 2: Run the full bench matrix on PyAV**

Run from the wows-renderer directory (3 trials × 3 replays = 9 renders, ~5-10 minutes total):

```bash
uv run python scripts/bench_encoder.py --label pyav --output bench_results.csv
```

Expected: prints per-trial progress lines, ends with 9 rows in `bench_results.csv` (header + 9 data rows).

Verify: `wc -l bench_results.csv` → 10. `head -1 bench_results.csv` shows the column header.

- [ ] **Step 3: Render the parity-check video on PyAV**

```bash
uv run python scripts/bench_encoder_worker.py 20260403_213640_PGSB708-Bismarck-1941_55_Seychelles.wowsreplay /tmp/pyav_bismarck.mp4
```

Expected: prints `__BENCH_RESULT__ ...` line. `/tmp/pyav_bismarck.mp4` exists, ~5-15 MB.

- [ ] **Step 4: Commit the partial results**

```bash
git add bench_results.csv
git commit -m "Bench results: PyAV trials"
```

---

## Task 11: Run benchmark on master branch

**Files:** none modified in tree (cross-branch operation).

- [ ] **Step 1: Verify the working tree is clean before switching branches**

Run: `git status`
Expected: all of the previously-modified files (CLAUDE.md, pyproject.toml, uv.lock, wows-gamedata submodule) should already be committed on the PyAV branch. Untracked `docs/superpowers/` is fine — that's the spec/plan files.

If anything is uncommitted, stash it: `git stash push -u -m "pre-master-checkout"`.

- [ ] **Step 2: Switch to master**

```bash
git checkout master
```

Expected: `Switched to branch 'master'`. The PyAV-branch changes (PyAVPipe in video.py, etc.) are gone — `FFmpegPipe` is back. The `bench_results.csv` from Task 10 was committed on PyAV and is no longer in the working tree on master, but `/tmp/bench_encoder*.py` are still there.

- [ ] **Step 3: Verify master branch is using the old encoder**

Run: `grep -l FFmpegPipe renderer/`
Expected: matches `renderer/video.py` and `renderer/core.py`. If not, abort and investigate.

- [ ] **Step 4: Sync the venv to master's pyproject (no av/numpy yet)**

Run: `uv sync`
Expected: removes av and numpy if they were not dependencies on master, restores the master state.

- [ ] **Step 5: Confirm system ffmpeg is installed (master requires it on PATH)**

Run: `ffmpeg -version | head -1`
Expected: prints version line. If "command not found", install ffmpeg before continuing (`sudo apt install ffmpeg`).

- [ ] **Step 6: Run the bench matrix on master using the staged scripts**

Note: `bench_results.csv` does not exist on master because it was committed on PyAV and master never had it. Use a different output filename to avoid confusion, then merge later.

```bash
uv run python /tmp/bench_encoder.py --label master --output /tmp/bench_master.csv --worker /tmp/bench_encoder_worker.py
```

Expected: 9 trial rows written to `/tmp/bench_master.csv`.

- [ ] **Step 7: Render the parity-check video on master**

```bash
uv run python /tmp/bench_encoder_worker.py 20260403_213640_PGSB708-Bismarck-1941_55_Seychelles.wowsreplay /tmp/master_bismarck.mp4
```

Expected: `/tmp/master_bismarck.mp4` exists, similar size to `/tmp/pyav_bismarck.mp4`.

- [ ] **Step 8: Switch back to PyAV branch and merge the master CSV rows**

```bash
git checkout PyAV
uv sync  # restore PyAV deps
# Append master's rows to the committed bench_results.csv (skip header)
tail -n +2 /tmp/bench_master.csv >> bench_results.csv
wc -l bench_results.csv
```

Expected: `bench_results.csv` now has 1 header + 18 data rows = 19 lines total.

- [ ] **Step 9: Commit the merged results**

```bash
git add bench_results.csv
git commit -m "Bench results: master trials merged"
```

---

## Task 12: Run the visual parity check

**Files:** none modified — runs the parity script on the two Bismarck mp4s from the previous tasks.

- [ ] **Step 1: Run the parity check**

```bash
uv run python scripts/bench_visual_parity.py /tmp/master_bismarck.mp4 /tmp/pyav_bismarck.mp4 --timestamp 60
```

Expected: prints per-channel diff stats, ends with `PASS: overall max diff N <= threshold 5`.

If FAIL: capture the output. The threshold may need to be relaxed if the two encoders' libx264 builds differ enough to push the diff above 5/255 at one timestamp. Investigate before relaxing — a meaningful drift indicates the migration isn't quality-neutral.

- [ ] **Step 2: Save the parity output to a known location for the results doc**

```bash
uv run python scripts/bench_visual_parity.py /tmp/master_bismarck.mp4 /tmp/pyav_bismarck.mp4 --timestamp 60 > /tmp/parity_results.txt 2>&1 || true
cat /tmp/parity_results.txt
```

(The `|| true` keeps the redirect happy even on FAIL exit code.)

No commit (just produces a file in /tmp that's used in Task 13).

---

## Task 13: Write the results document

**Files:**
- Create: `docs/superpowers/specs/2026-04-27-pyav-bench-results.md`

- [ ] **Step 1: Compute the comparison medians**

Run a one-liner to extract median wall time, render phase, encode phase, peak RSS, and output size per (label, replay) from the CSV:

```bash
uv run python -c "
import csv, statistics
from collections import defaultdict
buckets = defaultdict(list)
with open('bench_results.csv') as f:
    for row in csv.DictReader(f):
        key = (row['label'], row['replay'])
        buckets[key].append(row)
for key, rows in sorted(buckets.items()):
    label, replay = key
    print(f'{label:8s} | {replay[:50]:50s} | wall={statistics.median(float(r[\"wall_time_s\"]) for r in rows):6.2f}s | render={statistics.median(float(r[\"render_phase_s\"]) for r in rows):6.2f}s | encode={statistics.median(float(r[\"encode_phase_s\"]) for r in rows):6.2f}s | rss={statistics.median(int(r[\"peak_rss_kib\"]) for r in rows)/1024:6.0f}MB | size={statistics.median(int(r[\"output_bytes\"]) for r in rows)/1024/1024:5.2f}MB')
"
```

Expected: 6 lines (3 replays × 2 labels), each showing the median of 3 trials.

Save the output: redirect to `/tmp/medians.txt` and refer to it when filling the results doc.

- [ ] **Step 2: Create the results doc**

Create `docs/superpowers/specs/2026-04-27-pyav-bench-results.md`. Use the template below — fill in the actual numbers from `/tmp/medians.txt` and `/tmp/parity_results.txt`. Compute the percent deltas as `(pyav - master) / master * 100`.

```markdown
# PyAV Encoder Migration — Benchmark Results

**Date:** 2026-04-27
**Branch:** PyAV vs master
**Spec:** [2026-04-27-pyav-encoder-migration-design.md](2026-04-27-pyav-encoder-migration-design.md)
**Raw data:** `bench_results.csv` (3 trials × 3 replays × 2 branches = 18 rows)

## Summary

[ONE PARAGRAPH: did PyAV win, lose, or tie on each axis. Was the migration the right call. Quote concrete numbers.]

## Comparison Table (median of 3 trials)

| Replay | Branch | Wall (s) | Render (s) | Encode (s) | Peak RSS (MB) | Output (MB) |
|---|---|---:|---:|---:|---:|---:|
| Essex (CV) | master | NN.NN | NN.NN | NN.NN | NNN | NN.NN |
| Essex (CV) | pyav | NN.NN | NN.NN | NN.NN | NNN | NN.NN |
| Bismarck (BB) | master | NN.NN | NN.NN | NN.NN | NNN | NN.NN |
| Bismarck (BB) | pyav | NN.NN | NN.NN | NN.NN | NNN | NN.NN |
| Le-Terrible (DD) | master | NN.NN | NN.NN | NN.NN | NNN | NN.NN |
| Le-Terrible (DD) | pyav | NN.NN | NN.NN | NN.NN | NNN | NN.NN |

## Deltas (PyAV vs master, % change)

| Replay | Wall | Render | Encode | Peak RSS | Output Size |
|---|---:|---:|---:|---:|---:|
| Essex | ±N.N% | ±N.N% | ±N.N% | ±N.N% | ±N.N% |
| Bismarck | ±N.N% | ±N.N% | ±N.N% | ±N.N% | ±N.N% |
| Le-Terrible | ±N.N% | ±N.N% | ±N.N% | ±N.N% | ±N.N% |

(Negative = PyAV is faster / smaller.)

## Visual Parity

Bismarck replay, frame extracted at t=60s, RGB diff:

```
[paste content of /tmp/parity_results.txt here]
```

[ONE-LINE INTERPRETATION: PASS or FAIL, what it means.]

## Methodology

- 3 trials per (replay, branch) combination, median reported.
- Each trial runs in its own Python subprocess (cleanup of all in-process state, isolated peak-RSS measurement via `getrusage(RUSAGE_SELF)`).
- Identical render config: 1080px minimap, 420px panels, 20fps, 20x speed, all 12 standard layers (matching `render_quick.py`).
- Identical x264 settings: `preset=fast`, `tune=animation`, `crf=23`, `pix_fmt=yuv420p`, `+faststart`.
- master uses system ffmpeg via subprocess pipe; PyAV uses bundled FFmpeg libs in-process.

## Conclusion

[ONE PARAGRAPH: did the migration meet the goal of "no quality regression, no performance regression". Mention any surprises. Final recommendation: keep PyAV / revert / investigate further.]
```

- [ ] **Step 3: Fill in the actual numbers**

Manually edit the doc to replace every `NN.NN` placeholder with the real medians from `/tmp/medians.txt`, the real diff output from `/tmp/parity_results.txt`, and write the Summary + Conclusion paragraphs based on the data.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-04-27-pyav-bench-results.md
git commit -m "Bench results: PyAV vs FFmpeg subprocess comparison"
```

---

## Done criteria

- `renderer/video.py` exports `PyAVPipe` and `FrameWriter`; no `FFmpegPipe`, no `subprocess` import.
- `renderer/core.py` imports and uses `PyAVPipe`.
- `Dockerfile` runtime stage does not install ffmpeg.
- `CLAUDE.md` reflects the encoder change.
- Full test suite (`uv run pytest tests/`) passes (or skips fixture-dependent tests cleanly).
- A real replay renders end-to-end via `render_quick.py` and produces a playable mp4.
- `bench_results.csv` has 19 rows (header + 9 master + 9 pyav).
- `docs/superpowers/specs/2026-04-27-pyav-bench-results.md` is committed with real numbers and a conclusion paragraph.
- Visual parity check passes (max diff <= 5/255), or the failure is documented and investigated.
