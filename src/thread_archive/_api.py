"""The internal coordination layer for thread-archive.

Private, like every underscore-prefixed module — the CLI, the MCP servers, and
the web viewer are built on top of these functions; nothing outside the package
may import them. Each call
opens the archive — resolves the home, ensures its directories, pins it for the
process, and initializes the SQLite engine + schema — then dispatches into the
store / importers / search / truth / watch / ops layers. Internal imports are
lazy so ``import thread_archive`` stays light and pulls in no server backends.

The backup kit (``backup`` / ``restore_drill`` / ``verify`` / ``nightly``)
is implemented in :mod:`._ops` and re-exported here, so this module stays the
single coordination surface every caller goes through.

One archive per process: ``open_archive`` pins ``$THREAD_ARCHIVE_HOME`` so the
engine (index.db), the truth log (truth/), and search all resolve to the same
home. To switch archives, call :func:`close` first.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ._config import ENV_HOME, ArchivePaths, resolve_paths

if TYPE_CHECKING:
    from ._retrieval._types import EventHit

# The backup kit, re-exported (see docstring).
from ._ops.backup import backup, list_generations, restore, restore_drill  # noqa: F401
from ._ops.coverage import check_coverage  # noqa: F401
from ._ops.health import pipeline_verdict, read_health  # noqa: F401
from ._ops.nightly import nightly  # noqa: F401
from ._ops.snapshot import snapshot, stamp_snapshot  # noqa: F401
from ._ops.source_mirror import mirror_sources  # noqa: F401
from ._ops.verify import verify  # noqa: F401

_WATCH_PROCESS_ACTIVE = False


def _set_watch_process_active(active: bool) -> None:
    """Mark this process as the persistent watch loop for status consumers."""
    global _WATCH_PROCESS_ACTIVE
    _WATCH_PROCESS_ACTIVE = active


def open_archive(home: Optional[str] = None) -> ArchivePaths:
    """Open (and initialize) the archive at ``home`` (env / default if None).

    Switching homes within a process repoints everything atomically: the engine is
    rebuilt (``init_engine``) and the JSONL truth-log's cached append handles are
    dropped, so a second ``open_archive`` to a different home can't leave SQLite
    writing one store while JSONL appends resolve to another.
    """
    paths = resolve_paths(home).ensure()
    target = paths.sqlalchemy_url
    from ._store import active_dsn, init_db, init_engine, reconnect_if_swapped

    if active_dsn() is not None and active_dsn() != target:
        from ._truth import reset_handles

        reset_handles()  # stale handles point at the previous home's files
    # Pin the home so engine + truth + search resolve consistently for the process.
    os.environ[ENV_HOME] = str(paths.home)
    try:
        init_engine(target)  # rebuilds when the DSN changed
        # Read-path convergence: long-lived processes (an MCP server, the web
        # app) pass through here on every call, so a reindex's index.db swap is
        # picked up on the next call. Writers get the authoritative check on
        # ingest-lock *acquire* (see _truth.shared_ingest_lock) — this one runs
        # before any blocking wait and can go stale during it.
        reconnect_if_swapped()
        init_db()
    except Exception:
        # A failed first connection (corrupt index, permissions, failed PRAGMA)
        # must not leave its partially initialized pool pinned globally. Apart
        # from leaking the DB-API connection, a later call would keep retrying
        # through the poisoned engine instead of starting from a clean binding.
        from ._store import close_engine

        close_engine()
        raise
    # An archive becomes *known* by being used — no enrollment step to forget.
    # After the open succeeds, so a home that can't be opened is never advertised
    # as one of the user's archives; throttled and fail-soft inside register().
    from ._ops.archives import register

    register(paths.home)
    return paths


def close() -> None:
    """Dispose the engine so a different archive can be opened."""
    from ._store import close_engine
    from ._truth import reset_handles

    reset_handles()
    close_engine()


def search(
    query: str,
    *,
    home: Optional[str] = None,
    limit: int = 20,
    thread_id: Optional[int | str] = None,
    topic_id: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    exclude_content_types: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[list[str]] = None,
    types: Optional[list[str]] = None,
    agents: Optional[str] = None,
    startswith: Optional[str] = None,
    path: Optional[str] = None,
    path_ops: Optional[list[str]] = None,
    thread_ids: Optional[list[str]] = None,
    sort: Optional[str] = None,
    group: Optional[str] = None,
    output: Optional[str] = None,
    context_lines: int = 2,
    context_events: Optional[str] = None,
    rerank: Optional[bool] = None,
    match: str = "token",
    page: int = 1,
    params=None,
) -> "list[EventHit]":
    """Federated search over conversation events (lexical FTS5 + optional semantic
    vectors → RRF fusion → weighted rank → optional cross-encoder re-rank).
    Returns enriched event-hit dicts. ``source`` restricts to threads of the named
    provider(s); ``topic_id`` restricts to a topic's member conversations
    (a compatibility scope over existing KG records);
    ``agents`` controls agent-run threads (``thread_type='system'``): 'exclude'
    (default) / 'include' / 'only'; ``types`` restricts to the named
    ``thread_type`` values. An **empty query** is a browse — one row per thread by
    last activity, honoring the structural filters (see
    :func:`thread_archive._retrieval.browse.browse_threads`);
    ``startswith`` does a structural prefix scan; ``sort='oldest'``
    returns the pool chronologically; ``output`` ('count'/'linkable') and
    ``context_lines`` / ``context_events`` shape what each hit carries; ``rerank``
    forces the cross-encoder stage (else auto-gated to conceptual queries when the
    ``[embeddings]`` extra is present); ``params`` is a
    :class:`thread_archive._retrieval.SearchParams` retrieval configuration
    (default: the shipped weights — the search-lab experiment seam). The ranked shape returns one row per
    thread, repeats folded into ``_thread_more`` / ``_dup_thread_ids``
    annotations; ``group='none'`` returns every hit as its own row, and
    ``group='dup'`` folds only cross-thread duplicate content, keeping each
    surviving thread's own hits. ``group='browse'`` / ``group='nested'`` turn a
    keyword search into the thread-granular list shapes — matched threads alone,
    or every hit clustered under its thread — with ``limit`` counting threads.
    ``path`` (narrowed by ``path_ops``) restricts to the conversations that touched
    a file — the code axis; with an empty query that browse IS the "who worked on
    this file" answer, its rows carrying the op tally. ``thread_ids`` is a
    pre-resolved id-set scope for a caller that resolved the conversations itself.

    ``match`` picks what counts as a match: ``'token'`` (default) is the indexed
    FTS5 pipeline, ``'substring'`` an uncapped infix scan that reaches within-token
    matches the index cannot see (``p4`` in ``mp4``) at the price of a full-table
    scan. ``page`` (1-based) walks the result set; the returned
    :class:`~._retrieval._types.Results` — a plain ``list`` subclass — carries
    ``total`` / ``total_threads`` / ``pages`` and the ``exhaustive`` flag saying
    whether paging can reach every match."""
    open_archive(home)
    from ._retrieval import search as _search

    return _search(
        query,
        limit=limit,
        thread_id=thread_id,
        topic_id=topic_id,
        content_types=content_types,
        exclude_content_types=exclude_content_types,
        since=since,
        until=until,
        tool_name=tool_name,
        source=source,
        types=types,
        agents=agents,
        startswith=startswith,
        path=path,
        path_ops=path_ops,
        thread_ids=thread_ids,
        sort=sort,
        group=group,
        output=output,
        context_lines=context_lines,
        context_events=context_events,
        rerank=rerank,
        match=match,
        page=page,
        params=params,
    )


def blame(
    *,
    path: Optional[str] = None,
    commit: Optional[str] = None,
    thread_id: Optional[int | str] = None,
    home: Optional[str] = None,
    ops: Optional[list[str]] = None,
    limit: int = 20,
    since: Optional[str] = None,
    until: Optional[str] = None,
    source: Optional[list[str]] = None,
    agents: str = "exclude",
    repo: Optional[str] = None,
    refresh: bool = True,
) -> dict:
    """The code axis: which conversations touched a file, or produced a commit.

    Exactly one of ``path`` / ``commit`` / ``thread_id`` selects the direction —
    file → conversations, commit → the session behind it, conversation → its files.
    ``refresh`` tops up the projection first (bounded; the cursor makes the rest of
    a backfill the next call's work) so a just-finished session is answerable.
    """
    given = [n for n, v in (("path", path), ("commit", commit), ("thread_id", thread_id)) if v]
    if len(given) != 1:
        raise ValueError("blame takes exactly one of path=, commit=, thread_id= "
                         f"(got {given or 'none'})")
    open_archive(home)
    from ._retrieval import code as _code
    from ._retrieval.read import resolve_thread_ref
    from ._store import use_session

    if refresh:
        # Bounded: an MCP call must not block on a cold archive's whole backfill.
        _code.refresh_code_index(max_batches=8)
    if commit:
        return _code.blame_commit(commit, repo=repo, limit=limit)
    if path:
        return _code.blame_path(path, ops=ops, limit=limit, since=since, until=until,
                                sources=source, agents=agents)
    # Only the thread direction is left (the exactly-one check above). ``str`` is
    # lossless for every ref shape — an all-digit string resolves as the legacy id
    # an int would have.
    with use_session(None) as s:
        resolved = resolve_thread_ref(s, str(thread_id))
    if resolved is None:
        return {"thread_id": thread_id, "total_files": 0, "files": [],
                "note": "no such thread"}
    return _code.thread_files(resolved, ops=ops, limit=limit)


def code_index(*, home: Optional[str] = None, rebuild: bool = False) -> dict:
    """Fold the code axis forward (or ``rebuild`` it from scratch) and report the
    projection's counts + freshness."""
    open_archive(home)
    from ._retrieval import code as _code

    result = _code.rebuild_code_index() if rebuild else _code.refresh_code_index()
    return {**_code.code_index_status(), "folded": result}


def read_thread(
    thread_id: int | str,
    *,
    home: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    summary: bool | str = False,
    mode: Optional[str] = None,
    user_only: Optional[bool] = None,
    tool_results: bool = False,
    max_chars: int = 0,
    after_event: Optional[int] = None,
    around_event: Optional[int] = None,
    context_turns: int = 1,
) -> str:
    """Reconstruct a conversation thread as a readable transcript.

    ``thread_id`` is the archive's thread id or a provider **session id**
    (the uuid/source_id a tool knows the conversation by). ``mode`` picks the view —
    ``user`` (default), ``chat``, ``full``, ``last`` (final assistant text only), or
    ``ends`` (first + last ``context_turns`` turns) —
    and the read is turn-paginated +
    size-budgeted (``max_chars``, default ~48k). ``tool_results`` (default off) adds
    tool output under each call in ``full``. ``summary`` swaps in a summary view:
    ``True``/``'toc'`` = compact TOC, ``'short'`` / ``'indexed'`` = the stored thread
    summaries, ``'files'`` = the files this session touched (the code axis, read
    backwards from :func:`search`'s ``path`` scope). ``around_event`` opens a search-result event with
    ``context_turns`` turns of surrounding context; see
    :func:`thread_archive._retrieval.read_thread` for the full contract."""
    open_archive(home)
    from ._retrieval import read_thread as _read

    return _read(
        thread_id,
        limit=limit,
        offset=offset,
        summary=summary,
        mode=mode,
        user_only=user_only,
        tool_results=tool_results,
        max_chars=max_chars,
        after_event=after_event,
        around_event=around_event,
        context_turns=context_turns,
    )


def read_thread_structured(
    thread_id: int | str,
    *,
    home: Optional[str] = None,
    include_thinking: bool = True,
    include_tools: bool = True,
) -> dict:
    """Reconstruct a thread as structured messages (typed render blocks) for the web
    viewer. ``thread_id`` accepts a thread id (or legacy integer id) or a provider session id.
    Returns ``{thread_id, title, source, messages}``; see
    :func:`thread_archive._retrieval.read_thread_structured`."""
    open_archive(home)
    from ._retrieval import read_thread_structured as _read

    return _read(thread_id, include_thinking=include_thinking, include_tools=include_tools)


def import_path(path, *, home: Optional[str] = None, provider: str = "claude-code", source_id: Optional[str] = None):
    """Import a transcript (line-stream providers) or scan a DB (cursor/opencode).

    Each thread's metadata record and events are written to its own truth file as
    the import commits, so the per-thread files are already a complete restore set.
    The maintenance pass afterward just keeps the shard layout balanced and advances
    the manifest watermark — conversation import never changes the cross-thread
    overlays, so it does not rewrite those snapshots.
    """
    open_archive(home)
    from ._importers import db_scanners, line_stream_importers
    from ._truth import checkpoint as _checkpoint
    from ._truth import shared_ingest_lock

    line_streams = line_stream_importers(home)
    scanners = db_scanners(home)
    p = Path(path)
    # Held shared across the truth append AND the SQLite commit: an unlocked import
    # racing a reindex can land in the truth after the rebuild's read point and
    # commit into the inode the swap replaces. Blocks (bounded by one rebuild)
    # rather than skipping — a one-shot import has no later pass to retry on.
    result: object
    with shared_ingest_lock():
        if provider in line_streams:
            result = line_streams[provider](p, source_id or p.stem)
        elif provider in scanners:
            result = scanners[provider](p)
        else:
            raise ValueError(f"unknown provider {provider!r}")

        _checkpoint(snapshots=False)
    return result


def reindex(*, home: Optional[str] = None, vectors: bool = False, salvage: bool = False) -> dict:
    """Rebuild index.db (relational + FTS) from the JSONL truth directory.

    Fails closed when the rebuild would lose committed records the current index
    holds (damaged truth lines, a deleted thread file) — the old index stays live
    and the error names the loss; ``salvage=True`` publishes the lossy rebuild
    anyway. Crash artifacts (torn lines that never committed) never block."""
    open_archive(home)
    from ._truth import reindex as _reindex

    return _reindex(vectors=vectors, salvage=salvage)


def migrate(*, home: Optional[str] = None, dry_run: bool = False) -> dict:
    """Migrate older truth to the current format, then rebuild and verify it."""
    from ._config import resolve_paths
    from ._scripts.migrate_thread_ulids import migrate as _migrate

    result = _migrate(resolve_paths(home).home, dry_run=dry_run)
    if result.get("changed") and not dry_run:
        result["reindex"] = reindex(home=home)
        result["verify"] = verify(home=home)
        if not result["verify"].get("ok"):
            raise RuntimeError(
                "truth migration completed, but post-migration verification failed"
            )
    return result


def embed(
    *,
    home: Optional[str] = None,
    rebuild: bool = False,
    max_events: Optional[int] = None,
    newest_first: bool = False,
    embedder=None,
    progress=None,
) -> dict:
    """Embed the user/text events that are missing a vector (incremental anti-join).

    The catch-up counterpart to the watcher's live cohost: ``reindex(vectors=True)``
    rebuilds the whole vector index, this just fills the gap. ``rebuild=True``
    re-embeds everything; ``max_events`` caps one call; ``newest_first`` drains the
    freshest gap first (recent threads become semantically findable soonest).
    ``embedder`` is the model the vectors are computed with (default: the process
    embedder, and its space is the one queries resolve in) — the coordination
    layer passes it straight through to
    :func:`thread_archive._retrieval.vectors.index_events_local`, so a caller
    holding a loaded model, or indexing into a second embedding space, does not
    have to reach past this surface for it. No-op without the ``[embeddings]``
    extra. Returns ``{'embedded': n}``.

    The run is tracked: progress, rate, and ETA are published to
    ``<home>/load-state.json`` for any other process to read (:func:`load_status`),
    and a summary lands in the load ledger. ``progress`` is an optional callable
    taking the run snapshot, for a foreground caller that wants to show it —
    a cold embed is measured in hours and must not look like a hang."""
    paths = open_archive(home)
    from ._ops.load_runs import load_run
    from ._retrieval.vectors import index_events_local

    with load_run("embed", home=paths.home, reporter=progress) as run:
        # Events vary hugely in size and the drain length-sorts them on purpose,
        # so events-per-second is not a comparable quantity across the phase;
        # chunks are the uniform unit the encoder actually consumes.
        with run.phase("embed", work_unit="chunks") as ph:
            n = index_events_local(rebuild=rebuild, max_events=max_events,
                                   newest_first=newest_first, embedder=embedder,
                                   phase=ph)
    return {"embedded": n}


def checkpoint(*, home: Optional[str] = None) -> dict:
    """Snapshot the mutable authored tables (threads) to the JSONL truth log."""
    open_archive(home)
    from ._truth import checkpoint as _checkpoint

    return _checkpoint()


def watch(*, home: Optional[str] = None, interval: float = 5.0, once: bool = False,
          progress=None):
    """Watch local AI-tool stores and import incrementally. Blocks unless ``once``.

    A one-shot sweep (``once=True``) is the bulk catch-up a new archive loads
    through — the first import that makes search usable — so it is tracked: its
    progress, rate, and ETA are published to ``<home>/load-state.json`` and a
    summary lands in the load ledger (:func:`load_status`), and ``progress`` (a
    callable taking the run snapshot) lets a foreground caller show it. The
    continuous loop is steady-state, not a load: it keeps its own pass heartbeat
    and writes no per-poll ledger rows."""
    paths = open_archive(home)
    from ._truth import shared_ingest_lock
    from ._watcher import Watcher

    watcher = Watcher(home=home, interval=interval)
    if once:
        # run() takes the shared reindex lock per pass; a one-shot poll needs the
        # same coverage (blocking — it has no next pass to retry on).
        from ._ops.load_runs import load_run

        with load_run("import", home=paths.home, reporter=progress) as run:
            with run.phase("import") as ph, shared_ingest_lock():
                return watcher.poll_once(phase=ph)
    watcher.run()
    return None


def status(*, home: Optional[str] = None) -> dict:
    """Archive health: paths + thread/event/topic/link/FTS counts, plus the
    operational records — when the last checkpoint, verify, and backup ran and
    how they went (``last_verify`` / ``last_backup`` from ``<home>/health.json``,
    written by :func:`verify` / :func:`backup`). Staleness here is the signal
    that a scheduled integrity job quietly stopped running."""
    paths = open_archive(home)
    from sqlalchemy import func, select

    from ._retrieval import fts_status
    from ._store import Event, Thread, ThreadLink, get_session

    with get_session() as s:
        threads = s.execute(select(func.count()).select_from(Thread)).scalar() or 0
        events = s.execute(select(func.count()).select_from(Event)).scalar() or 0
        topics = s.execute(
            select(func.count()).select_from(Thread).where(Thread.thread_type == "topic")
        ).scalar() or 0
        links = s.execute(select(func.count()).select_from(ThreadLink)).scalar() or 0
    from ._retrieval.code import code_index_status
    from ._retrieval.vectors import get_status as _vec_status

    code = code_index_status()
    return {
        "home": str(paths.home),
        "truth_dir": str(paths.truth_dir),
        "index_path": str(paths.index_path),
        "threads": int(threads),
        "events": int(events),
        "topics": int(topics),
        "links": int(links),
        "fts_indexed": fts_status()["indexed"],
        "vectors_indexed": _vec_status().get("indexed", 0),
        "code_paths_indexed": code["paths"],
        "code_files": code["distinct_paths"],
        "code_commits": code["commits"],
        # False while the fold is still walking the log — the one state where an
        # empty blame answer means "not indexed yet", not "nobody touched it".
        # ``code_pending`` distinguishes ordinary between-passes lag from a real
        # backfill; the read paths top the fold up before querying either way.
        "code_current": code["current"],
        "code_pending": code["pending"],
        **operational_records(home=home),
    }


def operational_records(*, home: Optional[str] = None) -> dict:
    """The freshness-bearing half of :func:`status`: the ``health.json`` records,
    the pipeline verdict, watcher liveness, the backup's same-device check, and
    the load state.

    Split out because every field here is judged against *now* — a reader asks
    "how long since capture last checked in?" and answers red past a threshold.
    Costed to be read fresh on every request (a JSON file, a manifest, two
    stats, one ``kill(pid, 0)``) so it never has to ride a cache: served from a
    snapshot even minutes old, a live watcher's last pass reads as a stall.
    """
    paths = open_archive(home)
    from ._truth.jsonl_log import _read_manifest

    health = read_health()
    watch_pass = health.get("watch_pass_last")
    backup_record = health.get("backup_last") or {}

    watch_process_alive = _WATCH_PROCESS_ACTIVE
    if isinstance(watch_pass, dict):
        try:
            pid = int(watch_pass.get("pid", 0))
            if pid > 0:
                os.kill(pid, 0)
                watch_process_alive = True
        except (OSError, TypeError, ValueError):
            pass

    backup_same_device = None
    backup_dest = backup_record.get("dest") if isinstance(backup_record, dict) else None
    if backup_dest:
        try:
            # A destination may have been unmounted or removed since the last
            # run. Walk to its nearest existing parent so the warning still
            # works before the next backup recreates the leaf.
            dest_probe = Path(str(backup_dest)).expanduser()
            while not dest_probe.exists() and dest_probe != dest_probe.parent:
                dest_probe = dest_probe.parent
            backup_same_device = paths.home.stat().st_dev == dest_probe.stat().st_dev
        except OSError:
            pass

    return {
        "last_checkpoint_at": _read_manifest(paths.truth_dir).get("last_checkpoint_at"),
        "last_verify": health.get("verify_last"),
        "last_backup": health.get("backup_last"),
        "last_restore_drill": health.get("restore_drill_last"),
        "last_nightly": health.get("nightly_last"),
        "last_watch_errors": health.get("watch_errors_last"),
        "last_watch_pass": health.get("watch_pass_last"),
        "last_coverage": health.get("coverage_last"),
        "last_source_mirror": health.get("source_mirror_last"),
        "last_self_update": health.get("self_update_last"),
        "pipeline": pipeline_verdict(health),
        "watch_process_alive": watch_process_alive,
        "backup_same_device": backup_same_device,
        "load": _load_state(paths.home),
    }


def _load_state(home: Path) -> dict:
    from ._ops.load_runs import read_state

    return read_state(home)


def load_status(*, home: Optional[str] = None, limit: int = 20) -> dict:
    """How loading this archive is going: the live state of any in-flight load
    (phase, progress, rate, ETA) plus the recent finished runs.

    ``current`` is the record a long load publishes as it works, so progress is
    readable from outside the process doing it; ``recent`` is the history, which is
    what turns "the embed felt slow" into a per-phase series. A ``current`` whose
    writing process is gone comes back as ``stalled`` rather than ``running``."""
    paths = open_archive(home)
    from ._ops.load_runs import read_runs, read_state

    return {"home": str(paths.home), "current": read_state(paths.home),
            "recent": read_runs(limit, paths.home)}


def archives(*, home: Optional[str] = None) -> list[dict]:
    """Every known archive with its load state — including archives this process
    has not opened. An archive is registered by being opened, so this is the list
    of homes that have been used, each enriched live from its own directory."""
    paths = open_archive(home)
    from ._ops.archives import list_archives

    return list_archives(active_home=paths.home)


def stats(*, home: Optional[str] = None, model_limit: Optional[int] = None) -> dict:
    """Token/cost analytics for the viewer's stats page: an ``overview`` (totals +
    activity span), ``by_source`` (conversations, tokens, and — where a pay-per-token
    source recorded it — cost, per provider), and ``by_model`` (the busiest models).

    Cost is only present for sources that record it (the pay-per-token harnesses, e.g.
    sources); subscription tools log tokens but no dollar figure, so their cost comes back
    null rather than a fabricated estimate. Backed by an incrementally-maintained rollup
    (:mod:`._store._metrics`) so it stays fast on a large archive — the first call after
    a reindex pays a one-time survey, the rest fold only new events."""
    open_archive(home)
    from ._store._metrics import collect_stats

    return collect_stats(model_limit=model_limit)


def model_stats(model: str, *, home: Optional[str] = None) -> Optional[dict]:
    """One model's drill-down for the viewer's per-model page: overview totals,
    a per-session token distribution (min/median/avg/max), a monthly time series
    (sessions, tokens, compactions), and its heaviest sessions. None when the model
    has no live conversation data. Same rollup as :func:`stats`; see
    :func:`._store._metrics.collect_model_stats` for the semantics."""
    open_archive(home)
    from ._store._metrics import collect_model_stats

    return collect_model_stats(model)


def repair(*, home: Optional[str] = None, dry_run: bool = False) -> dict:
    """Quarantine unparseable truth lines and restore committed rows the truth
    lacks from the live index — the sanctioned path from a red ``verify`` back to
    green. See :func:`thread_archive._truth.repair.repair_truth`."""
    open_archive(home)
    from ._truth import repair_truth

    return repair_truth(dry_run=dry_run)


def amend(
    patches: list[tuple[str, int, dict]], *,
    reason: Optional[str] = None, home: Optional[str] = None,
) -> dict:
    """Merge non-content fields onto events' payloads via superseding truth
    lines — the append-only edit mechanism. ``patches`` is
    ``[(thread_id, event_id, {field: value}), ...]``. See
    :mod:`thread_archive._ops.amend`."""
    open_archive(home)
    from ._ops.amend import amend_event_payloads

    return amend_event_payloads(patches, reason=reason)


def amendments(*, home: Optional[str] = None) -> list[dict]:
    """Every amendment audit record, in append order."""
    open_archive(home)
    from ._ops.amend import load_amendments

    return load_amendments()
