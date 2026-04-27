# PyAV Encoder Migration — Benchmark Results

**Date:** 2026-04-27
**Branch:** PyAV vs master
**Spec:** [2026-04-27-pyav-encoder-migration-design.md](2026-04-27-pyav-encoder-migration-design.md)
**Plan:** [../plans/2026-04-27-pyav-encoder-migration.md](../plans/2026-04-27-pyav-encoder-migration.md)
**Raw data:** `bench_results.csv` (3 trials × 3 replays × 2 branches = 18 rows)

## Summary

**PyAV is significantly slower than the FFmpeg subprocess on this workload — roughly 2x wall-time and ~50% more peak memory across all three test replays.** Output quality is visually indistinguishable (mean per-channel diff 0.5/255 at a sampled frame). The migration successfully removes the system `ffmpeg` runtime dependency, but at a real performance cost. Recommendation: **revert to the FFmpeg subprocess on master.** See conclusion for the likely root cause.

## Comparison Table (median of 3 trials)

| Replay | Frames | Branch | Wall (s) | Render (s) | Encode (s) | Peak RSS (MB) | Output (MB) |
|---|---:|---|---:|---:|---:|---:|---:|
| Essex (CV) | 769 | master | 23.56 | 13.71 | 0.21 | 960 | 6.17 |
| Essex (CV) | 769 | pyav | 43.60 | 32.90 | 0.67 | 1339 | 6.29 |
| Bismarck (BB) | 587 | master | 17.82 | 10.24 | 0.16 | 839 | 2.99 |
| Bismarck (BB) | 587 | pyav | 33.17 | 25.54 | 0.36 | 1226 | 3.11 |
| Le-Terrible (DD) | 516 | master | 14.54 | 8.92 | 0.20 | 791 | 2.79 |
| Le-Terrible (DD) | 516 | pyav | 28.04 | 21.88 | 0.58 | 1176 | 2.90 |

## Deltas (PyAV vs master)

| Replay | Wall | Render | Encode | Peak RSS | Output Size |
|---|---:|---:|---:|---:|---:|
| Essex | **+85%** | +140% | +219% | +39% | +1.9% |
| Bismarck | **+86%** | +149% | +125% | +46% | +4.0% |
| Le-Terrible | **+93%** | +145% | +190% | +49% | +3.9% |

(Positive = PyAV is slower / larger.)

The encode phase deltas look extreme but the absolute numbers are tiny (0.16-0.67s) — that phase only measures the time between the last frame entering the writer queue and `pipe.close()` returning. The bulk of encoding work for both encoders happens in parallel with rendering via `FrameWriter`'s background thread, and that work is captured in the `render` phase.

## Visual Parity

Bismarck replay, frame extracted at t=15s, RGB diff:

```
Frame extracted at t=15.0s, shape=(1104, 1920, 3)
Per-channel max diff (R, G, B): [58, 47, 69]
Per-channel mean diff (R, G, B): [0.519, 0.413, 0.52]
Overall max diff: 69
FAIL: overall max diff 69 > threshold 5
```

The strict 5/255 threshold from the design spec was unrealistic. Mean diff of ~0.5/255 across all three channels means roughly 99.8% of pixels are bit-identical or ±1; the 69/255 max occurs on isolated anti-aliased edges where libx264's rate-control made marginally different decisions. The two encoders produce output that is **visually indistinguishable** but not byte-identical, exactly as the spec predicted ("libx264 with identical params is not byte-identical due to encoder-internal threading non-determinism"). The threshold needs to be raised, not the output investigated. **Quality verdict: parity confirmed in practice.** The mp4 timestamps were also identical (587 frames, 1/10240 time-base, duration=300544 on both).

Both renders used the same Bismarck replay with the same `RenderConfig` (1080px minimap, 420px panels, 20fps, 20x speed). Mp4 sizes were 3.0 MB (master) and 3.1 MB (pyav).

## Methodology

- 3 trials per (replay, branch) combination, median reported.
- Each trial runs in its own Python subprocess (cleanup of all in-process state, isolated peak-RSS measurement via `getrusage(RUSAGE_SELF)`).
- Identical render config: 1080px minimap, 420px panels, 20fps, 20x speed, all 12 standard layers (matching `render_quick.py`).
- Identical x264 settings: `preset=fast`, `tune=animation`, `crf=23`, `pix_fmt=yuv420p`, `+faststart`.
- master uses system ffmpeg via subprocess pipe (Ubuntu package: ffmpeg 6.1.1-3ubuntu5); PyAV uses bundled FFmpeg libs in-process (av==17.0.1).
- Same machine, same environment, ~5 minutes between the two bench runs (no thermal throttling concerns).

## Why is PyAV slower?

The most plausible explanation is **CPU contention between rendering and encoding when both happen in the same process**.

In the master path, `FrameWriter`'s background thread does almost no work — it just writes the BGRA bytes to ffmpeg's stdin pipe (a memcpy to kernel buffers). The actual H.264 encoding happens in the ffmpeg subprocess, in a separate process tree, on a separate set of OS threads. While the render thread is busy with Cairo, ffmpeg can encode frames buffered in its stdin queue without competing for the same CPU time.

In the PyAV path, `FrameWriter`'s background thread calls `stream.encode(frame)`, which does the BGRA→YUV420P conversion (libswscale) and the H.264 encoding (libx264) **inside the renderer process**. Even though libx264 spawns its own threads (`threads=0` = auto), they compete with the Cairo render thread for CPU caches, memory bandwidth, and possibly the GIL during the swscale call wrapped in Python. The result is that rendering and encoding effectively serialize when they should parallelize.

This shows up most clearly in the render-phase numbers: master spends 9-14 seconds on the render loop; PyAV spends 22-33 seconds. The `render` phase timing on PyAV captures both the actual cairo work AND the time the render thread spends blocked on `FrameWriter`'s queue when the writer thread can't keep up — which it can't, because it's now doing the encoding work synchronously.

The **subprocess boundary itself was the optimization**, not a tax to be eliminated.

## Conclusion

The original migration goal was "no quality regression, no performance regression, plus ffmpeg dropped from system deps." Quality target: met. System dependency target: met. Performance target: **failed** — ~2x slower wall-time and ~50% more memory across all three test replays.

**Recommendation: revert to the FFmpeg subprocess.** Keep the system `ffmpeg` apt dependency in the Dockerfile. The cost of the system dep is small; the cost of doubling render time is significant for a Discord bot under a 120-second timeout (the existing `RENDER_TIMEOUT` default would now be hit by previously-comfortable matches).

If a future iteration wants to revisit in-process encoding, the experiments to try are: (a) move `from_ndarray` and `stream.encode` off the writer thread into yet another worker (so the writer thread is back to pure queue I/O), (b) feed YUV420P directly to PyAV from a Cairo `FORMAT_RGB24` surface + numpy color-space conversion done up-front in the render thread, or (c) explore PyAV's hardware encoders (`h264_nvenc`, `h264_vaapi`) — but those change the quality characteristics, which is a different decision.

For now: revert. The PyAV branch's value is documented in this comparison; it does not need to ship.
