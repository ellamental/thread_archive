"""The source watchers — one per supported provider.

Two properties worth noting: (1) each watcher calls the **local importer**
directly rather than POSTing to an HTTP ingest route, so it needs no backend
running, and (2) the JSONL file watchers share a single ``(mtime_ns, size)``
fingerprint skip, so an unchanged file is a no-op without a server round-trip.

Out of scope here: any non-conversation data source, and any peer/status server —
neither belongs in a serverless conversation archive.
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path
from typing import Callable, Iterator, Optional

from ..importers import (
    import_antigravity_session_incremental,
    import_codex_session_incremental,
    import_cursor_db,
    import_grok_session_incremental,
    import_opencode_db,
    import_session_incremental,
)
from .base import SourceWatcher, WatchResult

logger = logging.getLogger(__name__)


def discover_claude_dirs() -> list[Path]:
    """All ``~/.claude*/projects/`` directories (handles .claude → .claude1 renames)."""
    home = Path.home()
    dirs = sorted(p for p in home.glob(".claude*/projects") if p.is_dir())
    return dirs or [home / ".claude" / "projects"]


# ── file (per-session JSONL) watchers ───────────────────────────────────────


class FileSessionWatcher(SourceWatcher):
    """Watches a tree of per-session JSONL transcripts, fingerprint-skipping
    unchanged files and importing changed ones via ``self._import``.

    Subclasses provide ``source_name``, ``is_available``, ``_iter_files`` (yields
    ``(path, source_id)``), and ``_import(path, source_id) -> result`` (a result
    with ``.events_created`` / ``.thread_id`` / ``.is_new_thread``).
    """

    def __init__(self) -> None:
        # Per-file (mtime_ns, size) fingerprints, pruned each poll to files on disk.
        self._seen: dict[str, tuple[int, int]] = {}

    def _iter_files(self) -> Iterator[tuple[Path, str]]:
        raise NotImplementedError

    def _import(self, path: Path, source_id: str):
        raise NotImplementedError

    def poll(self) -> WatchResult:
        result = WatchResult()
        seen_this_poll: set[str] = set()

        for session_file, source_id in self._iter_files():
            try:
                st = session_file.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue

            resolved = str(session_file.resolve())
            fingerprint = (st.st_mtime_ns, st.st_size)
            seen_this_poll.add(resolved)
            if self._seen.get(resolved) == fingerprint:
                result = result + WatchResult(sources_checked=1)
                continue

            try:
                imp = self._import(session_file, source_id)
                # Fingerprint only after a successful import, so a failure retries.
                self._seen[resolved] = fingerprint
                result = result + WatchResult(
                    sources_checked=1,
                    items_imported=1 if imp.events_created > 0 else 0,
                    events_created=imp.events_created,
                )
            except Exception as e:  # noqa: BLE001 — one bad file must not stop the poll
                msg = f"{self.source_name} import error for {source_id}: {e}"
                logger.warning(msg)
                result = result + WatchResult(sources_checked=1, errors=[msg])

        # Prune fingerprints for files no longer present.
        if seen_this_poll:
            self._seen = {k: v for k, v in self._seen.items() if k in seen_this_poll}
        return result


class ClaudeCodeWatcher(FileSessionWatcher):
    def __init__(self, projects_dirs: Optional[list[Path]] = None) -> None:
        super().__init__()
        self._projects_dirs = projects_dirs

    @property
    def source_name(self) -> str:
        return "claude-code"

    def _dirs(self) -> list[Path]:
        if self._projects_dirs is not None:
            return self._projects_dirs
        return discover_claude_dirs()

    def is_available(self) -> bool:
        return any(d.exists() for d in self._dirs())

    def _iter_files(self) -> Iterator[tuple[Path, str]]:
        for projects_dir in self._dirs():
            if not projects_dir.exists():
                continue
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for session_file in project_dir.glob("*.jsonl"):
                    yield session_file, f"{project_dir.name}:{session_file.stem}"
                for agent_file in project_dir.glob("*/subagents/*.jsonl"):
                    yield agent_file, f"{project_dir.name}:{agent_file.stem}"

    def _import(self, path: Path, source_id: str):
        return import_session_incremental(path, source_id)


class _RglobWatcher(FileSessionWatcher):
    """A FileSessionWatcher over ``root.rglob(glob)`` with provider-specific id +
    importer."""

    glob: str
    _name: str

    def __init__(self, root: Path, importer: Callable, source_id_of: Callable[[Path], str]) -> None:
        super().__init__()
        self.root = root
        self._importer = importer
        self._source_id_of = source_id_of

    @property
    def source_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self.root.exists()

    def _iter_files(self) -> Iterator[tuple[Path, str]]:
        if not self.root.exists():
            return
        for f in sorted(self.root.rglob(self.glob)):
            if f.is_file():
                yield f, self._source_id_of(f)

    def _import(self, path: Path, source_id: str):
        return self._importer(path, source_id)


def codex_watcher(sessions_dir: Optional[Path] = None) -> _RglobWatcher:
    w = _RglobWatcher(
        sessions_dir or (Path.home() / ".codex" / "sessions"),
        import_codex_session_incremental,
        lambda p: p.stem,
    )
    w.glob, w._name = "*.jsonl", "codex"
    return w


def grok_watcher(sessions_dir: Optional[Path] = None) -> _RglobWatcher:
    # Each session lives in its own UUID dir; that UUID is the stable id.
    w = _RglobWatcher(
        sessions_dir or (Path.home() / ".grok" / "sessions"),
        import_grok_session_incremental,
        lambda p: p.parent.name,
    )
    w.glob, w._name = "chat_history.jsonl", "grok"
    return w


def antigravity_watcher(brain_dir: Optional[Path] = None) -> _RglobWatcher:
    # brain/<id>/.system_generated/logs/transcript.jsonl → <id> is the session UUID.
    w = _RglobWatcher(
        brain_dir or (Path.home() / ".gemini" / "antigravity-cli" / "brain"),
        import_antigravity_session_incremental,
        lambda p: p.parents[2].name,
    )
    w.glob, w._name = "transcript.jsonl", "antigravity"
    return w


# ── DB (single live SQLite) watchers ────────────────────────────────────────


class _DbScanWatcher(SourceWatcher):
    """Detects mtime changes on a single live SQLite DB and runs a scan importer."""

    def __init__(self, db_path: Optional[Path], name: str, scanner: Callable, *, watch_wal: bool = False) -> None:
        self.db_path = db_path
        self._name = name
        self._scanner = scanner
        self._watch_wal = watch_wal
        self._last_mtime: Optional[float] = None

    @property
    def source_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self.db_path is not None and self.db_path.exists()

    def _current_mtime(self) -> Optional[float]:
        if not self.db_path or not self.db_path.exists():
            return None
        mtimes = [self.db_path.stat().st_mtime]
        if self._watch_wal:
            wal = self.db_path.with_name(self.db_path.name + "-wal")
            if wal.exists():
                mtimes.append(wal.stat().st_mtime)
        return max(mtimes)

    def poll(self) -> WatchResult:
        try:
            current = self._current_mtime()
        except OSError as e:
            return WatchResult(errors=[f"{self._name}: cannot stat db: {e}"])
        if current is None:
            return WatchResult(errors=[f"{self._name}: database not found"])
        if self._last_mtime == current:
            return WatchResult()
        self._last_mtime = current

        try:
            scan = self._scanner(self.db_path)
        except Exception as e:  # noqa: BLE001
            return WatchResult(errors=[f"{self._name}: scan failed: {e}"])

        fields = vars(scan)
        processed = next((v for k, v in fields.items() if k.endswith("_processed")), 0)
        imported = next((v for k, v in fields.items() if k.endswith("_imported")), 0)
        return WatchResult(
            sources_checked=processed,
            items_imported=imported,
            events_created=scan.events_created,
        )


def cursor_watcher(db_path: Optional[Path] = None) -> _DbScanWatcher:
    return _DbScanWatcher(db_path or _cursor_default_db(), "cursor", import_cursor_db)


def opencode_watcher(db_path: Optional[Path] = None) -> _DbScanWatcher:
    return _DbScanWatcher(
        db_path or _opencode_default_db(), "opencode", import_opencode_db, watch_wal=True
    )


def _cursor_default_db() -> Optional[Path]:
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        path = home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    elif system == "Linux":
        path = home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    elif system == "Windows":
        import os

        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            return None
        path = Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    else:
        return None
    return path if path.exists() else None


def _opencode_default_db() -> Optional[Path]:
    path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    return path if path.exists() else None


def default_watchers() -> list[SourceWatcher]:
    """The full set of provider watchers, default system paths.

    The exthost watcher runs last: the JSONL sources import each session's persisted
    messages first (establishing their dedup_keys), so the exthost pass only has the
    genuinely-lost steering messages left to write."""
    from .exthost import ExthostWatcher

    return [
        ClaudeCodeWatcher(),
        codex_watcher(),
        grok_watcher(),
        antigravity_watcher(),
        cursor_watcher(),
        opencode_watcher(),
        ExthostWatcher(),
    ]
