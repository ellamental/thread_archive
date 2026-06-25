"""Schema provisioning for the SQLite store.

``init_db`` is the schema baseline: ``Base.metadata.create_all`` over the active
(or given) engine. Idempotent — existing tables are left as-is. There is no
alembic; the JSONL truth log + ``reindex`` are the recovery primitive.

The FTS5 / vector *virtual* tables are not created here — they are built on
demand by the search layer's ``ensure_index``, since they are derived
projections, not base tables.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

from . import models  # noqa: F401  (import for side effect: registers tables on Base.metadata)
from ._base import Base, get_engine


def init_db(engine: Engine | None = None) -> None:
    """Create all base tables on ``engine`` (or the active engine). Idempotent."""
    Base.metadata.create_all(engine or get_engine())
