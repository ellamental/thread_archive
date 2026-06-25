"""SQLite-native column types for the archive ORM.

thread-archive has a single backend — SQLite — so these map ``JSONB`` / ``ARRAY`` /
``REAL`` straight to their SQLite forms; there is no postgres-dialect import (the
import-ratchet forbids it). The public names are kept so model definitions read
cleanly:

    JSONB  -> JSON   (SQLite's JSON1; dict/list round-trip)
    ARRAY  -> JSON   (a JSON array — SQLite has no native array type)
    REAL   -> Float
"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Float, Integer
from sqlalchemy.types import TypeEngine

# Shared instances are safe — SQLAlchemy type objects are immutable descriptors,
# reusable across every column site (`mapped_column(JSONB, ...)`).
JSONB: TypeEngine = JSON()
REAL: TypeEngine = Float()

# A big-integer PRIMARY KEY that autoincrements on SQLite. `BIGINT PRIMARY KEY`
# is *not* a rowid alias (so it won't autoincrement), but `INTEGER PRIMARY KEY`
# is — and SQLite's INTEGER rowid is already 64-bit, so nothing is lost. These ids
# are declared as ``BigInteger`` to keep the 64-bit intent legible; the variant
# compiles that to the autoincrementing form here.
BigIntPK: TypeEngine = BigInteger().with_variant(Integer(), "sqlite")


def ARRAY(item_type: TypeEngine) -> TypeEngine:
    """A JSON array on SQLite. ``item_type`` is accepted for call-site parity with
    the ``ARRAY(item_type)`` spelling, but SQLite has no native array type, so the
    value is stored as a JSON array regardless."""
    return JSON()
