"""Result contract for an incremental import."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class IncrementalImportResult:
    """Outcome of one incremental-import call.

    ``parse_errors`` counts source lines dropped as unparseable JSON this call
    (see :func:`._read.parse_session_lines_counted`) — content the archive does
    not hold, surfaced so the watcher's health accounting can see it.
    """

    lines_processed: int
    events_created: int
    thread_id: int
    is_new_thread: bool
    last_message_uuid: Optional[str] = None
    parse_errors: int = 0
