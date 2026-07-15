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

from .._importers import (
    import_antigravity_session_incremental,
    import_claude_science_db,
    import_cloth_session_incremental,
    import_codex_session_incremental,
    import_cowork_session_incremental,
    import_cursor_db,
    import_grok_session_incremental,
    import_opencode_db,
    import_session_incremental,
)
from .base import SourceDiscovery, SourceWatcher, WatchResult

logger = logging.getLogger(__name__)


def _stat_discovery(name: str, paths: Iterator[Path]) -> SourceDiscovery:
    """Fold a stream of store files into a :class:`SourceDiscovery` — stat only."""
    items = 0
    total = 0
    earliest: Optional[float] = None
    latest: Optional[float] = None
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue
        items += 1
        total += st.st_size
        earliest = st.st_mtime if earliest is None else min(earliest, st.st_mtime)
        latest = st.st_mtime if latest is None else max(latest, st.st_mtime)
    return SourceDiscovery(
        name=name, available=items > 0, items=items, bytes=total,
        earliest=earliest, latest=latest,
    )


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

    store_mtime_tracks_content = True  # per-session transcript files

    def __init__(self) -> None:
        # Per-file (mtime_ns, size) fingerprints, pruned each poll to files on disk.
        self._seen: dict[str, tuple[int, int]] = {}

    def _iter_files(self) -> Iterator[tuple[Path, str]]:
        raise NotImplementedError

    def _import(self, path: Path, source_id: str):
        raise NotImplementedError

    def discover(self) -> SourceDiscovery:
        return _stat_discovery(self.source_name, (p for p, _ in self._iter_files()))

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
                    lines_processed=imp.lines_processed,
                    parse_errors=imp.parse_errors,
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
        pairs: list[tuple[Path, str]] = []
        for projects_dir in self._dirs():
            if not projects_dir.exists():
                continue
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for session_file in project_dir.glob("*.jsonl"):
                    pairs.append((session_file, f"{project_dir.name}:{session_file.stem}"))
                for agent_file in project_dir.glob("*/subagents/*.jsonl"):
                    pairs.append((agent_file, f"{project_dir.name}:{agent_file.stem}"))

        # Import oldest-first so a continuation's parent thread already exists when
        # the continuation is processed (continuation detection resolves to an
        # existing thread). On the live path parents land in earlier polls anyway;
        # this makes a cold start / rebuild-from-source merge correctly too.
        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        pairs.sort(key=lambda ps: _mtime(ps[0]))
        yield from pairs

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


def cloth_watcher(threads_dir: Optional[Path] = None) -> _RglobWatcher:
    # cloth writes Claude-Code-shaped JSONL to ~/.thread/cloth/threads/<stem>.jsonl — one file
    # per CLI session, ``source_id = "<stem>"``. cloth names files by session uuid
    # (already globally unique), so the source_id is the bare stem — no prefix.
    # (Bare numeric stems like ``1``/``22`` land bare too; they namespace under
    # source='cloth' for import, and only collide with an integer thread PK on *read*,
    # an accepted tradeoff.) CLOTH_HOME relocates the store. This is the sole live
    # cloth store the standalone archive ingests.
    import os

    root = threads_dir or (Path(os.environ.get("CLOTH_HOME") or Path.home() / ".thread" / "cloth").expanduser() / "threads")
    w = _RglobWatcher(root, import_cloth_session_incremental, lambda p: p.stem)
    w.glob, w._name = "*.jsonl", "cloth"
    return w


# ── DB (single live SQLite) watchers ────────────────────────────────────────


def _scan_errors(source_name: str, scan) -> list[str]:
    """A DB scan's per-item failures, tagged with the source, for the poll's errors.

    The scanners catch per-item failures so one bad conversation can't abort the scan —
    but a caught failure that only reaches the log is a conversation the archive doesn't
    have and doesn't know it doesn't have. Carrying them here puts them in the daemon's
    ``health.json`` (``watch_errors_last``), so a failing item shows up in ``archive
    status`` instead of reading as "nothing new"."""
    return [f"{source_name}: {e}" for e in (getattr(scan, "errors", None) or [])]


