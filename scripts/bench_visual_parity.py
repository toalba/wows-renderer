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
