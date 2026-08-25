"""The worker.

Runs as a separate process from the API. It polls for queued jobs, claims one
atomically, runs the analysis in a child process, and writes the result back.

Why a process pool and not threads: tiling and focus scoring are CPU-bound
NumPy work. Python's GIL serialises bytecode across threads, so threads would
not add throughput. Processes each get their own interpreter.

Why polling and not Celery: one fewer moving part for a demo, and the claim
logic is visible instead of hidden in a framework. The JobQueue seam is where
Celery or SQS would slot in -- swapping it does not touch the analysis code.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..config import get_settings
from ..db import repository as repo
from ..db.session import init_db, session_scope
from ..imaging.analyze import analyze_slide
from ..imaging.render import write_heatmap, write_thumbnail
from ..logging_setup import configure_logging

log = logging.getLogger(__name__)


def run_analysis(slide_path: str, tile_size: int, target_downsample: float,
                 output_dir: str, job_id: int) -> dict:
    """Analyse one slide. Runs in a child process, so it takes and returns
    only picklable plain data -- no ORM objects, no open file handles."""
    report = analyze_slide(
        slide_path, tile_size=tile_size, target_downsample=target_downsample
    )
    out = Path(output_dir)
    heatmap = write_heatmap(report, out / f"job_{job_id}_heatmap.png")
    thumb = write_thumbnail(slide_path, out / f"job_{job_id}_thumb.png")
    return {
        "tiles_total": report.tiles_total,
        "tiles_analyzed": report.tiles_analyzed,
        "tiles_blurred": report.tiles_blurred,
        "tissue_coverage": report.tissue_coverage,
        "blur_threshold": report.blur_threshold,
        "median_focus": report.median_focus,
        "duration_seconds": report.duration_seconds,
        "passed": int(report.passed),
        "heatmap_path": str(heatmap),
        "thumbnail_path": str(thumb),
    }


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.settings.ensure_dirs()
        self._stop = False
        self._pool = ProcessPoolExecutor(max_workers=self.settings.worker_processes)

    def request_stop(self, *_args) -> None:
        """Finish the job in hand, then exit. Killing mid-job is what the
        stale-job reclaim exists to clean up, but it should be the exception."""
        log.info("stop requested, finishing current job")
        self._stop = True

    def process_one(self) -> bool:
        """Claim and run a single job. Returns False when the queue is empty."""
        with session_scope() as session:
            job = repo.claim_next_job(session)
            if job is None:
                return False
            job_id = job.id
            slide_path = job.slide.path
            tile_size = job.tile_size
            downsample = job.target_downsample

        log.info("job started", extra={"job_id": job_id, "slide": slide_path})
        try:
            future = self._pool.submit(
                run_analysis, slide_path, tile_size, downsample,
                str(self.settings.output_dir), job_id,
            )
            values = future.result()
        except Exception as exc:
            log.exception("job failed", extra={"job_id": job_id})
            with session_scope() as session:
                job = repo.get_job(session, job_id)
                if job is not None:
                    repo.mark_failed(session, job, f"{type(exc).__name__}: {exc}",
                                     self.settings.max_attempts)
            return True

        with session_scope() as session:
            job = repo.get_job(session, job_id)
            if job is not None:
                repo.mark_succeeded(session, job, values)
        log.info("job finished", extra={
            "job_id": job_id,
            "tiles_analyzed": values["tiles_analyzed"],
            "tiles_blurred": values["tiles_blurred"],
            "duration_seconds": round(values["duration_seconds"], 3),
        })
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        log.info("worker ready", extra={"pid": os.getpid(),
                                        "processes": self.settings.worker_processes})
        last_reclaim = 0.0
        while not self._stop:
            now = time.monotonic()
            if now - last_reclaim > 60:
                with session_scope() as session:
                    n = repo.reclaim_stale_jobs(session, self.settings.stale_job_seconds)
                if n:
                    log.warning("reclaimed stale jobs", extra={"count": n})
                last_reclaim = now

            if not self.process_one():
                time.sleep(self.settings.poll_interval_seconds)
        self.shutdown()

    def drain(self, limit: int = 1000) -> int:
        """Process everything currently queued, then return. Used by tests
        and by `wsiqc worker --once`."""
        done = 0
        while done < limit and self.process_one():
            done += 1
        return done

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
        log.info("worker stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    Worker().run_forever()


if __name__ == "__main__":
    main()
