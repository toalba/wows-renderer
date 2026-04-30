# PyAV Encoder Migration — Design

**Date:** 2026-04-27
**Branch:** `PyAV`
**Status:** Approved, ready for implementation plan

## Goal

Replace the `subprocess`-based `FFmpegPipe` in [renderer/video.py](../../../renderer/video.py) with an in-process PyAV encoder, then quantify the change with a benchmark across three representative replays.

The benchmark drives the migration: we want to confirm PyAV is at least on par with the current pipe (rendering is currently encode-bound at 40% of frame time per CLAUDE.md) before committing to it permanently. The decision to swap is partly motivated by removing the system-level `ffmpeg` dependency from the Docker runtime image.

## Non-goals

- No change to **quality-affecting** encoder parameters (libx264, preset=fast, tune=animation, crf=23, yuv420p, +faststart). We want apples-to-apples output quality. See "Encoder Parity Principles" below for the line between "same quality" and "don't hold PyAV back".
- No change to public API of `core.py`'s render loop or `FrameWriter`'s contract.
- No new encoder-selection config flag. We're cutting over directly on this branch; the master branch is the rollback option.
- No change to other layers, the bot, or the gamedata cache.

## Encoder Parity Principles

The benchmark is meaningful only if neither encoder is artificially handicapped. Two rules:

**Equal quality target.** Both paths use the same quality-affecting x264 settings: `preset=fast`, `tune=animation`, `crf=23`, output `yuv420p`. The comparison is "at the same target quality, which path is faster / lighter on memory?" — not "which encoder produces a smaller file at any cost."

**No artificial wrappers.** PyAV is allowed to use its native idiomatic path. Specifically: feed `VideoFrame` objects with `format="bgra"` directly via `from_ndarray`, not through a re-implementation of the rawvideo subprocess pipe. The whole point of moving in-process is to skip the pipe; forcing PyAV to mimic the pipe's data flow would defeat the migration.

**What this rules out:** giving PyAV a slower preset (e.g. `medium`/`slow`) to "showcase" quality gains, switching to hardware encoders (different quality characteristics — apples to oranges), or restricting thread count below `threads=0` (auto). Any of those changes the comparison's meaning.

**What this rules in:** PyAV inherits whatever FFmpeg/x264 build version ships in its wheel, which may be newer than the system ffmpeg on master. That's a genuine deployment difference and gets captured naturally in the benchmark — we don't try to pin versions to match. If output quality drifts beyond the visual parity threshold (5/255 max-channel-diff), that's a finding to investigate, not a bug to paper over.

## Architecture

### `PyAVPipe` — replaces `FFmpegPipe`

Same public surface as the old class — drop-in at the one call site in [core.py:114](../../../renderer/core.py#L114). `FrameWriter` is unchanged: PyAV's `stream.encode()` blocks the calling thread on the actual H.264 work, so the async background-thread queue still keeps the render loop unblocked.

**Constructor signature (unchanged):**

```python
PyAVPipe(output_path, width, height, fps=20, crf=23, codec="libx264")
```

**Internal flow per frame:**

1. `write_frame(frame_data)` receives a `bytes` or `memoryview` of Cairo's BGRA buffer (already a `bytes()` copy made by `FrameWriter` to detach from the Cairo surface that the main thread is reusing).
2. Wrap as a numpy view: `np.frombuffer(frame_data, dtype=np.uint8).reshape(height, width, 4)`. Zero-copy.
3. `frame = av.VideoFrame.from_ndarray(arr, format="bgra")`.
4. Set `frame.pts = self.frame_count` and `frame.time_base = Fraction(1, fps)`.
5. For packet in `stream.encode(frame)`: `container.mux(packet)`.
6. Increment `self.frame_count`.

**On `close()`:**

1. Flush encoder: `for packet in stream.encode(None): container.mux(packet)`.
2. `container.close()`.

**Stream configuration (matches current ffmpeg invocation):**

```python
container = av.open(str(output_path), mode="w", format="mp4",
                    options={"movflags": "+faststart"})
stream = container.add_stream(codec, rate=fps)  # codec from constructor; default "libx264"
stream.width, stream.height = width, height
stream.pix_fmt = "yuv420p"
stream.options = {
    "preset": "fast",
    "tune": "animation",
    "crf": str(crf),
    "threads": "0",
}
```

The `codec` constructor parameter is passed through to `add_stream` rather than hardcoded to `"libx264"`, matching the existing `FFmpegPipe` behavior where callers can override via `RenderConfig.codec`.

Two parameters from the old ffmpeg command are subsumed by PyAV defaults and don't need explicit mapping: `-y` (PyAV always overwrites), `-loglevel error` / `-nostats` (PyAV doesn't write stderr progress).

### Why keep `FrameWriter`

The async writer pattern was added to keep the render loop unblocked while the previous frame's BGRA buffer was being written to the kernel pipe. With PyAV the bottleneck shifts: there's no kernel pipe write, but `stream.encode()` still does the actual H.264 compression work synchronously on the calling thread. Offloading that to a background thread preserves the render-loop concurrency. Same `maxsize=16` queue.

The `bytes()` copy in `FrameWriter.write_frame()` is also still load-bearing — it detaches from the Cairo surface that the main thread will overwrite on the next frame.

### Removed dependencies

- **System ffmpeg** — no longer required at runtime. PyAV's wheel bundles statically-linked FFmpeg libs.
- **Dockerfile** — drop `ffmpeg` from the runtime stage's `apt-get install` line. Builder stage may still need it transitively; verify and remove if not.
- **`subprocess`, `queue.Queue`-as-stderr-drainer, `_drain_stderr` thread** — gone. PyAV raises Python exceptions directly.