class _DbScanWatcher(SourceWatcher):
    """Detects mtime changes on live SQLite DBs and runs a scan importer on each
    changed one.

    The subclass seam is ``_targets()``: it yields ``(db_path, scan, label)`` —
    the DB to fingerprint, a zero-arg callable running its scan (returning a
    :class:`.._importers.DbScanResult`), and the label its errors carry. The
    base form watches one fixed-path DB (Cursor / OpenCode); a subclass may
    discover DBs dynamically each poll (Claude Science's per-org DBs). The
    fingerprint (DB mtime, and the ``-wal``'s where watched — writes can land
    in the WAL with the main file's mtime untouched) advances only after a
    successful scan, so a failed scan retries. Per-item failures inside a scan
    don't hold the fingerprint back — a permanently broken row would then
    re-scan the whole DB every poll forever — but they do ride out as errors,
    and the item itself retries on the DB's next change (its own watermark
    never advanced)."""

    def __init__(self, db_path: Optional[Path], name: str, scanner: Callable, *, watch_wal: bool = False) -> None:
        self.db_path = db_path
        self._name = name
        self._scanner = scanner
        self._watch_wal = watch_wal
        self._last_mtime: dict[str, float] = {}

    @property
    def source_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self.db_path is not None and self.db_path.exists()

    def _targets(self) -> Iterator[tuple[Optional[Path], Callable, str]]:
        yield self.db_path, (lambda: self._scanner(self.db_path)), self._name

    def _current_mtime(self, db_path: Optional[Path]) -> Optional[float]:
        if not db_path or not db_path.exists():
            return None
        mtimes = [db_path.stat().st_mtime]
        if self._watch_wal:
            wal = db_path.with_name(db_path.name + "-wal")
            if wal.exists():
                mtimes.append(wal.stat().st_mtime)
        return max(mtimes)

    def discover(self) -> SourceDiscovery:
        # Live SQLite DBs: sizes + activity mtimes. No conversation count —
        # counting means opening and understanding the provider's schema, and
        # the discovery pass is stat-only by contract.
        report = _stat_discovery(
            self.source_name, (db for db, _, _ in self._targets() if db is not None)
        )
        report.items = None
        if self._watch_wal:
            # WAL-aware activity: recent writes can sit in the -wal alone.
            for db, _, _ in self._targets():
                try:
                    mtime = self._current_mtime(db)
                except OSError:
                    continue
                if mtime is not None:
                    report.latest = max(report.latest or 0.0, mtime)
        return report

    def poll(self) -> WatchResult:
        result = WatchResult()
        seen_this_poll: set[str] = set()
        for db_path, scan_fn, label in self._targets():
            try:
                current = self._current_mtime(db_path)
            except OSError as e:
                result = result + WatchResult(errors=[f"{label}: cannot stat db: {e}"])
                continue
            if current is None or db_path is None:
                result = result + WatchResult(errors=[f"{label}: database not found"])
                continue
            key = str(db_path.resolve())
            seen_this_poll.add(key)
            if self._last_mtime.get(key) == current:
                result = result + WatchResult(sources_checked=1)
                continue
            try:
                scan = scan_fn()
            except Exception as e:  # noqa: BLE001 — one bad DB must not stop the poll
                logger.warning("%s scan failed: %s", label, e)
                result = result + WatchResult(errors=[f"{label}: scan failed: {e}"])
                continue
            # Fingerprint only after a successful scan, so a failure retries.
            self._last_mtime[key] = current
            result = result + WatchResult(
                sources_checked=scan.processed,
                items_imported=scan.imported,
                events_created=scan.events_created,
                errors=_scan_errors(label, scan),
            )
        # Prune fingerprints for DBs no longer present.
        if seen_this_poll:
            self._last_mtime = {k: v for k, v in self._last_mtime.items() if k in seen_this_poll}
        return result


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


# ── Cowork (nested local-agent-mode sessions) ───────────────────────────────


def discover_cowork_session_dirs() -> list[Path]:
    """Org-scoped dirs under ``~/Library/Application Support/Claude/
    local-agent-mode-sessions/<user>/<org>/`` (one level above the ``local_<id>/``
    session folders), so the caller derives ``user_uuid`` from ``parent.name`` and
    ``org_uuid`` from ``dir.name``."""
    base = Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
    if not base.is_dir():
        return []
    dirs: list[Path] = []
    for user_dir in base.iterdir():
        if not user_dir.is_dir() or user_dir.name == "skills-plugin":
            continue
        for org_dir in user_dir.iterdir():
            if org_dir.is_dir():
                dirs.append(org_dir)
    return dirs


class CoworkWatcher(FileSessionWatcher):
    """Watches Cowork ``audit.jsonl`` logs across all org dirs and imports the grown
    ones directly. source_id is ``{user_uuid}:{org_uuid}:{session_id}``; the human
    title comes from the sibling ``local_{id}.json`` — remembered per source_id at
    iteration time, the one per-file extra the shared poll loop doesn't carry."""

    def __init__(self) -> None:
        super().__init__()
        self._metadata: dict[str, Optional[Path]] = {}

    @property
    def source_name(self) -> str:
        return "cowork"

    def is_available(self) -> bool:
        return bool(discover_cowork_session_dirs())

    def _iter_sessions(self) -> Iterator[tuple[Path, str, Optional[Path]]]:
        for org_dir in discover_cowork_session_dirs():
            user_uuid = org_dir.parent.name
            org_uuid = org_dir.name
            for session_dir in org_dir.iterdir():
                if not session_dir.is_dir() or not session_dir.name.startswith("local_"):
                    continue
                audit_path = session_dir / "audit.jsonl"
                if not audit_path.exists():
                    continue
                session_id = session_dir.name[len("local_"):]
                source_id = f"{user_uuid}:{org_uuid}:{session_id}"
                metadata_path = org_dir / f"{session_dir.name}.json"
                yield audit_path, source_id, (metadata_path if metadata_path.exists() else None)

    def _iter_files(self) -> Iterator[tuple[Path, str]]:
        for audit_path, source_id, metadata_path in self._iter_sessions():
            self._metadata[source_id] = metadata_path
            yield audit_path, source_id

    def _import(self, path: Path, source_id: str):
        return import_cowork_session_incremental(path, source_id, self._metadata.get(source_id))


