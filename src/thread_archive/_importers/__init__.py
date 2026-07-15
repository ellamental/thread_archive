"""Incremental import: provider transcripts → events (over the JSONL truth seam).

Three shapes: single-file line-stream importers (Claude Code, Codex, Grok,
Antigravity, cloth, Cowork), SQLite-DB scanners (Cursor, OpenCode, Claude
Science), and bulk account-export importers (claude.ai, ChatGPT, xAI — see
:mod:`.exports`). All converge on the same ``assemble_events`` build → dedup →
write loop, and all are atomic per unit of import. (cloth is Claude-Code-shaped,
so its importer delegates to the claude_code path under ``source="cloth"``.
Cowork and Claude Science take per-source dispatch arguments, so the watcher
drives them directly rather than through the registries below.)
"""

from __future__ import annotations

from ._result import DbScanResult, IncrementalImportResult
from .antigravity import import_antigravity_session_incremental
from .claude_code import import_session_incremental
from .claude_science import (
    ClaudeScienceImportResult,
    import_claude_science_db,
    import_claude_science_frame,
)
from .cloth import import_cloth_session_incremental
from .codex import import_codex_session_incremental
from .cowork import import_cowork_session_incremental
from .cursor import (
    CursorImportResult,
    import_cursor_db,
    import_cursor_from_payload,
)
from .grok import import_grok_session_incremental
from .opencode import (
    OpenCodeImportResult,
    import_opencode_db,
    import_opencode_from_payload,
)

# Provider name → single-file line-stream importer. (Cursor / OpenCode scan a DB
# of many sessions and are dispatched separately.)
LINE_STREAM_IMPORTERS = {
    "claude-code": import_session_incremental,
    "codex": import_codex_session_incremental,
    "grok": import_grok_session_incremental,
    "antigravity": import_antigravity_session_incremental,
    "cloth": import_cloth_session_incremental,
}

DB_SCANNERS = {
    "cursor": import_cursor_db,
    "opencode": import_opencode_db,
}

# Claude Science scans a DB of many frames per org, dispatched per-org by the watcher
# (not a single fixed DB path like Cursor/OpenCode), so it's tracked separately.

PROVIDERS = list(LINE_STREAM_IMPORTERS) + list(DB_SCANNERS)

__all__ = [
    "DbScanResult",
    "IncrementalImportResult",
    "import_session_incremental",
    "import_cloth_session_incremental",
    "import_codex_session_incremental",
    "import_cowork_session_incremental",
    "import_claude_science_db",
    "import_claude_science_frame",
    "ClaudeScienceImportResult",
    "import_grok_session_incremental",
    "import_antigravity_session_incremental",
    "import_cursor_db",
    "import_cursor_from_payload",
    "import_opencode_db",
    "import_opencode_from_payload",
    "CursorImportResult",
    "OpenCodeImportResult",
    "LINE_STREAM_IMPORTERS",
    "DB_SCANNERS",
    "PROVIDERS",
]
