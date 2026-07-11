"""SQLite engine + session factory for the archive store.

One backend: SQLite. There is no PostgreSQL branch, no connection pool, and no DDL
guard — on the embedded store there is no alembic, so ``create_all`` / ``reindex``
*must* be able to issue DDL.

PRAGMAs: WAL so concurrent readers don't block the single writer; ``foreign_keys``
ON (SQLite defaults them off) to match the schema's FK intent; a ``busy_timeout``
so a brief writer lock waits instead of erroring.

The ``use_engine`` ContextVar override is preserved verbatim: it's the seam the
serverless search relies on to point the whole query path at one index file for a
single call, with no global mutation and no cross-request interference. It is
carried into search-federation worker threads via ``copy_context``.
"""

from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

from sqlalchemy import create_engine, event
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base for all archive conversation-memory models."""

    pass


_engine: Engine | None = None

# Context-scoped engine override (see module docstring). Default None = use the
# module-global `_engine`. Per-call and concurrency-safe (a ContextVar, not a
# global swap).
_engine_override: contextvars.ContextVar = contextvars.ContextVar(
    "thread_archive_engine_override", default=None
)


def get_dsn() -> str:
    """Default DSN: the resolved ``index.db`` for this archive instance."""
    from .._config import resolve_paths

    return resolve_paths().ensure().sqlalchemy_url


def _attach_sqlite_pragmas(engine: Engine, *, enforce_fk: bool = True) -> None:
    """Per-connection PRAGMAs for the embedded SQLite backend.

    ``enforce_fk=False`` is for bulk replication paths (e.g. reindex from JSONL)
    that copy already-validated rows and full-snapshot parent tables, where FK
    enforcement would only fight dependency-agnostic bulk load — standard ETL
    practice.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON" if enforce_fk else "PRAGMA foreign_keys=OFF")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


def build_engine(dsn: str, *, enforce_fk: bool = True) -> Engine:
    """Construct a SQLite engine for a DSN.

    No server-style pool; cross-thread access is allowed (``check_same_thread``
    off) since the read path may run on a threadpool. WAL + busy_timeout PRAGMAs
    are attached per connection.
    """
    engine = create_engine(
        dsn,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    _attach_sqlite_pragmas(engine, enforce_fk=enforce_fk)
    return engine


def init_engine(dsn: str | None = None) -> None:
    """Initialize (or rebind) the module-global SQLAlchemy engine.

    Rebuilds when the target DSN differs from the current engine's — so opening a
    different archive home actually repoints the engine instead of silently keeping
    the old one (which would split writes between two stores).
    """
    global _engine
    target = dsn or get_dsn()
    if _engine is not None:
        if str(_engine.url) == target:
            return
        _engine.dispose()
    _engine = build_engine(target)


def active_dsn() -> str | None:
    """The DSN the module-global engine is bound to, or None if uninitialized."""
    return str(_engine.url) if _engine is not None else None


def get_engine() -> Engine:
    """Get the engine, initializing if needed.

    Honors a context-scoped override (``use_engine``) when one is active, so a
    library call can run the whole query path against a different store without
    touching — or initializing — the module-global engine.
    """
    override = _engine_override.get()
    if override is not None:
        return override
    if _engine is None:
        init_engine()
    assert _engine is not None, "init_engine() failed to set _engine"
    return _engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager yielding a Session bound to the active engine.

    Does not commit on exit — callers commit explicitly.
    """
    with Session(get_engine()) as session:
        yield session


@contextmanager
def use_session(session: Session | None = None) -> Generator[Session, None, None]:
    """Use an existing session or create a fresh one.

    Lets functions accept an optional session for testability while defaulting to
    a new session when none is provided.
    """
    if session:
        yield session
    else:
        with get_session() as s:
            yield s


@contextmanager
def use_engine(engine: Engine) -> Generator[Engine, None, None]:
    """Point ``get_engine()`` / ``get_session()`` at ``engine`` for the block.

    The seam that makes the embedded search a library: wrap a call in
    ``use_engine(index_engine)`` and every ``get_engine()`` inside it resolves to
    that engine. The previous binding is restored on exit, even on error.
    """
    token = _engine_override.set(engine)
    try:
        yield engine
    finally:
        _engine_override.reset(token)


def dml_rowcount(session: Session, statement: Any) -> int:
    """Execute a DML statement and return its rowcount."""
    result = cast(CursorResult, session.execute(statement))
    return result.rowcount


def close_engine() -> None:
    """Dispose the module-global engine. Called at shutdown."""
    global _engine
    if _engine:
        _engine.dispose()
        _engine = None
