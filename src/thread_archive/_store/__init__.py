"""The SQLite store: engine/session machinery + the conversation schema.

JSONL is truth; this SQLite store is the rebuildable projection.
"""

from __future__ import annotations

from ._base import (
    ArchiveSession,
    Base,
    active_dsn,
    archive_cache,
    build_engine,
    close_engine,
    current_archive,
    current_archive_or_none,
    dml_rowcount,
    get_engine,
    get_session,
    init_engine,
    reconnect_if_swapped,
    use_engine,
    use_session,
)
from ._instance import Archive
from .models import (
    CodeCursor,
    Event,
    EventFts,
    EventGitRef,
    EventPath,
    ImportState,
    KgEvent,
    MetricsCursor,
    Thread,
    ThreadLink,
    ThreadMetrics,
    TopicMessage,
)
from .resolve import resolve_session_source_id
from .schema import init_db
from .ulid import mint_ulid, normalize_ulid, ulid_timestamp_ms

__all__ = [
    # the open archive: engine, session, per-archive caches
    "Archive",
    "ArchiveSession",
    "Base",
    "build_engine",
    "init_engine",
    "active_dsn",
    "current_archive",
    "current_archive_or_none",
    "archive_cache",
    "get_engine",
    "get_session",
    "use_session",
    "use_engine",
    "close_engine",
    "reconnect_if_swapped",
    "dml_rowcount",
    # schema
    "init_db",
    # session-id resolution
    "resolve_session_source_id",
    # thread-id format
    "mint_ulid",
    "normalize_ulid",
    "ulid_timestamp_ms",
    # models
    "Thread",
    "Event",
    "EventFts",
    "EventPath",
    "EventGitRef",
    "CodeCursor",
    "ImportState",
    "ThreadLink",
    "ThreadMetrics",
    "MetricsCursor",
    "TopicMessage",
    "KgEvent",
]
