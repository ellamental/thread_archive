"""Result contract for an incremental import."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class IncrementalImportResult:
    """Outcome of one incremental-import call."""

    lines_processed: int
    events_created: int
    thread_id: int
    is_new_thread: bool
    last_message_uuid: Optional[str] = None
