"""cloth session import — cloth is a Claude-Code-shaped terminal harness.

cloth writes the same per-session JSONL shape as Claude Code (``user`` / ``assistant``
lines, plus a ``cloth_meta`` line the parser harmlessly ignores), so its importer
**delegates to the claude_code line-stream import** under ``source="cloth"`` rather
than duplicating a parser. Threads land with ``source="cloth"`` and ``source_id`` set
to the bare session-file stem (the CLI's session uuid — already globally unique — so
no prefix; see ``watcher.sources.cloth_watcher``). Idempotence, the truth-log seam, and
incremental watermarking all come from the shared claude_code path unchanged.
"""

from __future__ import annotations

from ._result import IncrementalImportResult
from .claude_code import import_session_incremental


def import_cloth_session_incremental(session_path, source_id: str, *, session=None) -> IncrementalImportResult:
    """Import one cloth CLI transcript into the event log, under ``source="cloth"``."""
    return import_session_incremental(session_path, source_id, source="cloth", session=session)