### Added dependencies

- **`av>=17.0.1`** — already added via `uv add av`.
- **`numpy`** — added explicitly to `pyproject.toml`. Already a transitive dep but we use it directly.

## Benchmark Harness

### `scripts/bench_encoder.py`

Standalone driver that benchmarks one branch at a time. The user checks out the branch they want to measure (master or PyAV), runs the script, results are written to a CSV labeled by branch. No git mutations from inside the script.

**Usage:**

```bash
git checkout master
python scripts/bench_encoder.py --label master --output bench_results.csv
git checkout PyAV
python scripts/bench_encoder.py --label pyav --output bench_results.csv  # appends
```

**Per-trial worker subprocess:** Each render runs as a subprocess (`subprocess.run([sys.executable, ...])`) so peak RSS can be isolated. `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` is read after the worker exits — KiB on Linux. The worker subprocess is a tiny script that imports `MinimapRenderer`, runs one render, and prints timings as JSON to stdout.

**Configuration matrix:**

- 3 replays:
  - `wows-renderer/20260327_213920_PASA020-Essex_18_NE_ice_islands.wowsreplay` (CV — high projectile/aircraft load)
  - `wows-renderer/20260403_213640_PGSB708-Bismarck-1941_55_Seychelles.wowsreplay` (BB — long match, secondaries)
  - `wows-renderer/20260321_194840_PFSD508-Le-Terrible_56_AngelWings.wowsreplay` (DD — short, fast)
- 3 trials per replay → median reported (libx264 single-trial noise ~3% from threading non-determinism)
- Render config matches `render_quick.py` — 1080px minimap, 20fps, 10x speed, all standard layers

**Per-trial measurements:**

| Metric | Source |
|---|---|
| `wall_time_s` | Outer `time.perf_counter()` around the worker subprocess |
| `render_phase_s` | `MinimapRenderer.timings["render"]` (already instrumented) |
| `encode_phase_s` | `MinimapRenderer.timings["encode"]` (already instrumented) |
| `peak_rss_kib` | `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` after worker exit |
| `output_bytes` | `Path(output).stat().st_size` |
| `frame_count` | `MinimapRenderer.timings["frames"]` |

CSV columns: `label,replay,trial,wall_time_s,render_phase_s,encode_phase_s,peak_rss_kib,output_bytes,frame_count`.

### `scripts/bench_visual_parity.py`

One-shot script run after both encoders have been benchmarked. Decodes a fixed timestamp from each output mp4 with PyAV, computes per-pixel diff with numpy.

**Logic:**

1. Render `Bismarck-1941` replay (longest of the three — guarantees the seek timestamp lands inside the match) with master branch → `master.mp4`.
2. Render same replay with PyAV branch → `pyav.mp4`.
3. For both: open with `av.open()`, seek to t=60s, decode one frame, convert to ndarray.
4. Compute `np.abs(a.astype(int16) - b.astype(int16))` channel-wise.
5. Report `max_diff` and `mean_diff` per channel.
6. Pass criterion: `max_diff <= 5/255` on all channels (libx264 with same params is not byte-identical due to encoder-internal threading non-determinism, but should be visually indistinguishable).

If the parity check fails, that's a signal that the BGRA path through `swscale` differs meaningfully from the raw-pipe path — design escalation, not a routine bug.

### Results document

`docs/superpowers/specs/2026-04-27-pyav-bench-results.md` — written after the benchmark runs. Markdown table comparing master vs pyav per replay (median of 3 trials each), plus the visual parity numbers, plus a one-paragraph conclusion.

## Testing

- **Existing test suite must pass on PyAV branch** — `tests/test_smoke.py` and `tests/test_golden_images.py` exercise the full render path including encoding. If golden images fail, that's a real regression: investigate before declaring victory.
- **No new unit tests for `PyAVPipe`** — it's a thin adapter. The existing smoke and golden-image tests validate that frames make it through to a playable mp4 of the right dimensions and length.

## Risks

| Risk | Mitigation |
|---|---|
| PyAV's bundled ffmpeg version differs from system ffmpeg → encoder output drift | Visual parity check catches drift > 5/255. If it trips, lock the comparison to identical libx264 versions or accept the difference. |
| `av.VideoFrame.from_ndarray` performs a copy of the bgra buffer internally | Acceptable — same memory profile as old path which did `bytes()` copy in `FrameWriter`. Profile to confirm if the benchmark shows unexpected memory growth. |
| Docker image breaks because builder stage needed ffmpeg for something else | Build the image after Dockerfile change and run the canary suite. If broken, restore ffmpeg in the builder stage only. |
| ProcessPoolExecutor workers in the bot can't pickle PyAV containers | Containers are local to `_render_frames` — never crossed across process boundary. Worker recycling (`RENDER_MAX_TASKS_PER_CHILD`) is unaffected. |

## Implementation Order

1. Replace `FFmpegPipe` with `PyAVPipe` in `renderer/video.py`. Keep `FrameWriter` untouched.
2. Add `numpy` to `pyproject.toml` `[project.dependencies]`. Run `uv lock`.
3. Update Dockerfile — drop runtime `ffmpeg`, verify builder still works.
4. Update [CLAUDE.md](../../../CLAUDE.md) external dependencies section: `FFmpeg must be on PATH` → removed.
5. Run existing test suite (`pytest tests/`). Fix any breakage.
6. Render a sanity replay end-to-end (`render_quick.py`) and play the output.
7. Write `scripts/bench_encoder.py` and `scripts/bench_visual_parity.py`.
8. Run the benchmark on both branches (master via `git checkout`).
9. Write `2026-04-27-pyav-bench-results.md` with the comparison.
10. Commit per logical chunk; final commit references results doc.
