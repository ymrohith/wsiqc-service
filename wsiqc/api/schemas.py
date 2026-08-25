"""API schemas.

Pydantic models are the contract. ORM objects never leave the session, which
is what keeps DetachedInstanceError out of the responses, and it means the
database schema can change without silently changing the public API.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..db.models import JobStatus


class SlideSubmission(BaseModel): #input to our first API operation.
                                  # SlideSubmission describes: What comes INTO the API.
    path: str = Field(..., description="Path to the slide file, relative or absolute")
    tile_size: int = Field(512, ge=64, le=4096)
    target_downsample: float = Field(4.0, gt=0, le=64)


class SlideOut(BaseModel):
    #SlideOut describes:What information about a slide can go OUT of the API.
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    size_bytes: int
    content_hash: str
    created_at: datetime


class ReportOut(BaseModel):
    #This describes the final Quality Control(QC) result that the client can receive.
    model_config = ConfigDict(from_attributes=True)

    tiles_total: int
    tiles_analyzed: int
    tiles_blurred: int
    tissue_coverage: float
    blur_threshold: float
    median_focus: float
    duration_seconds: float
    passed: bool
    heatmap_path: str | None = None
    thumbnail_path: str | None = None


class JobOut(BaseModel):
    #JobOut is the API's way of telling the client:"Here is the current state of that work."
    model_config = ConfigDict(from_attributes=True)

    id: int
    slide_id: int
    status: JobStatus
    attempts: int
    error: str | None = None
    tile_size: int
    target_downsample: float
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobCreated(BaseModel):
    """202 response: the work is accepted, not done."""
     #when the client submits a slide, the API will return information about:
     #the slide + the job + whether it was a duplicate submission.
    job: JobOut
    slide: SlideOut
    duplicate: bool = Field(
        False,
        description="True when this file was already submitted and an existing "
                    "job was returned instead of queueing a second one",
    )


class HealthOut(BaseModel):
    #It's operational information:
    # "Is the service alive and can it talk to its database?"
    status: str
    database: str
    queued: int
    running: int
