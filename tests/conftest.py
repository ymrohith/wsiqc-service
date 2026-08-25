"""Test fixtures.

Every test gets its own database and its own tiny slide. Nothing here touches
the developer's real wsiqc.db, and nothing needs a server running.

The sample slide is deliberately small: a suite that tiles a real slide takes
minutes, and a suite that takes minutes stops being run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from wsiqc.config import get_settings          # noqa: E402
from wsiqc.db.session import init_db, reset_engine  # noqa: E402


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolated settings, database and directories for one test."""
    slide_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    slide_dir.mkdir()
    out_dir.mkdir()

    monkeypatch.setenv("WSIQC_DATABASE_URL", f"sqlite:///{tmp_path/'test.db'}")
    monkeypatch.setenv("WSIQC_SLIDE_DIR", str(slide_dir))
    monkeypatch.setenv("WSIQC_OUTPUT_DIR", str(out_dir))
    monkeypatch.setenv("WSIQC_LOG_LEVEL", "WARNING")

    get_settings.cache_clear()
    reset_engine()
    init_db()

    yield tmp_path

    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def sample_slide(workspace):
    """A small synthetic slide with a known blurred band."""
    from make_sample_slide import synth_slide, write_pyramid

    path = workspace / "data" / "sample.tif"
    arr = synth_slide(1024, 768, seed=3)
    write_pyramid(arr, path, tile=256, levels=3)
    return path


@pytest.fixture
def client(workspace):
    from fastapi.testclient import TestClient

    from wsiqc.api.main import app

    with TestClient(app) as c:
        yield c