# ── Claude Science (per-org SQLite DB of conversation frames) ────────────────


def discover_claude_science_dbs(base: Optional[Path] = None) -> list[tuple[Path, str]]:
    """``(operon-cli.db path, org_uuid)`` for each org under ``~/.claude-science/orgs/``.

    Claude Science (the AI Workbench app) keeps one live SQLite DB per org at
    ``orgs/<org_uuid>/operon-cli.db``; the org_uuid is the dir name, used to scope the
    thread source_id so two orgs' frames never collide."""
    base = base or (Path.home() / ".claude-science" / "orgs")
    if not base.is_dir():
        return []
    out: list[tuple[Path, str]] = []
    for org_dir in sorted(base.iterdir()):
        if not org_dir.is_dir():
            continue
        db = org_dir / "operon-cli.db"
        if db.exists():
            out.append((db, org_dir.name))
    return out


class ClaudeScienceWatcher(_DbScanWatcher):
    """Watches every org's ``operon-cli.db`` and imports its conversation frames.

    The dynamic-discovery form of the DB-scan watcher: DBs are found each poll —
    as cowork does for its org dirs — so an org created after startup is picked
    up without a restart. WAL-watched, since frame writes can land in the WAL
    with the main file's mtime untouched."""

    def __init__(self, base: Optional[Path] = None) -> None:
        super().__init__(None, "claude-science", import_claude_science_db, watch_wal=True)
        self._base = base

    def is_available(self) -> bool:
        return bool(discover_claude_science_dbs(self._base))

    def _targets(self) -> Iterator[tuple[Optional[Path], Callable, str]]:
        for db_path, org_uuid in discover_claude_science_dbs(self._base):
            yield (
                db_path,
                (lambda db=db_path, org=org_uuid: self._scanner(db, org)),
                f"claude-science {org_uuid[:8]}",
            )


def default_watchers() -> list[SourceWatcher]:
    """One watcher per provider, default system paths.

    Each is **self-gating** — ``poll_once`` skips any whose ``is_available()`` is
    false — so a provider whose store is absent (no Cursor installed, no cloth store)
    costs nothing and adds no process. cloth is a provider like the rest, riding this
    one loop; there is no separate cloth daemon.

    The exthost watcher runs last: the JSONL sources import each session's persisted
    messages first (establishing their dedup_keys), so the exthost pass only has the
    genuinely-lost steering messages left to write.

    The export-drop watcher rides the same loop: it imports any claude.ai / ChatGPT /
    xAI account export dropped into ``<home>/dumps/`` (a human-driven drop zone, not a
    live store), so a one-time bulk export needs no separate command."""
    from .export_drop import ExportDropWatcher
    from .exthost import ExthostWatcher

    return [
        ClaudeCodeWatcher(),
        codex_watcher(),
        grok_watcher(),
        antigravity_watcher(),
        cloth_watcher(),
        cursor_watcher(),
        opencode_watcher(),
        CoworkWatcher(),
        ClaudeScienceWatcher(),
        ExportDropWatcher(),
        ExthostWatcher(),
    ]


# The mechanism watchers, as distinct from provider *sources*: export-drop only
# reads the archive's own <home>/dumps drop zone (consented by construction),
# and cc-exthost recovers claude-code steering messages — it follows the
# claude-code source rather than being a store of its own. Setup presents
# provider sources only; these two ride along.
_MECHANISM_SOURCES = frozenset({"export-drop", "cc-exthost"})


def provider_watchers() -> list[SourceWatcher]:
    """The consumer-facing provider sources (default set minus the mechanisms)."""
    return [w for w in default_watchers() if w.source_name not in _MECHANISM_SOURCES]


def enabled_watchers(home=None) -> list[SourceWatcher]:
    """``default_watchers()`` minus sources disabled in ``<home>/config.json``.

    The single choke point where operator source opt-outs (see
    :func:`.._config.source_enabled`) reach every ingest path — the daemon, the
    lazy MCP catch-up, ``archive watch`` — all of which construct their watcher
    set here. No config file means the full default set.
    """
    from .._config import load_config, source_enabled

    cfg = load_config(home)
    return [w for w in default_watchers() if source_enabled(cfg, w.source_name)]
