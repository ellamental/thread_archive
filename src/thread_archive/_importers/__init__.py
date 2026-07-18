"""Incremental import: provider transcripts → events (over the JSONL truth seam).

Three shapes: single-file line-stream importers (Claude Code, Codex, Grok,
Antigravity, Cowork), SQLite-DB scanners (Cursor, OpenCode, Claude Science), and
bulk account-export importers (claude.ai, ChatGPT, xAI — see :mod:`.exports`).
All converge on the same ``assemble_events`` build → dedup → write loop, and all
are atomic per unit of import.

Which importer belongs to which provider is the registry's business, not this
module's — the built-ins here are just the implementations it points at, and a
plugin's importer dispatches through the same tables. Cowork and Claude Science
take per-source dispatch arguments (a sibling metadata path, an org uuid), so
their watchers drive them directly and they declare no dispatch kind.
"""

from __future__ import annotations

from ._result import DbScanResult, IncrementalImportResult
from .antigravity import import_antigravity_session_incremental
from .claude_code import import_session_incremental  # noqa: F401 — re-export
from .claude_science import (
    ClaudeScienceImportResult,
    import_claude_science_db,
    import_claude_science_frame,
)
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


def line_stream_importers(home=None) -> dict:
    """Provider name → single-file line-stream importer, from the registry.

    A DB scanner reads many sessions out of one store and is dispatched through
    :func:`db_scanners` instead; a provider needing more than a path to dispatch
    on declares no kind and appears in neither.
    """
    from .._providers import importers

    return importers("line-stream", home=home)


def db_scanners(home=None) -> dict:
    """Provider name → whole-store scan importer, from the registry."""
    from .._providers import importers

    return importers("db-scan", home=home)


def providers(home=None) -> list[str]:
    """Every provider name ``archive import --provider`` can dispatch on."""
    return sorted({**line_stream_importers(home), **db_scanners(home)})


__all__ = [
    "DbScanResult",
    "IncrementalImportResult",
    "import_session_incremental",
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
    "line_stream_importers",
    "db_scanners",
    "providers",
]
