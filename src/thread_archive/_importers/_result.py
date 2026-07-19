"""Result contracts for an incremental import and a whole-DB scan."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    thread_id: str
    is_new_thread: bool
    last_message_uuid: Optional[str] = None
    parse_errors: int = 0


@dataclass
class DbScanResult:
    """Outcome of one whole-DB scan (Cursor composers, OpenCode sessions,
    Claude Science frames).

    One shape across every DB scanner, so the watch loop reads its counters off
    a typed contract instead of guessing field names per provider. ``processed``
    counts the units the scan looked at, ``imported`` the units that produced
    events this scan, ``failed`` the units whose import raised (each also
    carries a line in ``errors`` — a caught failure that only reached the log
    would be a conversation the archive doesn't have and doesn't know it
    doesn't have)."""

    processed: int = 0
    imported: int = 0
    events_created: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
