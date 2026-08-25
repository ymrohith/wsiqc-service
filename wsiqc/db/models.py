"""Database models.

Two tables. A `Slide` is a file we know about, identified by content hash. A
`Job` is one attempt to analyse one slide, and it owns the state machine:

    queued -> running -> succeeded
                      -> failed        (attempts exhausted)
                      -> queued        (retry, or reclaimed after a worker died)

Storing the failure reason on the row is deliberate. A job that failed with
no recorded reason is the thing that wastes an afternoon in production.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Always timezone-aware UTC. Naive local timestamps age badly."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Slide(Base):
    __tablename__ = "slides"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Content hash, not filename: the same slide submitted twice under two
    # names is still the same slide, and this is what makes POST idempotent.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    jobs: Mapped[list["Job"]] = relationship(back_populates="slide",
                                             cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_slides_content_hash"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    slide_id: Mapped[int] = mapped_column(ForeignKey("slides.id", ondelete="CASCADE"))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=16),
        default=JobStatus.QUEUED,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    tile_size: Mapped[int] = mapped_column(Integer, default=512)
    target_downsample: Mapped[float] = mapped_column(Float, default=4.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    slide: Mapped[Slide] = relationship(back_populates="jobs")
    report: Mapped["Report | None"] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        # The worker's claim query filters on status and orders by id, so this
        # is the index that keeps polling cheap as the table grows.
        Index("ix_jobs_status_id", "status", "id"),
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"),
                                        unique=True)

    tiles_total: Mapped[int] = mapped_column(Integer)
    tiles_analyzed: Mapped[int] = mapped_column(Integer)
    tiles_blurred: Mapped[int] = mapped_column(Integer)
    tissue_coverage: Mapped[float] = mapped_column(Float)
    blur_threshold: Mapped[float] = mapped_column(Float)
    median_focus: Mapped[float] = mapped_column(Float)
    duration_seconds: Mapped[float] = mapped_column(Float)
    passed: Mapped[int] = mapped_column(Integer)          # 0/1, portable across DBs
    heatmap_path: Mapped[str | None] = mapped_column(String(1024))
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="report")
