"""The source watchers — one per built-in provider, plus the shared machinery
every watcher is built from.

Two properties worth noting: (1) each watcher calls the **local importer**
directly rather than POSTing to an HTTP ingest route, so it needs no backend
running, and (2) the JSONL file watchers share a single ``(mtime_ns, size)``
fingerprint skip, so an unchanged file is a no-op without a server round-trip.

:class:`FileSessionWatcher`, :class:`RglobWatcher` and :class:`DbScanWatcher`
are re-exported from :mod:`thread_archive.provider` and are part of the public
plugin API — a provider defined outside this package builds its watcher from the
same three shapes the built-ins use.

Which watchers run is not decided here: the set comes from the provider
registry, so a plugin's watcher joins the same poll loop on equal terms.

Out of scope here: any non-conversation data source, and any peer/status server —
neither belongs in a serverless conversation archive.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterator, Optional

from .._importers import (
    import_antigravity_session_incremental,
    import_claude_science_db,
    import_codex_session_incremental,
    import_cowork_session_incremental,
    import_cursor_db,
    import_grok_session_incremental,
    import_opencode_db,
    import_session_incremental,
)
from .base import SourceDiscovery, SourceWatcher, WatchResult, fingerprint_poll
from .paths import app_data_dir

logger = logging.getLogger(__name__)


def stat_discovery(name: str, paths: Iterator[Path]) -> SourceDiscovery:
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
    unchanged files and importing changed ones via ``self.import_session``.

    Subclasses provide ``source_name``, ``is_available``, ``iter_files`` (yields
    ``(path, source_id)``), and ``import_session(path, source_id) -> result`` (a result
    with ``.events_created`` / ``.thread_id`` / ``.is_new_thread``).
    """

    store_mtime_tracks_content = True  # per-session transcript files

    def __init__(self) -> None:
        # Per-file (mtime_ns, size) fingerprints, pruned each poll to files on disk.
        self._seen: dict[str, tuple[int, int]] = {}

    def iter_files(self) -> Iterator[tuple[Path, str]]:
        raise NotImplementedError

    def import_session(self, path: Path, source_id: str):
        raise NotImplementedError

    def discover(self) -> SourceDiscovery:
        return stat_discovery(self.source_name, (p for p, _ in self.iter_files()))

    def store_paths(self) -> Iterator[Path]:
        return (p for p, _ in self.iter_files())

    def _probe(self, target: tuple[Path, str]):
        session_file, _ = target
        try:
            st = session_file.stat()
        except OSError:
            return None  # unstattable → skip silently, don't retain a fingerprint
        if st.st_size == 0:
            return None  # empty file (mid-create) → skip until it has content
        return str(session_file.resolve()), (st.st_mtime_ns, st.st_size)

    def _work(self, target: tuple[Path, str]) -> WatchResult:
        session_file, source_id = target
        imp = self.import_session(session_file, source_id)
        return WatchResult(
            sources_checked=1,
            items_imported=1 if imp.events_created > 0 else 0,
            events_created=imp.events_created,
            lines_processed=imp.lines_processed,
            parse_errors=imp.parse_errors,
        )

    def _import_error(self, target: tuple[Path, str], exc: Exception) -> WatchResult:
        _, source_id = target
        msg = f"{self.source_name} import error for {source_id}: {exc}"
        logger.warning(msg)
        return WatchResult(sources_checked=1, errors=[msg])

    def poll(self) -> WatchResult:
        return fingerprint_poll(
            self.iter_files(), self._seen,
            probe=self._probe, work=self._work, on_error=self._import_error,
        )


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

    def iter_files(self) -> Iterator[tuple[Path, str]]:
        pairs: list[tuple[Path, str]] = []
        for projects_dir in self._dirs():
            if not projects_dir.exists():
                continue
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for session_file in project_dir.glob("*.jsonl"):
                    pairs.append((session_file, f"{project_dir.name}:{session_file.stem}"))
                # Recursive under subagents/, because workflow runs nest their
                # agents a further two levels down
                # (subagents/workflows/<wf-id>/agent-*.jsonl). Matched on the
                # agent- prefix rather than *.jsonl: a workflow directory also
                # holds a journal.jsonl, which is a run ledger rather than a
                # transcript, and whose name — unlike the globally unique
                # agent-<id> the source_id relies on — repeats once per run, so
                # every journal in a project would land on one id and fight over
                # it.
                for agent_file in project_dir.glob("*/subagents/**/agent-*.jsonl"):
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

    def import_session(self, path: Path, source_id: str):
        return import_session_incremental(path, source_id)


