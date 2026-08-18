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
    "test_replays/20260327_213920_PASA020-Essex_18_NE_ice_islands.wowsreplay",
    "test_replays/20260403_213640_PGSB708-Bismarck-1941_55_Seychelles.wowsreplay",
    "test_replays/20260321_194840_PFSD508-Le-Terrible_56_AngelWings.wowsreplay",
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
