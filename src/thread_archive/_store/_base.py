"""SQLite engine + session factory for the archive store.

One backend: SQLite. There is no PostgreSQL branch, no connection pool, and no DDL
guard — on the embedded store there is no alembic, so ``create_all`` / ``reindex``
*must* be able to issue DDL.

PRAGMAs: WAL so concurrent readers don't block the single writer; ``foreign_keys``
ON (SQLite defaults them off) to match the schema's FK intent; a ``busy_timeout``
long enough that a writer blocked behind an in-place maintenance transaction
waits it out instead of erroring.

What is open is an :class:`._instance.Archive` — the engine together with every
layer's per-archive cache — so closing one archive and opening another is a single
step that cannot leave the first one's derived state behind.

The ``use_engine`` ContextVar override is the seam the serverless search relies on
to point the whole query path at one index file for a single call, with no global
mutation and no cross-request interference. It is carried into search-federation
worker threads via ``copy_context``.
"""

from __future__ import annotations

import contextvars
import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

from sqlalchemy import create_engine, event
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import DeclarativeBase, Session

from ._instance import Archive


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base for all archive conversation-memory models."""

    pass


class ArchiveSession(Session):
    """The archive's own Session class — the type every archive write path uses.

    The truth-log listeners (the before-commit drain and its commit/rollback
    compensation, see :mod:`.._truth.jsonl_log`) are registered on THIS class,
    not on ``sqlalchemy.orm.Session``: the archive is a library, and class-level
    listeners on the base ``Session`` would fire for every SQLAlchemy session in
    a host process. Constructing a session any other way opts out of the truth
    drain entirely, so every archive session must come from :func:`get_session`
    (or construct ``ArchiveSession`` directly)."""

    pass


# The process's open archive (:class:`._instance.Archive` — engine plus every
# layer's per-archive cache). One at a time by default; `use_engine` opens a
# second for the duration of a block.
_current: "Archive | None" = None

# Context-scoped archive override (see module docstring). Default None = use
# `_current`. Per-call and concurrency-safe (a ContextVar, not a global swap).
_archive_override: contextvars.ContextVar = contextvars.ContextVar(
    "thread_archive_override", default=None
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
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute(
                "PRAGMA foreign_keys=ON" if enforce_fk else "PRAGMA foreign_keys=OFF"
            )
            # Sized to ride out an in-place maintenance transaction (rebuild_fts
            # over the full corpus, a bulk repair/backfill) which can hold the
            # write lock for several minutes — 60s demonstrably wasn't enough and
            # errored watcher imports into health during exactly those windows.
            # Under WAL only writers wait — readers are never blocked — so a long
            # timeout costs nothing on the read path, and a blocked writer that
            # waits here converges instead of erroring into health and retrying a
            # poll later.
            cur.execute("PRAGMA busy_timeout=300000")
        except Exception:
            # SQLAlchemy has not admitted this DB-API connection to the pool yet,
            # so a connect-listener failure leaves ownership ambiguous. Close it
            # here; a database that rejected its initialization PRAGMAs is not a
            # usable connection in any case.
            cur.close()
            dbapi_conn.close()
            raise
        else:
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
    """Open the process's archive at ``dsn`` (or the resolved default).

    Rebuilds when the target DSN differs from the open one — so opening a
    different archive home actually repoints instead of silently keeping the old
    one (which would split writes between two stores). The outgoing archive is
    *closed*, which disposes its engine and drops every layer's cache with it: a
    home switch cannot leave the previous home's vector matrix or set memo behind
    to be served for the new one.
    """
    global _current
    target = dsn or get_dsn()
    if _current is not None:
        if _current.dsn == target:
            return
        _current.close()
    _current = Archive(build_engine(target), target)


def active_dsn() -> str | None:
    """The DSN the open archive is bound to, or None if none is open."""
    return _current.dsn if _current is not None else None


def reconnect_if_swapped() -> None:
    """Dispose pooled connections when the open archive's database file was
    atomically replaced, and drop what was derived from the old rows.

    Reindex publishes by renaming a fresh build over index.db, and pooled
    connections keep the *old inode* open: reads freeze at the pre-swap state and
    commits land in a file nothing else can see. Everything cached off those rows
    is wrong rather than merely dated once the swap lands, so the caches go with
    the pool.

    Called from ``open_archive`` (read-path convergence) and — critically — on
    every ingest-lock *acquire* (``_truth.shared_ingest_lock`` and its try-
    variant): a writer blocks on that lock for the whole duration of a running
    reindex, so any identity check done before the wait is stale by the time
    the lock is granted. Checking under the lock is sound — the swap needs the
    exclusive side, so the identity observed here holds until release. Only the
    process's own archive is checked; a ``use_engine`` override (e.g. reindex's
    own loader) is never disposed from here."""
    if _current is None:
        return
    db = _current.engine.url.database
    if not db:  # pragma: no cover — non-file DSN (":memory:")
        return
    try:
        st = os.stat(db)
    except OSError:
        _current.index_ident = None
        return
    ident = (_current.dsn, st.st_dev, st.st_ino)
    previous = _current.index_ident
    # Same database file, different inode = a reindex published over it. A DSN
    # change is a home switch — init_engine already rebuilt for that.
    if previous is not None and previous[0] == ident[0] and previous != ident:
        _current.engine.dispose()
        _current.drop_caches()
    _current.index_ident = ident


def current_archive() -> Archive:
    """The archive this call runs against, opening the default one if needed.

    Honors a context-scoped override (``use_engine``) when one is active, so a
    library call can run the whole query path — and cache against — a different
    store without touching, or initializing, the process's own archive.
    """
    override = _archive_override.get()
    if override is not None:
        return override
    if _current is None:
        init_engine()
    assert _current is not None, "init_engine() failed to open an archive"
    return _current


def current_archive_or_none() -> "Archive | None":
    """The archive this call runs against, or ``None`` when none is open.

    For callers that must not *cause* one to open. Opening resolves the home from
    the environment and pins it for the process, so a function that opens as a side
    effect of doing nothing — a cache reset with no cache to reset, a status probe —
    fixes the archive at whatever the environment said at that moment, and a later,
    deliberate choice of home silently has no effect.
    """
    return _archive_override.get() or _current


def archive_cache(name: str) -> dict:
    """The open archive's scratch dict for the layer named ``name``.

    The seam every derived cache above the store hangs off: entries live inside
    the :class:`._instance.Archive` they were computed from, so they are scoped to
    it without being keyed on it, and they are gone when it closes. Callers key
    only by what varies *within* one archive (a content-type scope, a query
    shape) — never by the archive itself.
    """
    return current_archive().cache(name)


def get_engine() -> Engine:
    """The SQLAlchemy engine for the archive this call runs against."""
    return current_archive().engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager yielding a Session bound to the active engine.

    Does not commit on exit — callers commit explicitly.
    """
    with ArchiveSession(get_engine()) as session:
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

    The block gets a whole transient :class:`._instance.Archive`, not a bare
    engine swap, so anything cached inside it (a matrix, a graph, a set memo)
    lands in that archive's own slots and is dropped at exit — one store's
    derived answers can never be served as another's. The engine itself is the
    caller's and is not disposed here.
    """
    token = _archive_override.set(Archive(engine))
    try:
        yield engine
    finally:
        _archive_override.reset(token)


def dml_rowcount(session: Session, statement: Any) -> int:
    """Execute a DML statement and return its rowcount."""
    result = cast(CursorResult, session.execute(statement))
    return result.rowcount


def close_engine() -> None:
    """Close the process's archive — engine disposed, every layer's cache dropped.

    Called at shutdown, and between tests. One call is the whole teardown: a cache
    added above the store needs no new line here, and cannot be forgotten.
    """
    global _current
    if _current is not None:
        _current.close()
        _current = None
