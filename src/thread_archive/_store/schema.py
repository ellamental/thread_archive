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
    # The external-content FTS index reads occurred_at from the shadow, so the
    # column must exist before ensure_fts can build the current-shape table.
    "events_fts": {"occurred_at": "TEXT"},
}


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


# Concurrent openers race on a virgin store: ``create_all``'s existence check is
# not atomic with its CREATEs, so two threads (e.g. the MCP server's warm-models
# search vs. the first tool call) can both see "no tables" and collide. The lock
# serializes openers in this process; the retry absorbs a racer in another one.
_init_lock = threading.Lock()


def init_db(engine: Engine | None = None) -> None:
    """Create all base tables on ``engine`` (or the active engine), then ALTER in any
    column a pre-existing index predates. Idempotent and safe under concurrent
    first-open: a lost CREATE race is retried and a lost ADD-COLUMN race is
    absorbed, not raised."""
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
