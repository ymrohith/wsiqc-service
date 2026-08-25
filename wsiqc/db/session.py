"""Engine and session management.

SQLite is the default so the project runs with nothing installed, but it has
one sharp edge worth knowing: writers block each other. WAL mode plus a busy
timeout makes an API process and a worker process coexist. At real volume
this is where you move to Postgres, and saying so is better than pretending
SQLite scales.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from .models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - driver level
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")     # readers don't block writers
        cur.execute("PRAGMA busy_timeout=5000")    # wait rather than fail fast
        cur.execute("PRAGMA foreign_keys=ON")      # off by default in SQLite
        cur.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            _configure_sqlite(_engine)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False,
                                       future=True)
    return _SessionFactory


def init_db() -> None:
    """Create tables. Real deployments would run Alembic instead."""
    Base.metadata.create_all(get_engine())


def reset_engine() -> None:
    """Drop cached engine and factory -- used by tests switching databases."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaction boundary: commit on success, roll back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency -- one session per request, always closed."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
