"""The public Python API for thread-archive.

The native surface (the MCP server and CLI are built on top of these). Each call
opens the archive — resolves the home, ensures its directories, pins it for the
process, and initializes the SQLite engine + schema — then dispatches into the
store / importers / search / truth / watch layers. Internal imports are lazy so
``import thread_archive`` stays light and pulls in no server backends.

One archive per process: ``open_archive`` pins ``$THREAD_ARCHIVE_HOME`` so the
engine (index.db), the truth log (truth/), and search all resolve to the same
home. To switch archives, call :func:`close` first.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .config import ENV_HOME, ArchivePaths, resolve_paths


def open_archive(home: Optional[str] = None) -> ArchivePaths:
    """Open (and initialize) the archive at ``home`` (env / default if None).

    Switching homes within a process repoints everything atomically: the engine is
    rebuilt (``init_engine``) and the JSONL truth-log's cached append handles are
    dropped, so a second ``open_archive`` to a different home can't leave SQLite
    writing one store while JSONL appends resolve to another.
    """
    paths = resolve_paths(home).ensure()
    target = paths.sqlalchemy_url
    from .store import active_dsn, init_db, init_engine

    if active_dsn() is not None and active_dsn() != target:
        from .truth import reset_handles

        reset_handles()  # stale handles point at the previous home's files
    # Pin the home so engine + truth + search resolve consistently for the process.
    os.environ[ENV_HOME] = str(paths.home)
    init_engine(target)  # rebuilds when the DSN changed
    init_db()
    return paths


def close() -> None:
    """Dispose the engine so a different archive can be opened."""
    from .store import close_engine
    from .truth import reset_handles

    reset_handles()
    close_engine()


def search(
    query: str,
    *,
    home: Optional[str] = None,
    limit: int = 20,
    thread_id: Optional[int] = None,
    content_types: Optional[list[str]] = None,
    exclude_content_types: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[list[str]] = None,
    startswith: Optional[str] = None,
    sort: Optional[str] = None,
    output: Optional[str] = None,
    context_lines: int = 2,
    context_events: Optional[str] = None,
    rerank: Optional[bool] = None,
) -> list[dict]:
    """Federated search over conversation events (lexical FTS5 + optional semantic
    vectors → RRF fusion → weighted rank → optional cross-encoder re-rank). Returns
    enriched event-hit dicts. ``source`` restricts to threads of the named
    provider(s); ``startswith`` does a structural prefix scan; ``sort='oldest'``
    returns the pool chronologically; ``output`` ('count'/'linkable') and
    ``context_lines`` / ``context_events`` shape what each hit carries; ``rerank``
    forces the cross-encoder stage (else auto-gated to conceptual queries when the
    ``[embeddings]`` extra is present)."""
    open_archive(home)
    from .retrieval import search as _search

    return _search(
        query,
        limit=limit,
        thread_id=thread_id,
        content_types=content_types,
        exclude_content_types=exclude_content_types,
        since=since,
        until=until,
        tool_name=tool_name,
        source=source,
        startswith=startswith,
        sort=sort,
        output=output,
        context_lines=context_lines,
        context_events=context_events,
        rerank=rerank,
    )


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
) -> str:
    """Reconstruct a conversation thread as a readable transcript.

    ``thread_id`` is the archive's integer thread id or a provider **session id**
    (the uuid/source_id a tool knows the conversation by). ``mode`` picks the view —
    ``user`` (default), ``chat``, or ``full`` — and the read is turn-paginated +
    size-budgeted (``max_chars``, default ~48k). ``tool_results`` (default off) adds
    tool output under each call in ``full``. ``summary`` swaps in a summary view:
    ``True``/``'toc'`` = compact TOC, ``'short'`` / ``'indexed'`` = the stored thread
    summaries; see :func:`thread_archive.retrieval.read_thread` for the full contract."""
    open_archive(home)
    from .retrieval import read_thread as _read

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
    )


def read_thread_structured(
    thread_id: int | str,
    *,
    home: Optional[str] = None,
    include_thinking: bool = True,
    include_tools: bool = True,
) -> dict:
    """Reconstruct a thread as structured messages (typed render blocks) for the web
    viewer. ``thread_id`` accepts an integer thread id or a provider session id.
    Returns ``{thread_id, title, source, messages}``; see
    :func:`thread_archive.retrieval.read_thread_structured`."""
    open_archive(home)
    from .retrieval import read_thread_structured as _read

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
    from .importers import DB_SCANNERS, LINE_STREAM_IMPORTERS

    p = Path(path)
    if provider in LINE_STREAM_IMPORTERS:
        result = LINE_STREAM_IMPORTERS[provider](p, source_id or p.stem)
    elif provider in DB_SCANNERS:
        result = DB_SCANNERS[provider](p)
    else:
        raise ValueError(f"unknown provider {provider!r}")

    from .truth import checkpoint as _checkpoint

    _checkpoint(snapshots=False)
    return result


def reindex(*, home: Optional[str] = None, vectors: bool = False) -> dict:
    """Rebuild index.db (relational + FTS) from the JSONL truth directory."""
    open_archive(home)
    from .truth import reindex as _reindex

    return _reindex(vectors=vectors)


def embed(
    *,
    home: Optional[str] = None,
    rebuild: bool = False,
    max_events: Optional[int] = None,
    newest_first: bool = False,
) -> dict:
    """Embed the user/text events that are missing a vector (incremental anti-join).

    The catch-up counterpart to the watcher's live cohost: ``reindex(vectors=True)``
    rebuilds the whole vector index, this just fills the gap. ``rebuild=True``
    re-embeds everything; ``max_events`` caps one call; ``newest_first`` drains the
    freshest gap first (recent threads become semantically findable soonest). No-op
    without the ``[embeddings]`` extra. Returns ``{'embedded': n}``."""
    open_archive(home)
    from .retrieval.vectors import index_events_local

    n = index_events_local(rebuild=rebuild, max_events=max_events, newest_first=newest_first)
    return {"embedded": n}


def checkpoint(*, home: Optional[str] = None) -> dict:
    """Snapshot the mutable authored tables (threads) to the JSONL truth log."""
    open_archive(home)
    from .truth import checkpoint as _checkpoint

    return _checkpoint()


def watch(*, home: Optional[str] = None, interval: float = 5.0, once: bool = False):
    """Watch local AI-tool stores and import incrementally. Blocks unless ``once``."""
    open_archive(home)
    from .watcher import Watcher

    watcher = Watcher(interval=interval)
    if once:
        return watcher.poll_once()
    watcher.run()
    return None


def _mirror_dir(src: Path, dest: Path) -> tuple[int, int]:
    """Incrementally mirror ``src`` into ``dest`` (skip files unchanged by size +
    mtime). Returns ``(files_copied, bytes_copied)``. ``copy2`` preserves mtime so a
    re-run copies only what changed — the truth dir is append-mostly, so a periodic
    backup moves little."""
    import shutil

    copied = total = 0
    for sp in src.rglob("*"):
        if sp.is_dir():
            continue
        dp = dest / sp.relative_to(src)
        if dp.exists():
            ss, ds = sp.stat(), dp.stat()
            if ss.st_size == ds.st_size and int(ss.st_mtime) <= int(ds.st_mtime):
                continue
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, dp)
        copied += 1
        total += sp.stat().st_size
    return copied, total


def backup(dest: str, *, home: Optional[str] = None) -> dict:
    """Back the archive up by mirroring its JSONL truth directory to ``dest``.

    A ``cp``/``rsync`` of the truth dir *is* the backup (index.db is rebuildable from
    it), so this checkpoints first — flushing the cross-thread overlays + any thread-
    metadata updates so the on-disk truth is a complete restore set — then mirrors
    ``truth/`` into ``dest`` incrementally. Point ``dest`` at a *different disk /
    machine*: for pre-retention history the truth log is the only copy.
    """
    open_archive(home)
    from .truth import checkpoint as _checkpoint

    _checkpoint()  # full: overlays + metadata-update backstop → truth is a complete restore set
    paths = resolve_paths(home)
    dest_path = Path(dest).expanduser()
    dest_path.mkdir(parents=True, exist_ok=True)
    copied, total = _mirror_dir(paths.truth_dir, dest_path)
    return {
        "truth_dir": str(paths.truth_dir),
        "dest": str(dest_path),
        "files_copied": copied,
        "bytes_copied": total,
    }


def verify(*, home: Optional[str] = None) -> dict:
    """Integrity check: the JSONL truth parses cleanly and matches the SQLite index.

    Scans every per-thread truth file (counting threads + events, tallying parse
    errors) and compares to the projection's counts. ``ok`` is True only when the
    counts align and nothing failed to parse. A negative event drift (truth > index)
    is the *safe* direction — ``archive reindex`` rebuilds the index from truth; a
    positive drift (index > truth) or any parse error is a real integrity problem.
    """
    open_archive(home)
    from sqlalchemy import func, select

    from .store import Event, Thread, get_session
    from .truth import scan_truth_counts

    truth = scan_truth_counts()
    with get_session() as s:
        idx_threads = s.execute(select(func.count()).select_from(Thread)).scalar() or 0
        idx_events = s.execute(select(func.count()).select_from(Event)).scalar() or 0
    drift_threads = int(idx_threads) - truth["threads"]
    drift_events = int(idx_events) - truth["events"]
    return {
        "ok": drift_threads == 0 and drift_events == 0 and truth["parse_errors"] == 0,
        "truth": truth,
        "index": {"threads": int(idx_threads), "events": int(idx_events)},
        "drift": {"threads": drift_threads, "events": drift_events},
    }


def status(*, home: Optional[str] = None) -> dict:
    """Archive health: paths + thread/event/topic/link/FTS counts."""
    paths = open_archive(home)
    from sqlalchemy import func, select

    from .retrieval import fts_status
    from .store import Event, Thread, ThreadLink, get_session

    with get_session() as s:
        threads = s.execute(select(func.count()).select_from(Thread)).scalar() or 0
        events = s.execute(select(func.count()).select_from(Event)).scalar() or 0
        topics = s.execute(
            select(func.count()).select_from(Thread).where(Thread.thread_type == "topic")
        ).scalar() or 0
        links = s.execute(select(func.count()).select_from(ThreadLink)).scalar() or 0
    from .retrieval.vectors import get_status as _vec_status

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
    }


def knowledge_status(*, home: Optional[str] = None) -> dict:
    """Topic-graph status: node/community/component counts (empty until topics exist)."""
    open_archive(home)
    from .knowledge import get_status

    return get_status()


def bridge_topics(*, home: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Highest-betweenness topics — the structural bridges between communities."""
    open_archive(home)
    from .knowledge import get_bridge_topics

    return get_bridge_topics(limit=limit)


def topic_peers(thread_id: int, *, home: Optional[str] = None, limit: int = 5) -> list[dict]:
    """Topics in the same community as ``thread_id``, highest-pagerank first."""
    open_archive(home)
    from .knowledge import get_community_peers

    return get_community_peers(thread_id, limit=limit)
