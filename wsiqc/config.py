"""Configuration.

Everything comes from the environment with a sane default, so the same image
runs on a laptop and in a container without code changes. Twelve-factor, and
it also means tests can point at a temporary database without monkeypatching.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WSIQC_", env_file=".env")

    database_url: str = "sqlite:///./wsiqc.db"

    # Where uploaded or referenced slides live, and where artefacts are written.
    slide_dir: Path = Path("data")
    output_dir: Path = Path("out")

    # Analysis defaults. Overridable per request.
    tile_size: int = 512
    target_downsample: float = 4.0
    min_tissue: float = 0.10

    # Worker behaviour.
    poll_interval_seconds: float = 1.0
    max_attempts: int = 3
    stale_job_seconds: int = 900        # reclaim jobs whose worker died
    worker_processes: int = 2

    log_level: str = "INFO"

    def ensure_dirs(self) -> None:
        self.slide_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """Cached so every caller sees the same object, and it is FastAPI-injectable."""
    return Settings()
