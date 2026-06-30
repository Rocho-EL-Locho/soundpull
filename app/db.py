"""Database engine, session helpers and initialization."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings


def _make_engine():
    url = settings.database_url
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # Ensure the parent directory exists for file-based SQLite URLs.
        # sqlite:///relative/path  or  sqlite:////absolute/path
        path_part = url.split("sqlite:///", 1)[-1]
        if path_part and path_part != ":memory:":
            db_path = Path(path_part)
            db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, echo=False, connect_args=connect_args)


engine = _make_engine()

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:
        # WAL + a busy timeout keep concurrent worker/UI writes from hitting
        # "database is locked" (check_same_thread=False shares connections).
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def init_db() -> None:
    """Create tables. Import models so they are registered on the metadata."""
    from app import models  # noqa: F401  (registers tables)

    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope. Safe to use from worker threads."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
