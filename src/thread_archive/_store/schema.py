"""Schema provisioning for the SQLite store.

``init_db`` is the schema baseline: ``Base.metadata.create_all`` over the active
(or given) engine, then ``_add_missing_columns``. Idempotent — existing tables are
left as-is. There is no alembic; the JSONL truth log + ``reindex`` are the recovery
primitive.

``create_all`` only issues ``CREATE TABLE`` for tables that don't exist yet, so it
never adds anything to a table a live index already has. Columns are the one gap that
can't wait for a reindex: the ORM selects every mapped column, so an index missing a
newly-added one fails *every* query against that table. Those are ALTERed in on open.

Missing *indexes* are left alone here — they're a correctness/performance property of
the data, not something a query needs to parse, and building one (e.g. the unique
dedup index over a multi-million-row events table) is far too much work to do silently
inside an open. ``verify`` reports an under-enforced schema and ``reindex`` heals it;
that stays the operator-visible path.

The FTS5 / vector *virtual* tables are not created here — they are built on
demand by the search layer's ``ensure_index``, since they are derived
projections, not base tables.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from . import models  # noqa: F401  (import for side effect: registers tables on Base.metadata)
from ._base import Base, get_engine

logger = logging.getLogger(__name__)

# table → {column: SQLite type declaration}. Columns added to a table after its
# baseline shipped; ALTERed in on open when a live index predates them.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "import_state": {"last_content_hash": "TEXT"},
    "thread_metrics": {"cache_read_tokens": "INTEGER NOT NULL DEFAULT 0"},
    "metrics_cursor": {"projection_version": "INTEGER NOT NULL DEFAULT 0"},
    # The external-content FTS index reads occurred_at from the shadow, so the
    # column must exist before ensure_fts can build the current-shape table.
    "events_fts": {"occurred_at": "TEXT"},
}

# Superseded rollup projections. These are disposable re-derivations of the event log
# — ``_metrics.refresh_metrics`` rebuilds whatever is still needed on the next stats
# read — so they are dropped rather than migrated. Leaving them would strand a stale
# shape that reads like the live one.
_DROPPED_TABLES: tuple[str, ...] = ("request_cache_metrics",)
_DROPPED_COLUMNS: tuple[tuple[str, str], ...] = (("metrics_cursor", "cache_requests_ready"),)


def _add_missing_columns(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            present = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
                {"t": table},
            ).first()
            if not present:
                continue
            have = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for column, decl in columns.items():
                if column in have:
                    continue
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))
                except OperationalError as exc:
                    # The daemon and MCP server open the store concurrently at start,
                    # so another process can win the ADD between our PRAGMA read and
                    # this ALTER — the column now exists, the same idempotent outcome
                    # this function targets, so absorb the race (mirroring init_db's
                    # "already exists" handling). Any other OperationalError is real.
                    if "duplicate column name" not in str(exc).lower():
                        raise
                    continue
                logger.info("schema: added %s.%s (%s)", table, column, decl)


def _drop_obsolete(engine: Engine) -> None:
    """Remove superseded projection tables and columns.

    Dropping is safe only because everything listed is a disposable re-derivation of
    the event log; nothing here holds truth. A racer that already dropped it, or a
    SQLite too old for ``DROP COLUMN``, leaves the store working either way, so
    neither is worth failing an open over.
    """
    with engine.begin() as conn:
        for table in _DROPPED_TABLES:
            try:
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            except OperationalError as exc:
                logger.warning("schema: could not drop obsolete table %s (%s)", table, exc)
        for table, column in _DROPPED_COLUMNS:
            present = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
                {"t": table},
            ).first()
            if not present:
                continue
            have = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column not in have:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
            except OperationalError as exc:
                logger.warning("schema: could not drop obsolete %s.%s (%s)", table, column, exc)
                continue
            logger.info("schema: dropped obsolete %s.%s", table, column)


# Concurrent openers race on a virgin store: ``create_all``'s existence check is
# not atomic with its CREATEs, so two threads (e.g. the MCP server's warm-models
# search vs. the first tool call) can both see "no tables" and collide. The lock
# serializes openers in this process; the retry absorbs a racer in another one.
_init_lock = threading.Lock()


def init_db(engine: Engine | None = None) -> None:
    """Create all base tables on ``engine`` (or the active engine), ALTER in any column
    a pre-existing index predates, then drop what a newer projection superseded.
    Idempotent and safe under concurrent first-open: a lost CREATE race is retried and
    a lost ADD-COLUMN race is absorbed, not raised.

    Each step only provisions shape. Deciding that a rollup's *contents* are stale is
    ``_metrics.refresh_metrics``'s job, keyed off its own ``projection_version`` —
    schema work never reaches across tables to rewrite data, which is what keeps the
    steps order-independent."""
    engine = engine or get_engine()
    with _init_lock:
        for attempt in (1, 2, 3):
            try:
                Base.metadata.create_all(engine)
                break
            except OperationalError as exc:
                if "already exists" not in str(exc) or attempt == 3:
                    raise
                # Another process created it between check and CREATE; re-run —
                # create_all skips what now exists and creates the remainder.
        _add_missing_columns(engine)
        _drop_obsolete(engine)
