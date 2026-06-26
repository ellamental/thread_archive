"""Local-source watcher: poll AI-tool stores and import incrementally.

Product-owned (no ops, no HTTP) — each source watcher calls the local importer
directly. Covers the seven providers with importers — Claude Code, Codex, Grok,
Antigravity, cloth, Cursor, OpenCode. Every watcher self-gates on ``is_available()``,
so all of them ride the one ``archive watch`` loop and an absent store simply costs
nothing.
"""

from __future__ import annotations

from .base import SourceWatcher, WatchResult
from .daemon import Watcher
from .exthost import ExthostWatcher
from .sources import (
    ClaudeCodeWatcher,
    antigravity_watcher,
    cloth_watcher,
    codex_watcher,
    cursor_watcher,
    default_watchers,
    discover_claude_dirs,
    grok_watcher,
    opencode_watcher,
)

__all__ = [
    "Watcher",
    "SourceWatcher",
    "WatchResult",
    "default_watchers",
    "discover_claude_dirs",
    "ClaudeCodeWatcher",
    "codex_watcher",
    "grok_watcher",
    "antigravity_watcher",
    "cloth_watcher",
    "cursor_watcher",
    "opencode_watcher",
    "ExthostWatcher",
]
