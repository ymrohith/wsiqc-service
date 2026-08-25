"""Data access.

Kept separate from the API so the worker can reuse it, and so the queueing
logic is testable without a running server.

The interesting function is claim_next_job(): several workers race for the
same rows, and the UPDATE-then-check pattern is what stops two of them
processing the same slide.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Job, JobStatus, Report, Slide, utcnow


def hash_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of file contents, read in chunks.

    Chunked because these files are gigabytes. Reading one into memory to
    hash it would defeat the point of the entire service.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def get_or_create_slide(session: Session, path: Path) -> tuple[Slide, bool]:
    """Register a slide by content hash. Returns (slide, created)."""
    digest = hash_file(path)
    existing = session.scalar(select(Slide).where(Slide.content_hash == digest))
    if existing is not None:
        return existing, False

    slide = Slide(
        path=str(path.resolve()),
        filename=path.name,
        content_hash=digest,
        size_bytes=path.stat().st_size,
    )
    session.add(slide)
    session.flush()
    return slide, True


def find_reusable_job(session: Session, slide_id: int) -> Job | None:
    """An existing job worth returning instead of creating a duplicate.

    This is what makes POST /slides idempotent: resubmitting the same file
    returns the job already in flight rather than queueing the work twice.
    """
    return session.scalars(
        select(Job)
        .where(
            Job.slide_id == slide_id,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED]),
        )
        .order_by(Job.id.desc())
    ).first()


def create_job(session: Session, slide: Slide, tile_size: int,
               target_downsample: float) -> Job:
    job = Job(
        slide_id=slide.id,
        status=JobStatus.QUEUED,
        tile_size=tile_size,
        target_downsample=target_downsample,
    )
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: int) -> Job | None:
    return session.get(Job, job_id)


def list_jobs(session: Session, status: JobStatus | None = None,
              limit: int = 50, offset: int = 0) -> list[Job]:
    stmt = select(Job).order_by(Job.id.desc()).limit(limit).offset(offset)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    return list(session.scalars(stmt))


def claim_next_job(session: Session) -> Job | None:
    """Atomically take one queued job for this worker.

    Two workers can read the same queued row, so reading is not enough. The
    UPDATE carries `status == QUEUED` in its WHERE clause, and the database
    reports how many rows it actually changed. Exactly one worker sees 1.
    """
    candidate = session.scalars(
        select(Job.id).where(Job.status == JobStatus.QUEUED).order_by(Job.id).limit(1)
    ).first()
    if candidate is None:
        return None

    result = session.execute(
        update(Job)
        .where(Job.id == candidate, Job.status == JobStatus.QUEUED)
        .values(status=JobStatus.RUNNING, started_at=utcnow(),
                attempts=Job.attempts + 1)
    )
    session.commit()

    if result.rowcount != 1:
        return None  # another worker won the race; caller polls again
    return session.get(Job, candidate)


def reclaim_stale_jobs(session: Session, older_than_seconds: int) -> int:
    """Return jobs abandoned by dead workers to the queue.

    A worker killed mid-job leaves its row in `running` forever. Nothing
    re-queues it on its own; something has to notice the row is old. This is
    the visible-timeout pattern, and the reason task processing must be safe
    to run more than once.
    """
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    result = session.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.started_at < cutoff)
        .values(status=JobStatus.QUEUED, started_at=None,
                error="reclaimed after worker timeout")
    )
    session.commit()
    return int(result.rowcount or 0)


def mark_succeeded(session: Session, job: Job, report_values: dict) -> Report:
    report = Report(job_id=job.id, **report_values)
    session.add(report)
    job.status = JobStatus.SUCCEEDED
    job.finished_at = utcnow()
    job.error = None
    session.commit()
    return report


def mark_failed(session: Session, job: Job, message: str, max_attempts: int) -> None:
    """Retry until attempts are exhausted, then stop and record why."""
    if job.attempts >= max_attempts:
        job.status = JobStatus.FAILED
        job.finished_at = utcnow()
    else:
        job.status = JobStatus.QUEUED
        job.started_at = None
    job.error = message[:2000]
    session.commit()