class RglobWatcher(FileSessionWatcher):
    """A :class:`FileSessionWatcher` over ``root.rglob(glob)``.

    The whole watcher for a provider that keeps one transcript file per session
    somewhere under a root directory. ``source_id_of`` derives each session's
    stable id from its path — the id that must stay the same across every poll
    of that session, since it is what the watermark and the thread are keyed on.
    Where the id lives varies: the file stem, its parent directory's name, a
    grandparent's.
    """

    def __init__(
        self,
        root: Path,
        importer: Callable,
        source_id_of: Callable[[Path], str],
        *,
        name: str,
        glob: str = "*.jsonl",
    ) -> None:
        super().__init__()
        self.root = root
        self.glob = glob
        self._name = name
        self._importer = importer
        self._source_id_of = source_id_of

    @property
    def source_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self.root.exists()

    def iter_files(self) -> Iterator[tuple[Path, str]]:
        if not self.root.exists():
            return
        for f in sorted(self.root.rglob(self.glob)):
            if f.is_file():
                yield f, self._source_id_of(f)

    def import_session(self, path: Path, source_id: str):
        return self._importer(path, source_id)


def codex_watcher(sessions_dir: Optional[Path] = None) -> RglobWatcher:
    return RglobWatcher(
        sessions_dir or (Path.home() / ".codex" / "sessions"),
        import_codex_session_incremental,
        lambda p: p.stem,
        name="codex",
    )


def grok_watcher(sessions_dir: Optional[Path] = None) -> RglobWatcher:
    # Each session lives in its own UUID dir; that UUID is the stable id.
    return RglobWatcher(
        sessions_dir or (Path.home() / ".grok" / "sessions"),
        import_grok_session_incremental,
        lambda p: p.parent.name,
        name="grok",
        glob="chat_history.jsonl",
    )


def antigravity_watcher(brain_dir: Optional[Path] = None) -> RglobWatcher:
    # brain/<id>/.system_generated/logs/transcript.jsonl → <id> is the session UUID.
    return RglobWatcher(
        brain_dir or (Path.home() / ".gemini" / "antigravity-cli" / "brain"),
        import_antigravity_session_incremental,
        lambda p: p.parents[2].name,
        name="antigravity",
        glob="transcript.jsonl",
    )


# ── DB (single live SQLite) watchers ────────────────────────────────────────


def _scan_errors(source_name: str, scan) -> list[str]:
    """A DB scan's per-item failures, tagged with the source, for the poll's errors.

    The scanners catch per-item failures so one bad conversation can't abort the scan —
    but a caught failure that only reaches the log is a conversation the archive doesn't
    have and doesn't know it doesn't have. Carrying them here puts them in the daemon's
    ``health.json`` (``watch_errors_last``), so a failing item shows up in ``archive
    status`` instead of reading as "nothing new"."""
    return [f"{source_name}: {e}" for e in (getattr(scan, "errors", None) or [])]


