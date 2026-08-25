"""API and queue tests.

The analysis itself is stubbed in the worker tests: these are about job
state, idempotency and status codes, and a suite that actually tiles images
is too slow to run on every save.
"""

from __future__ import annotations

from wsiqc.db import repository as repo
from wsiqc.db.models import JobStatus
from wsiqc.db.session import session_scope


def test_health_reports_an_empty_queue(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "queued": 0, "running": 0}


def test_submit_returns_202_and_a_queued_job(client, sample_slide):
    r = client.post("/slides", json={"path": sample_slide.name})
    assert r.status_code == 202
    body = r.json()
    assert body["job"]["status"] == "queued"
    assert body["duplicate"] is False
    assert body["slide"]["filename"] == sample_slide.name


def test_resubmitting_the_same_file_is_idempotent(client, sample_slide):
    first = client.post("/slides", json={"path": sample_slide.name}).json()
    second = client.post("/slides", json={"path": sample_slide.name}).json()

    assert second["duplicate"] is True
    assert second["job"]["id"] == first["job"]["id"]
    assert client.get("/jobs").json().__len__() == 1


def test_unknown_slide_is_404(client):
    r = client.post("/slides", json={"path": "nope.tif"})
    assert r.status_code == 404


def test_path_traversal_is_rejected(client):
    r = client.post("/slides", json={"path": "../../etc/passwd"})
    assert r.status_code == 400


def test_invalid_tile_size_is_422(client, sample_slide):
    r = client.post("/slides", json={"path": sample_slide.name, "tile_size": 3})
    assert r.status_code == 422


def test_report_is_409_while_the_job_is_pending(client, sample_slide):
    job_id = client.post("/slides", json={"path": sample_slide.name}).json()["job"]["id"]
    r = client.get(f"/jobs/{job_id}/report")
    assert r.status_code == 409


def test_missing_job_is_404(client):
    assert client.get("/jobs/999").status_code == 404


def test_claim_is_exclusive(client, sample_slide):
    """Two claims against one queued job: the second must come back empty."""
    client.post("/slides", json={"path": sample_slide.name})

    with session_scope() as s:
        first = repo.claim_next_job(s)
        assert first is not None
        assert first.status is JobStatus.RUNNING

    with session_scope() as s:
        assert repo.claim_next_job(s) is None


def test_stale_running_jobs_are_reclaimed(client, sample_slide):
    client.post("/slides", json={"path": sample_slide.name})
    with session_scope() as s:
        job = repo.claim_next_job(s)
        job_id = job.id

    with session_scope() as s:
        # A worker that died the instant it claimed the job.
        assert repo.reclaim_stale_jobs(s, older_than_seconds=0) == 1
        assert repo.get_job(s, job_id).status is JobStatus.QUEUED


def test_failure_retries_then_gives_up(client, sample_slide):
    client.post("/slides", json={"path": sample_slide.name})

    with session_scope() as s:
        job = repo.claim_next_job(s)
        repo.mark_failed(s, job, "boom", max_attempts=2)
        assert job.status is JobStatus.QUEUED     # attempt 1 of 2, retry

    with session_scope() as s:
        job = repo.claim_next_job(s)
        repo.mark_failed(s, job, "boom again", max_attempts=2)
        assert job.status is JobStatus.FAILED     # attempts exhausted
        assert "boom again" in job.error
