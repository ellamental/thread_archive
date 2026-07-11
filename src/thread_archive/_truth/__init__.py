"""JSONL truth-log: durable source of truth, with reindex as the recovery primitive.

JSONL is truth; ``index.db`` is a rebuildable projection. See :mod:`.jsonl_log`.
"""

from __future__ import annotations

from .jsonl_log import (
    append_event_row,
    append_kg_event,
    checkpoint,
    log_dir,
    rebuild_truth_from_store,
    record_thread,
    reindex,
    reset_handles,
    scan_truth_counts,
    shared_ingest_lock,
    try_shared_ingest_lock,
    unstage_thread,
    write_events,
)
from .repair import repair_truth

__all__ = [
    "write_events",
    "append_event_row",
    "append_kg_event",
    "record_thread",
    "unstage_thread",
    "checkpoint",
    "reindex",
    "rebuild_truth_from_store",
    "log_dir",
    "repair_truth",
    "reset_handles",
    "scan_truth_counts",
    "shared_ingest_lock",
    "try_shared_ingest_lock",
]
