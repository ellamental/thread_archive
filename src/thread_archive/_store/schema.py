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

from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import models  # noqa: F401  (import for side effect: registers tables on Base.metadata)
from ._base import Base, get_engine

logger = logging.getLogger(__name__)

# table → {column: SQLite type declaration}. Columns added to a table after its
# baseline shipped; ALTERed in on open when a live index predates them.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "import_state": {"last_content_hash": "TEXT"},
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
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))
                logger.info("schema: added %s.%s (%s)", table, column, decl)


def init_db(engine: Engine | None = None) -> None:
    """Create all base tables on ``engine`` (or the active engine), then ALTER in any
    column a pre-existing index predates. Idempotent."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
