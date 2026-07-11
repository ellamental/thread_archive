"""Local-source watcher: poll AI-tool stores and import incrementally.

Product-owned (no ops, no HTTP) — each source watcher calls the local importer
directly. Covers the providers with importers — Claude Code, Codex, Grok, Antigravity,
cloth, Cursor, OpenCode, Cowork, Claude Science — plus a drop-folder watcher that
auto-imports claude.ai / xAI account exports dropped into ``<home>/dumps/``. Every
watcher self-gates on
``is_available()``, so all of them ride the one ``archive watch`` loop and an absent
store simply costs nothing.
"""

from __future__ import annotations

from .base import SourceWatcher, WatchResult
from .daemon import Watcher
from .export_drop import ExportDropWatcher
from .lazy import catch_up_once, try_ingest_owner_lock
from .exthost import ExthostWatcher
from .sources import (
    ClaudeCodeWatcher,
    ClaudeScienceWatcher,
    antigravity_watcher,
    cloth_watcher,
    codex_watcher,
    cursor_watcher,
    default_watchers,
    discover_claude_dirs,
    discover_claude_science_dbs,
    grok_watcher,
    opencode_watcher,
)

__all__ = [
    "Watcher",
    "SourceWatcher",
    "WatchResult",
    "catch_up_once",
    "try_ingest_owner_lock",
    "default_watchers",
    "discover_claude_dirs",
    "discover_claude_science_dbs",
    "ClaudeCodeWatcher",
    "ClaudeScienceWatcher",
    "codex_watcher",
    "grok_watcher",
    "antigravity_watcher",
    "cloth_watcher",
    "cursor_watcher",
    "opencode_watcher",
    "ExportDropWatcher",
    "ExthostWatcher",
]
