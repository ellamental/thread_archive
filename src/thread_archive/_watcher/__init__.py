"""Local-source watcher: poll AI-tool stores and import incrementally.

Product-owned (no ops, no HTTP) — each source watcher calls the local importer
directly. Which watchers run comes from the provider registry, so a plugin's
source joins the same loop as a built-in one; alongside them ride a drop-folder
watcher for account exports left in ``<home>/dumps/`` and a recovery pass over
Claude Code's VS Code host log. Every watcher self-gates on ``is_available()``,
so all of them share the one ``archive watch`` loop and an absent store simply
costs nothing.
"""

from __future__ import annotations

from .base import SourceDiscovery, SourceWatcher, WatchResult
from .daemon import Watcher
from .export_drop import ExportDropWatcher
from .exthost import ExthostWatcher
from .lazy import catch_up_once, try_ingest_owner_lock
from .sources import (
    ClaudeCodeWatcher,
    ClaudeScienceWatcher,
    CoworkWatcher,
    DbScanWatcher,
    FileSessionWatcher,
    RglobWatcher,
    antigravity_watcher,
    codex_watcher,
    cursor_watcher,
    default_watchers,
    discover_claude_dirs,
    discover_claude_science_dbs,
    enabled_watchers,
    grok_watcher,
    opencode_watcher,
    provider_watchers,
)

__all__ = [
    "Watcher",
    "SourceDiscovery",
    "SourceWatcher",
    "WatchResult",
    "FileSessionWatcher",
    "RglobWatcher",
    "DbScanWatcher",
    "catch_up_once",
    "try_ingest_owner_lock",
    "default_watchers",
    "enabled_watchers",
    "provider_watchers",
    "discover_claude_dirs",
    "discover_claude_science_dbs",
    "ClaudeCodeWatcher",
    "ClaudeScienceWatcher",
    "CoworkWatcher",
    "codex_watcher",
    "grok_watcher",
    "antigravity_watcher",
    "cursor_watcher",
    "opencode_watcher",
    "ExportDropWatcher",
    "ExthostWatcher",
]
