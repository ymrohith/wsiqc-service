"""HTTP layer.

Deliberately thin: validate input, call the repository, return schemas. No
image processing happens inside a request handler. That is the architectural
point of the whole service and the first thing worth being asked about.

Every endpoint is `def`, not `async def`. FastAPI runs sync handlers in a
threadpool, so a slow one cannot stall the event loop. Declaring handlers
`async def` and then doing blocking work inside them is the standard way to
make a FastAPI service freeze under load.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import repository as repo
from ..db.models import Job, JobStatus
from ..db.session import get_db, init_db
from ..logging_setup import configure_logging
from .schemas import (
    HealthOut,
    JobCreated,
    JobOut,
    ReportOut,
    SlideOut,
    SlideSubmission,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_dirs()
    init_db()
    log.info("api started (db=%s)", settings.database_url)
    yield
    log.info("api stopping")


app = FastAPI(
    title="Slide QC Service",
    version="1.0.0",
    description="Queue whole-slide images for tiling and focus quality control.",
    lifespan=lifespan,
)


def _resolve_slide_path(raw: str, settings: Settings) -> Path:
    """Resolve a submitted path, refusing anything outside the slide directory.

    Without this, `../../etc/passwd` is a valid slide path. Any service that
    accepts a caller-supplied path needs exactly this check.
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = settings.slide_dir / candidate
    candidate = candidate.resolve()

    root = settings.slide_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"path must be inside the slide directory ({root})",
        )
    if not candidate.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such slide: {raw}")
    return candidate


@app.post("/slides", response_model=JobCreated, status_code=status.HTTP_202_ACCEPTED)
def submit_slide(
    body: SlideSubmission,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JobCreated:
    """Accept a slide for analysis and return the job that will process it.

    202 rather than 200 or 201: the work is accepted but not finished, and
    the caller must poll. Resubmitting the same file returns the job already
    in flight instead of queueing the work twice.
    """
    path = _resolve_slide_path(body.path, settings)

    slide, created = repo.get_or_create_slide(db, path)
    if not created:
        existing = repo.find_reusable_job(db, slide.id)
        if existing is not None:
            db.commit()
            log.info("duplicate submission, returning job %s", existing.id)
            return JobCreated(
                job=JobOut.model_validate(existing),
                slide=SlideOut.model_validate(slide),
                duplicate=True,
            )

    job = repo.create_job(db, slide, body.tile_size, body.target_downsample)
    db.commit()
    log.info("queued job %s for slide %s", job.id, slide.id)
    return JobCreated(
        job=JobOut.model_validate(job),
        slide=SlideOut.model_validate(slide),
        duplicate=False,
    )


@app.get("/jobs", response_model=list[JobOut])
def list_jobs(
    job_status: JobStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[JobOut]:
    return [JobOut.model_validate(j) for j in repo.list_jobs(db, job_status, limit, offset)]


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)) -> JobOut:
    job = repo.get_job(db, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job {job_id}")
    return JobOut.model_validate(job)


@app.get("/jobs/{job_id}/report", response_model=ReportOut)
def get_report(job_id: int, db: Session = Depends(get_db)) -> ReportOut:
    """The QC result, once one exists.

    409 rather than 404 while the job is queued or running: the resource
    will exist, it just does not yet. Clients retry on 409 and give up on
    404, so the distinction is not cosmetic.
    """
    job = repo.get_job(db, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job {job_id}")
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise HTTPException(status.HTTP_409_CONFLICT, f"job {job_id} is {job.status.value}")
    if job.status is JobStatus.FAILED or job.report is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            job.error or "analysis produced no report",
        )
    return ReportOut.model_validate(job.report)


@app.get("/jobs/{job_id}/heatmap")
def get_heatmap(job_id: int, db: Session = Depends(get_db)) -> Response:
    """The focus heatmap PNG: the human-readable version of the report."""
    job = repo.get_job(db, job_id)
    if job is None or job.report is None or not job.report.heatmap_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no heatmap for this job")
    path = Path(job.report.heatmap_path)
    if not path.exists():
        raise HTTPException(status.HTTP_410_GONE, "heatmap file has been removed")
    return FileResponse(path, media_type="image/png")


@app.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    """Readiness rather than liveness: it actually touches the database."""
    try:
        queued = db.scalar(
            select(func.count()).select_from(Job).where(Job.status == JobStatus.QUEUED)
        )
        running = db.scalar(
            select(func.count()).select_from(Job).where(Job.status == JobStatus.RUNNING)
        )
    except Exception as exc:  # pragma: no cover - only on a broken database
        log.exception("health check failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HealthOut(
        status="ok", database="ok", queued=int(queued or 0), running=int(running or 0)
    )
