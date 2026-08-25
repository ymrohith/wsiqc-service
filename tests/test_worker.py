"""Worker tests: the full submit -> process -> report loop."""

from __future__ import annotations

from wsiqc.db import repository as repo
from wsiqc.db.models import JobStatus
from wsiqc.db.session import session_scope
from wsiqc.worker.runner import Worker


def test_worker_drains_the_queue_and_writes_a_report(client, sample_slide):
    job_id = client.post(
        "/slides", json={"path": sample_slide.name, "tile_size": 128,
                         "target_downsample": 1.0}
    ).json()["job"]["id"]

    worker = Worker()
    try:
        assert worker.drain() == 1
    finally:
        worker.shutdown()

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "succeeded"

    report = client.get(f"/jobs/{job_id}/report")
    assert report.status_code == 200
    body = report.json()
    assert body["tiles_analyzed"] > 0
    assert body["tiles_total"] >= body["tiles_analyzed"]
    assert 0.0 <= body["tissue_coverage"] <= 1.0

    heatmap = client.get(f"/jobs/{job_id}/heatmap")
    assert heatmap.status_code == 200
    assert heatmap.headers["content-type"] == "image/png"


def test_worker_records_the_reason_a_job_failed(client, sample_slide, workspace):
    """A slide that vanishes between submission and processing must fail
    loudly, with the reason stored on the row."""
    client.post("/slides", json={"path": sample_slide.name})
    sample_slide.unlink()

    worker = Worker()
    try:
        worker.drain()
    finally:
        worker.shutdown()

    with session_scope() as s:
        job = repo.list_jobs(s)[0]
        # One attempt used; default max_attempts is 3, so it is queued for retry
        # rather than failed outright -- but the error is already recorded.
        assert job.status in (JobStatus.QUEUED, JobStatus.FAILED)
        assert job.error
        assert "FileNotFound" in job.error or "no such" in job.error.lower()


def test_empty_queue_returns_false(client):
    worker = Worker()
    try:
        assert worker.process_one() is False
    finally:
        worker.shutdown()
