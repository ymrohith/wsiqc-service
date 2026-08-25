"""Command line interface.

    python -m wsiqc analyze data/sample.tif
    python -m wsiqc submit data/sample.tif
    python -m wsiqc worker --once
    python -m wsiqc jobs

`analyze` runs the pipeline directly with no database or server, which is the
fastest way to debug the imaging layer in isolation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import get_settings
from .db import repository as repo
from .db.session import init_db, session_scope
from .imaging.analyze import analyze_slide
from .imaging.render import write_heatmap, write_thumbnail
from .logging_setup import configure_logging
from .worker.runner import Worker


def cmd_analyze(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    report = analyze_slide(
        args.slide,
        tile_size=args.tile_size,
        target_downsample=args.downsample,
        progress=lambda i, n: print(f"  tile {i}/{n}", end="\r", file=sys.stderr),
    )
    stem = Path(args.slide).stem
    heat = write_heatmap(report, settings.output_dir / f"{stem}_heatmap.png")
    thumb = write_thumbnail(args.slide, settings.output_dir / f"{stem}_thumb.png")

    print(json.dumps(report.to_dict(include_tiles=False), indent=2, default=str))
    print(f"\nheatmap   {heat}\nthumbnail {thumb}")
    return 0 if report.passed else 1


def cmd_submit(args: argparse.Namespace) -> int:
    init_db()
    path = Path(args.slide).resolve()
    with session_scope() as session:
        slide, created = repo.get_or_create_slide(session, path)
        existing = None if created else repo.find_reusable_job(session, slide.id)
        if existing is not None:
            print(f"already queued as job {existing.id} ({existing.status.value})")
            return 0
        job = repo.create_job(session, slide, args.tile_size, args.downsample)
        print(f"queued job {job.id} for {slide.filename}")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    init_db()
    worker = Worker()
    if args.once:
        n = worker.drain()
        worker.shutdown()
        print(f"processed {n} job(s)")
        return 0
    worker.run_forever()
    return 0


def cmd_jobs(_args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        jobs = repo.list_jobs(session)
        if not jobs:
            print("no jobs yet -- submit one with: python -m wsiqc submit <slide>")
            return 0
        print(f"{'id':>4}  {'status':<10} {'attempts':>8}  slide")
        for j in jobs:
            print(f"{j.id:>4}  {j.status.value:<10} {j.attempts:>8}  {j.slide.filename}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wsiqc", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="run the pipeline directly, no database")
    a.add_argument("slide")
    a.add_argument("--tile-size", type=int, default=512)
    a.add_argument("--downsample", type=float, default=4.0)
    a.set_defaults(func=cmd_analyze)

    s = sub.add_parser("submit", help="queue a slide for a worker to pick up")
    s.add_argument("slide")
    s.add_argument("--tile-size", type=int, default=512)
    s.add_argument("--downsample", type=float, default=4.0)
    s.set_defaults(func=cmd_submit)

    w = sub.add_parser("worker", help="run the worker")
    w.add_argument("--once", action="store_true",
                   help="drain the queue and exit instead of running forever")
    w.set_defaults(func=cmd_worker)

    j = sub.add_parser("jobs", help="list recent jobs")
    j.set_defaults(func=cmd_jobs)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings().log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