class DbScanWatcher(SourceWatcher):
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
        report = stat_discovery(
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

    def store_paths(self) -> Iterator[Path]:
        return (db for db, _, _ in self._targets() if db is not None)

    def _probe(self, target: tuple[Optional[Path], Callable, str]):
        db_path, _, label = target
        try:
            current = self._current_mtime(db_path)
        except OSError as e:
            return WatchResult(errors=[f"{label}: cannot stat db: {e}"])
        if current is None or db_path is None:
            return WatchResult(errors=[f"{label}: database not found"])
        return str(db_path.resolve()), current

    def _work(self, target: tuple[Optional[Path], Callable, str]) -> WatchResult:
        _, scan_fn, label = target
        scan = scan_fn()
        return WatchResult(
            sources_checked=scan.processed,
            items_imported=scan.imported,
            events_created=scan.events_created,
            errors=_scan_errors(label, scan),
        )

    def _scan_error(
        self, target: tuple[Optional[Path], Callable, str], exc: Exception
    ) -> WatchResult:
        _, _, label = target
        logger.warning("%s scan failed: %s", label, exc)
        return WatchResult(errors=[f"{label}: scan failed: {exc}"])

    def poll(self) -> WatchResult:
        return fingerprint_poll(
            self._targets(), self._last_mtime,
            probe=self._probe, work=self._work, on_error=self._scan_error,
        )


def cursor_watcher(db_path: Optional[Path] = None) -> DbScanWatcher:
    return DbScanWatcher(db_path or _cursor_default_db(), "cursor", import_cursor_db)


def opencode_watcher(db_path: Optional[Path] = None) -> DbScanWatcher:
    return DbScanWatcher(
        db_path or _opencode_default_db(), "opencode", import_opencode_db, watch_wal=True
    )


def _cursor_default_db() -> Optional[Path]:
    base = app_data_dir()
    if base is None:
        return None
    path = base / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return path if path.exists() else None


def _opencode_default_db() -> Optional[Path]:
    path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    return path if path.exists() else None


# ── Cowork (nested local-agent-mode sessions) ───────────────────────────────


def _cowork_base() -> Optional[Path]:
    """The Claude desktop app's ``local-agent-mode-sessions`` root (Electron keeps
    it under the platform's per-user app-data dir), or ``None`` where that OS has
    no known location."""
    base = app_data_dir()
    return base / "Claude" / "local-agent-mode-sessions" if base is not None else None


def discover_cowork_session_dirs() -> list[Path]:
    """Org-scoped dirs under the Claude app's ``local-agent-mode-sessions/
    <user>/<org>/`` root (one level above the ``local_<id>/`` session folders), so
    the caller derives ``user_uuid`` from ``parent.name`` and ``org_uuid`` from
    ``dir.name``."""
    base = _cowork_base()
    if base is None or not base.is_dir():
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

    def iter_files(self) -> Iterator[tuple[Path, str]]:
        for audit_path, source_id, metadata_path in self._iter_sessions():
            self._metadata[source_id] = metadata_path
            yield audit_path, source_id

    def import_session(self, path: Path, source_id: str):
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


class ClaudeScienceWatcher(DbScanWatcher):
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


def _build_watchers(providers) -> list[SourceWatcher]:
    """Construct each provider's watcher, skipping providers that have none.

    A watcher that raises on construction is logged and dropped rather than
    propagated: a provider whose factory is broken must cost only itself, not
    every other source in the poll.
    """
    built: list[SourceWatcher] = []
    for provider in providers:
        if provider.watcher is None:
            continue
        try:
            built.append(provider.watcher())
        except Exception:  # noqa: BLE001 — one bad factory must not stop ingest
            logger.exception(
                "provider %r: building its watcher failed — source skipped this pass",
                provider.name,
            )
    return built


def default_watchers(home=None) -> list[SourceWatcher]:
    """One watcher per registered provider that has a store to watch, in poll order.

    Each is **self-gating** — ``poll_once`` skips any whose ``is_available()`` is
    false — so a provider whose store is absent (no Cursor installed) costs
    nothing and adds no process. Every source rides this one loop; there is no
    per-provider daemon.

    Order comes from the registry: a watcher that recovers what another source
    persists must poll after it, so the primary import establishes its dedup keys
    first and the recovery pass writes only what is genuinely lost.
    """
    from .._providers import sources as provider_sources

    return _build_watchers(provider_sources(home=home))


def provider_watchers(home=None) -> list[SourceWatcher]:
    """The consumer-facing provider sources — the default set minus archive's own
    machinery (the drop zone, a recovery pass over another source's store), which
    an operator doesn't choose the way they choose a tool they use."""
    from .._providers import sources as provider_sources

    return _build_watchers(provider_sources(include_mechanisms=False, home=home))


def enabled_watchers(home=None) -> list[SourceWatcher]:
    """``default_watchers()`` minus sources disabled in ``<home>/config.json``.

    The single choke point where operator source opt-outs (see
    :func:`.._config.source_enabled`) reach every ingest path — the daemon, the
    opted-in MCP catch-up, ``archive watch`` — all of which construct their
    watcher set here. A provider that ``follows`` another is disabled with it: a recovery
    pass over a store the operator opted out of has nothing legitimate to read.
    No config file means the full default set.
    """
    from .._config import load_config, source_enabled
    from .._providers import registry

    cfg = load_config(home)

    def _enabled(provider) -> bool:
        if not source_enabled(cfg, provider.name):
            return False
        if provider.follows and not source_enabled(cfg, provider.follows):
            return False
        return True

    return _build_watchers([p for p in registry(cfg).values() if _enabled(p)])
