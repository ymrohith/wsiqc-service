"""Throughput and memory benchmark.

Two things worth measuring, and they answer different questions:

  serial      how fast one process gets through tiles, and what it costs in RAM
  parallel    whether more processes actually help, or whether decode is the wall

Peak RSS is the number that matters most here. If it stays flat while the
slide gets bigger, the streaming design is doing its job. If it tracks slide
size, something is calling asarray() on a whole level somewhere.

    python scripts/benchmark.py data/sample.tif
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wsiqc.imaging.analyze import analyze_slide  # noqa: E402


def peak_rss_mb() -> float:
    """ru_maxrss is KB on Linux and bytes on macOS. Assume Linux here."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _run(path: str, tile_size: int, downsample: float) -> tuple[int, float]:
    t0 = time.perf_counter()
    report = analyze_slide(path, tile_size=tile_size, target_downsample=downsample)
    return report.tiles_analyzed, time.perf_counter() - t0


def serial(path: str, downsample: float, sizes: list[int]) -> None:
    print(f"\n{'tile':>6} {'tiles':>7} {'seconds':>9} {'tiles/s':>9} {'peak MB':>9}")
    print("-" * 44)
    for size in sizes:
        tiles, secs = _run(path, size, downsample)
        rate = tiles / secs if secs else 0.0
        print(f"{size:>6} {tiles:>7} {secs:>9.2f} {rate:>9.1f} {peak_rss_mb():>9.1f}")


def parallel(path: str, downsample: float, tile_size: int,
             worker_counts: list[int], jobs: int) -> None:
    print(f"\n{'workers':>8} {'jobs':>5} {'seconds':>9} {'jobs/min':>9} {'speedup':>8}")
    print("-" * 44)
    baseline = None
    for n in worker_counts:
        t0 = time.perf_counter()
        with ProcessPoolExecutor(max_workers=n) as pool:
            list(pool.map(_run, [path] * jobs, [tile_size] * jobs,
                          [downsample] * jobs))
        secs = time.perf_counter() - t0
        baseline = baseline or secs
        print(f"{n:>8} {jobs:>5} {secs:>9.2f} {jobs / secs * 60:>9.1f} "
              f"{baseline / secs:>7.2f}x")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slide", type=Path)
    ap.add_argument("--downsample", type=float, default=2.0)
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()

    print(f"slide: {args.slide}  ({args.slide.stat().st_size / 1e6:.1f} MB on disk)")
    serial(str(args.slide), args.downsample, [128, 256, 512])
    parallel(str(args.slide), args.downsample, 256, [1, 2, 4], args.jobs)
    print(f"\npeak RSS of this process: {peak_rss_mb():.1f} MB")


if __name__ == "__main__":
    main()
