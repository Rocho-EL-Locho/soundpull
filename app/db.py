"""Database engine, session helpers and initialization."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import DateTime, event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

log = logging.getLogger("db")


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
    """Create tables, then additively reconcile columns onto existing ones.

    `create_all` only ever creates *missing tables* — it never alters a table that
    already exists. So when a new model column is added (e.g. ``language`` or the
    ``tag_*`` fields), an old on-disk DB is missing that column and every query on
    the table crashes with ``no such column``. Since the project deliberately
    carries no migration framework (see CLAUDE.md), we bridge that gap here by
    adding any model columns that are missing from the live schema. This is
    *additive only* — it never drops, renames or retypes a column, and it never
    touches data. Structural changes (drops/renames/type changes) still require a
    manual step.
    """
    from app import models  # noqa: F401  (registers tables)

    SQLModel.metadata.create_all(engine)
    reconcile_columns(engine)


def reconcile_columns(engine: Engine) -> None:
    """Add columns present in the models but missing from existing DB tables."""
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table → create_all already built it in full
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = _add_column_ddl(engine, table.name, col)
            log.warning("Schema drift: adding missing column via %s", ddl)
            with engine.begin() as conn:
                conn.execute(text(ddl))


def _add_column_ddl(engine: Engine, table_name: str, col) -> str:
    """Render a single ``ALTER TABLE ... ADD COLUMN`` for a missing column."""
    type_sql = col.type.compile(dialect=engine.dialect)
    ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {type_sql}'
    default_sql = _default_literal(col)
    if not col.nullable:
        if default_sql is None:
            # SQLite cannot add a NOT NULL column without a default to a table that
            # already has rows. Fall back to nullable so startup never crashes.
            log.warning("Column %s.%s is NOT NULL but has no constant default; "
                        "adding it as nullable.", table_name, col.name)
            return ddl
        return f"{ddl} NOT NULL DEFAULT {default_sql}"
    return f"{ddl} DEFAULT {default_sql}" if default_sql is not None else ddl


def _default_literal(col) -> str | None:
    """Best-effort SQL literal for a column's default, or None if not expressible.

    Existing rows need a value for a new NOT NULL column, so we surface the model's
    scalar default (``Field(default=...)``). Callable defaults (``default_factory``)
    aren't constants; for datetime columns we substitute ``CURRENT_TIMESTAMP``.
    """
    default = col.default
    if default is not None and getattr(default, "is_scalar", False):
        return _sql_literal(default.arg)
    if isinstance(col.type, DateTime) and default is not None:
        return "CURRENT_TIMESTAMP"
    return None


def _sql_literal(value) -> str:
    if isinstance(value, bool):  # before int — bool is a subclass of int
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


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
