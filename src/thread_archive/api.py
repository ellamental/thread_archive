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

# (st_dev, st_ino) of index.db at the last open_archive — swap detection.
_index_ident: Optional[tuple[int, int]] = None


def _reconnect_if_swapped(paths: ArchivePaths) -> None:
    """Dispose pooled connections when ``index.db`` was atomically replaced.

    ``reindex`` publishes by renaming a freshly built database over ``index.db``;
    another process's pooled connections keep the *old inode* open — their reads are
    frozen at the pre-swap state and their commits write a file nothing else can see.
    Every API call passes through :func:`open_archive`, so comparing the file identity
    here makes long-lived processes (the librarian MCP, the web app) converge on the
    new index on their next call."""
    global _index_ident
    try:
        st = os.stat(paths.index_path)
    except OSError:
        _index_ident = None
        return
    ident = (st.st_dev, st.st_ino)
    if _index_ident is not None and ident != _index_ident:
        from .store import get_engine

        get_engine().dispose()
    _index_ident = ident


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
    _reconnect_if_swapped(paths)
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
    from .truth import checkpoint as _checkpoint
    from .truth import shared_ingest_lock

    p = Path(path)
    # Held shared across the truth append AND the SQLite commit: an unlocked import
    # racing a reindex can land in the truth after the rebuild's read point and
    # commit into the inode the swap replaces. Blocks (bounded by one rebuild)
    # rather than skipping — a one-shot import has no later pass to retry on.
    with shared_ingest_lock():
        if provider in LINE_STREAM_IMPORTERS:
            result = LINE_STREAM_IMPORTERS[provider](p, source_id or p.stem)
        elif provider in DB_SCANNERS:
            result = DB_SCANNERS[provider](p)
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
    from .truth import reindex as _reindex

    return _reindex(vectors=vectors, salvage=salvage)


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
    from .truth import shared_ingest_lock
    from .watcher import Watcher

    watcher = Watcher(interval=interval)
    if once:
        # run() takes the shared reindex lock per pass; a one-shot poll needs the
        # same coverage (blocking — it has no next pass to retry on).
        with shared_ingest_lock():
            return watcher.poll_once()
    watcher.run()
    return None


# Delete-sync sanity bound: refuse to delete more than this fraction of the
# destination's files (once past the absolute floor). A mass-wipe of the source —
# a bug emptying truth/, an accidental rm — must not propagate to the last backup;
# a legitimate mass-move (shard rebalance) re-homes files, so the copies land
# first and the stale paths deleted stay a bounded fraction only when the copy
# half of the mirror actually ran.
_MIRROR_DELETE_FLOOR = 64
_MIRROR_DELETE_MAX_FRACTION = 0.25


def _is_append_only_truth(rel: Path) -> bool:
    """True for truth files that only ever grow in normal operation: the per-thread
    files and the curatorial event log. The cross-thread overlay snapshots and
    ``import_state.jsonl`` are full rewrites and may legitimately shrink."""
    return (
        rel.parts[:1] == ("threads",) or rel.name == "kg_events.jsonl"
    ) and rel.suffix == ".jsonl"


def _atomic_copy(sp: Path, dp: Path, *, trim_to_newline: bool) -> None:
    """Copy ``sp`` over ``dp`` with no destructive window: the bytes land in a
    same-directory temp file, fsynced, then an atomic rename publishes them — a
    crash or disk-full mid-copy can never leave ``dp`` partial or destroy its
    previous good copy. ``copy2`` under the hood, so mtime rides along and the
    mirror's unchanged-skip keeps working.

    ``trim_to_newline`` (append-only truth files) drops an unterminated final
    fragment from the copy: the mirror doesn't quiesce writers, so a copy racing
    a live append can catch half a line — trimmed to the last newline, every
    published backup copy is a clean, parseable prefix of its source, and the
    full line arrives with the next run."""
    import shutil

    tmp = dp.parent / f".{dp.name}.tmp-{os.getpid()}"
    try:
        shutil.copy2(sp, tmp)
        with open(tmp, "rb+") as fh:
            size = fh.seek(0, os.SEEK_END)
            if trim_to_newline and size:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    pos, last_nl = size, -1
                    while pos > 0:
                        step = min(65536, pos)
                        fh.seek(pos - step)
                        idx = fh.read(step).rfind(b"\n")
                        if idx >= 0:
                            last_nl = pos - step + idx
                            break
                        pos -= step
                    fh.truncate(last_nl + 1)
            os.fsync(fh.fileno())
        os.replace(tmp, dp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _mirror_dir(
    src: Path, dest: Path, *, delete: bool = False, allow_shrink: bool = False
) -> dict:
    """Incrementally mirror ``src`` into ``dest`` (skip files unchanged by size +
    mtime). Each file is published atomically (:func:`_atomic_copy`), preserving
    mtime so a re-run copies only what changed — the truth dir is append-mostly,
    so a periodic backup moves little.

    ``delete=True`` makes it a true mirror: destination files with no source
    counterpart are removed (and emptied directories pruned). Without it the mirror
    is additive, and a shard rebalance — which *moves* thread files to new paths —
    leaves the backup holding both layouts. Deletion is bounded: when the planned
    deletions exceed both the absolute floor and the fraction cap of the
    destination's files, they are skipped (``deletions_skipped``) — the mirror
    stays additive rather than letting a gutted source strip the backup.

    **Re-homed twins are exempt from that bound.** A rebalance moves every thread
    file at once, so the stale old-layout copies at the destination can vastly
    exceed the cap — and skipping them forever leaves the backup double-sized and
    holding records a restore must not see. A doomed ``threads/**`` file whose
    thread has a file at the source's canonical shard depth — present at *both*
    ends, with the destination's canonical copy at least as large as the stale
    one — is provably superseded (the sweep moves/merges, never truncates), so it
    is deleted (``rehomed_twins_deleted``) regardless of the cap. Everything else
    stays capped.

    **Shrink guard.** An append-only truth file (``threads/**``, ``kg_events.jsonl``)
    whose source copy is *smaller* than its backup copy means the source lost data —
    truncation, corruption, an accidental overwrite. Copying it would destroy the
    last good copy, so the guard keeps the destination file and counts the skip
    (``shrinks_skipped`` + sample); the trailing size check reports it as divergent
    until it is investigated. ``allow_shrink=True`` is the deliberate override for
    an understood re-emit (``rebuild_truth_from_store`` legitimately rewrites files
    smaller)."""
    from .truth.jsonl_log import _fsync_dir

    copied = total = deleted = skipped = shrinks = 0
    shrink_sample: list[str] = []
    synced_dirs: set[Path] = set()
    for sp in src.rglob("*"):
        if sp.is_dir():
            continue
        rel = sp.relative_to(src)
        dp = dest / rel
        if dp.exists():
            ss, ds = sp.stat(), dp.stat()
            if ss.st_size == ds.st_size and int(ss.st_mtime) <= int(ds.st_mtime):
                continue
            if (
                not allow_shrink
                and ss.st_size < ds.st_size
                and _is_append_only_truth(rel)
            ):
                shrinks += 1
                if len(shrink_sample) < 10:
                    shrink_sample.append(str(rel))
                continue
        dp.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(sp, dp, trim_to_newline=_is_append_only_truth(rel))
        copied += 1
        total += dp.stat().st_size
        synced_dirs.add(dp.parent)
    for sd in synced_dirs:
        _fsync_dir(sd)  # the renames that published this pass's copies must stick
    twins_deleted = 0
    if delete:
        dest_files = [dp for dp in dest.rglob("*") if not dp.is_dir()]
        doomed = [dp for dp in dest_files if not (src / dp.relative_to(dest)).exists()]
        if doomed:
            twins, doomed = _split_rehomed_twins(src, dest, doomed)
            for dp in twins:
                dp.unlink()
                twins_deleted += 1
                deleted += 1
        limit = max(_MIRROR_DELETE_FLOOR, int(len(dest_files) * _MIRROR_DELETE_MAX_FRACTION))
        if len(doomed) > limit:
            skipped = len(doomed)
        else:
            for dp in doomed:
                dp.unlink()
                deleted += 1
        if deleted:
            for dp in sorted((p for p in dest.rglob("*") if p.is_dir()), reverse=True):
                try:
                    dp.rmdir()  # only succeeds when empty
                except OSError:
                    pass
    return {
        "files_copied": copied,
        "bytes_copied": total,
        "files_deleted": deleted,
        "rehomed_twins_deleted": twins_deleted,
        "deletions_skipped": skipped,
        "shrinks_skipped": shrinks,
        "shrink_sample": shrink_sample,
    }


def _split_rehomed_twins(src: Path, dest: Path, doomed: list[Path]) -> tuple[list[Path], list[Path]]:
    """Partition planned mirror deletions into provably-superseded rebalance
    twins and everything else (see :func:`_mirror_dir`). A twin qualifies only
    when its thread's canonical-depth file exists at the source *and* the
    destination's copy of that canonical file is at least as large as the stale
    one — the rebalance sweep moves/merges whole files, so a genuine re-home can
    never leave the canonical copy smaller."""
    from .truth.jsonl_log import THREADS_SUBDIR, _shard_depth, _thread_relpath

    depth = _shard_depth(src)
    twins: list[Path] = []
    rest: list[Path] = []
    for dp in doomed:
        rel = dp.relative_to(dest)
        if rel.parts[0] == THREADS_SUBDIR and dp.suffix == ".jsonl":
            try:
                tid = int(dp.stem)
            except ValueError:
                tid = None
            if tid is not None:
                canonical = _thread_relpath(tid, depth)
                try:
                    if (
                        canonical != rel
                        and (src / canonical).exists()
                        and (dest / canonical).stat().st_size >= dp.stat().st_size
                    ):
                        twins.append(dp)
                        continue
                except OSError:  # canonical dest copy missing/unreadable — stay capped
                    pass
        rest.append(dp)
    return twins, rest


def backup(
    dest: str,
    *,
    home: Optional[str] = None,
    allow_shrink: bool = False,
    verify_first: bool = True,
) -> dict:
    """Back the archive up by mirroring its JSONL truth directory to ``dest``.

    A ``cp``/``rsync`` of the truth dir *is* the backup (index.db is rebuildable from
    it), so this checkpoints first — flushing the cross-thread overlays + any thread-
    metadata updates so the on-disk truth is a complete restore set — then mirrors
    ``truth/`` into ``dest`` incrementally. Point ``dest`` at a *different disk /
    machine*: for pre-retention history the truth log is the only copy.

    The source is verified before it is mirrored (``verify_first``): a truth dir
    that fails the shallow integrity check (parse errors, index ⊃ truth drift) is
    still backed up — a flawed copy beats no copy — but *additively*: delete-sync
    is disabled for the run so a sick source can't strip the last good backup, and
    ``verify_ok=False`` in the result flags the run for investigation.

    The mirror holds the rebalance lock so a shard sweep can't move files under it
    (which could otherwise leave a moved file in *neither* layout in the backup for
    a whole cycle), and it finishes with a structural check — per-file size parity
    of every source ``.jsonl`` against its destination copy (``mirror_complete``).
    Live appends between the copy pass and the check make a file *larger* at the
    source; that isn't a mirror failure, so the check tolerates dest ≤ src growth
    on files it copied and only flags missing or divergent copies. Append-only
    truth files are additionally shrink-guarded (see :func:`_mirror_dir`);
    ``allow_shrink=True`` overrides after a deliberate truth re-emit.
    """
    open_archive(home)
    from .truth import checkpoint as _checkpoint
    from .truth.jsonl_log import _try_rebalance_lock

    verify_ok = True
    if verify_first:
        verify_ok = bool(verify(home=home)["ok"])

    _checkpoint()  # full: overlays + metadata-update backstop → truth is a complete restore set
    paths = resolve_paths(home)
    # Refresh the durable vector cache so live-embedded vectors (the watcher's
    # cohost writes them into index.db only) survive an index loss and ride the
    # mirror. No-op when the store holds no vectors — an empty save never
    # replaces a populated sidecar.
    from .retrieval.vectors import save_vectors_sidecar

    vectors_cached = save_vectors_sidecar(paths.truth_dir)
    dest_path = Path(dest).expanduser()
    dest_path.mkdir(parents=True, exist_ok=True)
    # Delete-sync only when the source looks like a real, checkpointed truth dir —
    # a mirror of an empty/foreign source must never strip a good backup — and only
    # when it verified clean (a sick source mirrors additively; see docstring).
    delete = (paths.truth_dir / "manifest.json").exists() and verify_ok
    with _try_rebalance_lock() as held:
        # If a rebalance sweep is mid-flight, mirror additively (no deletions):
        # copies of both layouts are safe; stale-path deletion waits for the next run.
        result = _mirror_dir(
            paths.truth_dir, dest_path, delete=delete and held, allow_shrink=allow_shrink
        )

    # Structural completeness: every source .jsonl must exist at the destination,
    # at ≥ its size at copy time (append-only files may have grown since).
    missing = divergent = 0
    for sp in paths.truth_dir.rglob("*.jsonl"):
        dp = dest_path / sp.relative_to(paths.truth_dir)
        try:
            ds = dp.stat()
        except OSError:
            missing += 1
            continue
        if ds.st_size > sp.stat().st_size:
            divergent += 1  # dest larger than source: divergent copy, not growth
    result["mirror_complete"] = missing == 0 and divergent == 0
    result["dest_missing_files"] = missing
    result["dest_divergent_files"] = divergent
    return {
        "truth_dir": str(paths.truth_dir),
        "dest": str(dest_path),
        "vectors_cached": vectors_cached,
        "verify_ok": verify_ok,
        **result,
    }


def verify(
    *,
    home: Optional[str] = None,
    deep: bool = False,
    hashes: bool = False,
    backup: Optional[str] = None,
) -> dict:
    """Integrity check: the JSONL truth parses cleanly and matches the SQLite index.

    Scans every per-thread truth file and compares to the projection's counts.
    The index is compared against ``events_effective`` — the truth's line count
    after collapsing superseded lines (re-appended ids, same-content twins), which
    is exactly what a reindex materializes; the raw line count and the superseded
    remainder are reported alongside. ``ok`` is True only when the effective counts
    align and nothing failed to parse. A negative event drift (truth > index) is
    the *safe* direction — ``archive reindex`` rebuilds the index from truth; a
    positive drift (index > truth) or any parse error is a real integrity problem.

    ``deep=True`` adds an id-level comparison (both directions, below a stable id
    watermark so in-flight ingest can't false-alarm), dedup_key parity for ids on
    both sides, knowledge-layer parity, and dangling-reference checks. Slower — it
    re-reads the whole truth directory (twice) and queries the index per thread —
    but it sees what count parity can't: missing content masked by compensating
    errors, and exactly which events drifted.

    ``hashes=True`` adds content-level self-validation on both stores: each
    event's ``dedup_key`` ends in a hash of its payload's semantic content, so
    re-hashing the stored payload and comparing detects silent payload corruption
    (bit rot, a bad write) with no extra state. The hash covers only the payload's
    *semantic content keys* (``_DEDUP_CONTENT_KEYS``) — corruption in other payload
    fields (model names, metadata) is invisible to it. Report-only — it never fails
    ``ok``: a payload-repair pass that rewrites content in place leaves a stale
    hash behind, so a stable nonzero baseline is expected; the signal is the
    count *jumping* between runs, so the previous run's counts are persisted in
    ``manifest.json`` and each run reports the delta. CPU-heavy (re-hashes every
    payload twice).

    ``backup`` scans a backup mirror of the truth directory with the same
    parse-and-count pass as the live truth (no watermark bound — the mirror is a
    point-in-time copy) and reports its counts against the live ones. The backup
    check fails ``ok`` only on parse errors in the mirror; a lower effective count
    is expected staleness (the mirror ages between runs) and is reported as
    ``coverage`` for trending.

    The SQLite file itself gets a ``PRAGMA quick_check`` — page-level index
    corruption is otherwise invisible until a query happens to touch a bad page.
    The index is rebuildable, so a failure here means ``archive reindex``, not
    data loss — but it must be *seen*.

    The shallow comparison is watermark-bounded on both sides too (ids at or
    below the index maxima captured up front), so verify can run against a live
    watcher without racing its ingest. An *empty* index gets no bound — a
    restored-but-not-yet-reindexed archive must show its full drift, not a
    vacuous OK.
    """
    open_archive(home)
    from sqlalchemy import func, select

    from .store import Event, Thread, get_session
    from .truth import scan_truth_counts

    with get_session() as s:
        watermark = s.execute(select(func.max(Event.id))).scalar() or 0
        thread_watermark = s.execute(select(func.max(Thread.id))).scalar() or 0
    truth = scan_truth_counts(
        event_id_max=watermark or None, thread_id_max=thread_watermark or None,
    )
    with get_session() as s:
        thread_q = select(func.count()).select_from(Thread)
        if thread_watermark:
            thread_q = thread_q.where(Thread.id <= thread_watermark)
        idx_threads = s.execute(thread_q).scalar() or 0
        event_q = select(func.count()).select_from(Event)
        if watermark:
            event_q = event_q.where(Event.id <= watermark)
        idx_events = s.execute(event_q).scalar() or 0
    drift_threads = int(idx_threads) - truth["threads"]
    drift_events = int(idx_events) - truth["events_effective"]
    # Page-level self-check of the index file. quick_check reads every page but
    # skips index-content verification — the cheap form; a torn page still shows.
    with get_session() as s:
        qc_rows = s.connection().connection.execute("PRAGMA quick_check(10)").fetchall()
    quick_check = "ok" if [r[0] for r in qc_rows] == ["ok"] else "; ".join(
        str(r[0]) for r in qc_rows
    )
    result = {
        "ok": (
            drift_threads == 0 and drift_events == 0 and truth["parse_errors"] == 0
            and quick_check == "ok"
        ),
        "truth": truth,
        "index": {
            "threads": int(idx_threads),
            "events": int(idx_events),
            "quick_check": quick_check,
        },
        "drift": {"threads": drift_threads, "events": drift_events},
    }
    if deep:
        result["deep"] = _verify_deep(watermark)
        result["ok"] = result["ok"] and result["deep"]["ok"]
    if hashes:
        result["hashes"] = _verify_hashes(watermark)
    if backup is not None:
        result["backup"] = _verify_backup(Path(backup).expanduser(), truth)
        result["ok"] = result["ok"] and result["backup"]["ok"]
    return result


def _verify_backup(dest: Path, live_truth: dict) -> dict:
    """Scan a backup mirror of the truth dir and compare it to the live scan.

    The restore-drill primitive: the same parse-and-count pass ``verify`` runs on
    the live truth, pointed at the mirror. Zero parse errors is the hard
    requirement (a mirror that doesn't parse doesn't restore); the effective-count
    ratio against the live truth (``coverage``) quantifies staleness — it should
    hover near 1.0 and only ever *rise* between backup runs. A coverage *drop*
    means the backup lost content relative to its last state — investigate before
    the next mirror run overwrites anything."""
    from .truth import scan_truth_counts

    if not (dest / "manifest.json").exists() and not (dest / "threads").exists():
        return {"dest": str(dest), "ok": False, "error": "not a truth mirror"}
    scan = scan_truth_counts(truth_dir=dest)
    live_effective = live_truth["events_effective"] or 1
    return {
        "dest": str(dest),
        "ok": scan["parse_errors"] == 0,
        "scan": scan,
        "coverage": round(scan["events_effective"] / live_effective, 6),
    }


def _verify_hashes(watermark: int) -> dict:
    """Content-level self-validation: re-hash each stored payload against the
    content hash embedded in its own ``dedup_key`` (its last ``:``-segment; see
    ``thread_import.event_builder.compute_dedup_key``), on both the truth files
    and the index. A mismatch means the payload changed since its key was
    computed — corruption, or an in-place payload repair that didn't recompute
    the key. Events with no dedup_key (or a key whose tail isn't a hash) are
    skipped and counted."""
    import json as _json
    import re as _re

    from thread_import.event_builder import compute_content_hash

    from .store import get_session
    from .truth.jsonl_log import THREADS_SUBDIR, _iter_jsonl, log_dir

    hex16 = _re.compile(r"^[0-9a-f]{16}$")

    def _check(payload: dict, dedup_key: str) -> Optional[bool]:
        """True = hash matches, False = mismatch, None = key has no hash tail."""
        tail = dedup_key.rsplit(":", 1)[-1]
        if not hex16.match(tail):
            return None
        return compute_content_hash(payload) == tail

    truth_checked = truth_mismatched = truth_skipped = 0
    truth_sample: list[int] = []
    threads_dir = log_dir() / THREADS_SUBDIR
    if threads_dir.exists():
        for path in threads_dir.rglob("*.jsonl"):
            for rec in _iter_jsonl(path):
                if rec.get("type", "event") != "event":
                    continue
                ev_id, key = rec.get("id"), rec.get("dedup_key")
                if ev_id is None or ev_id > watermark or not key:
                    continue
                payload = rec.get("payload")
                verdict = _check(payload, key) if isinstance(payload, dict) else False
                if verdict is None:
                    truth_skipped += 1
                    continue
                truth_checked += 1
                if not verdict:
                    truth_mismatched += 1
                    if len(truth_sample) < 10:
                        truth_sample.append(int(ev_id))

    index_checked = index_mismatched = index_skipped = 0
    index_sample: list[int] = []
    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3 — stream, don't materialize
        cur = conn.execute(
            "SELECT id, dedup_key, payload FROM events "
            "WHERE dedup_key IS NOT NULL AND id <= ?", (watermark,)
        )
        for ev_id, key, payload_text in cur:
            try:
                payload = _json.loads(payload_text)
            except (TypeError, ValueError):
                payload = None
            verdict = _check(payload, key) if isinstance(payload, dict) else False
            if verdict is None:
                index_skipped += 1
                continue
            index_checked += 1
            if not verdict:
                index_mismatched += 1
                if len(index_sample) < 10:
                    index_sample.append(int(ev_id))

    result = {
        "truth": {
            "checked": truth_checked, "mismatched": truth_mismatched,
            "unhashed_keys": truth_skipped, "mismatch_sample": truth_sample,
        },
        "index": {
            "checked": index_checked, "mismatched": index_mismatched,
            "unhashed_keys": index_skipped, "mismatch_sample": index_sample,
        },
    }
    # The signal is the mismatch count *jumping* between runs, so persist this
    # run's counts in the manifest and surface the previous run's for comparison.
    from datetime import datetime, timezone

    from .truth.jsonl_log import _read_manifest, _write_manifest

    m = _read_manifest(log_dir())
    previous = m.get("hashes_baseline")
    if previous is not None:
        result["previous"] = previous
        result["delta"] = {
            "truth_mismatched": truth_mismatched - int(previous.get("truth_mismatched", 0)),
            "index_mismatched": index_mismatched - int(previous.get("index_mismatched", 0)),
        }
    m["hashes_baseline"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "truth_mismatched": truth_mismatched,
        "index_mismatched": index_mismatched,
    }
    _write_manifest(log_dir(), m)
    return result


def _verify_deep(watermark: int) -> dict:
    """Id-level truth↔index comparison plus knowledge-layer checks.

    Only events with ``id <= watermark`` (committed before the scan began) are
    compared: the truth line for any such event was written *before* its commit
    (the staging invariant), so at any later read it must be present — and any
    index row at or below the watermark must have a truth line. Everything above
    the watermark is in-flight ingest and skipped.

    Truth-only ids are split into two classes: **superseded** (a same-content twin
    — equal ``dedup_key`` — exists in the index under another id; the benign
    residue of a lost-commit re-import, collapsed on reindex) and **missing**
    (no twin: content the index genuinely lacks — recoverable via reindex).
    Index-only ids are the forbidden direction and always fail.

    For ids present on both sides, the stored ``dedup_key`` values are compared
    (``events_key_mismatch``): a mismatch means the two stores disagree on an
    event's content identity — an index-only mutation the truth never received
    (a reindex would rewrite it) or corruption on one side. Reported with a
    sample, and it fails ``ok``.

    The search surface is checked too (both FTS tables are written in the same
    transaction as their events): orphan shadow rows and a shadow↔FTS5 row-count
    mismatch fail; indexable events with no shadow row are reported only, since an
    event with no extractable text legitimately has none.

    Thread *metadata* parity (title/description/summary between the winning truth
    record and the index row) is report-only: an in-flight metadata commit can
    legitimately race the scan, but a persistent mismatch means a missed re-stage —
    and the next reindex would revert the index to the stale truth record.
    """
    import json as _json

    from sqlalchemy import text as sa_text

    from .store import get_session
    from .truth.jsonl_log import (
        KG_EVENTS_FILE,
        THREADS_SUBDIR,
        _iter_jsonl,
        log_dir,
        thread_file_load_order,
    )

    d = log_dir()
    threads_dir = d / THREADS_SUBDIR

    # Pass 1 — truth ids per thread (≤ watermark), which files hold each thread,
    # and the winning (last, in reindex's load order — canonical-depth file
    # last) thread record per id.
    truth_ids: dict[int, set[int]] = {}
    thread_files: dict[int, list] = {}
    truth_meta: dict[int, dict] = {}
    if threads_dir.exists():
        for path in thread_file_load_order(d):
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type", "event") == "thread":
                        if rec.get("id") is not None:
                            truth_meta[int(rec["id"])] = rec
                        continue
                    if rec.get("type", "event") != "event":
                        continue
                    ev_id, tid = rec.get("id"), rec.get("thread_id")
                    if ev_id is None or tid is None or ev_id > watermark:
                        continue
                    truth_ids.setdefault(int(tid), set()).add(int(ev_id))
                    files = thread_files.setdefault(int(tid), [])
                    if path not in files:
                        files.append(path)

    # Pass 2 — per-thread diff against the index.
    index_only: list[int] = []
    missing: list[int] = []
    key_mismatch: list[int] = []
    superseded = 0
    with get_session() as s:
        idx_tids = {
            r[0] for r in s.execute(sa_text(
                "SELECT DISTINCT thread_id FROM events WHERE id <= :wm"), {"wm": watermark})
        }
        for tid in sorted(set(truth_ids) | idx_tids):
            rows = s.execute(sa_text(
                "SELECT id, dedup_key FROM events WHERE thread_id = :t AND id <= :wm"),
                {"t": tid, "wm": watermark}).all()
            idx_map = {r[0]: r[1] for r in rows}
            t_set = truth_ids.get(tid, set())
            index_only.extend(sorted(set(idx_map) - t_set))
            if not t_set:
                continue
            # Re-read this thread's files for every truth id's dedup_key: the last
            # line for an id wins, matching what a reindex would materialize.
            truth_keys: dict[int, str | None] = {}
            for path in thread_files.get(tid, []):
                for rec in _iter_jsonl(path):
                    if rec.get("type", "event") == "event" and rec.get("id") in t_set:
                        truth_keys[rec["id"]] = rec.get("dedup_key")
            idx_keys = {v for v in idx_map.values() if v}
            for ev_id in sorted(t_set - set(idx_map)):
                if truth_keys.get(ev_id) and truth_keys[ev_id] in idx_keys:
                    superseded += 1
                else:
                    missing.append(ev_id)
            for ev_id in sorted(t_set & set(idx_map)):
                if truth_keys.get(ev_id) != idx_map[ev_id]:
                    key_mismatch.append(ev_id)

        # Thread-metadata parity: the winning truth record vs the index row, on the
        # stable text fields. Report-only — the truth line for a metadata update is
        # staged before its commit, so a commit landing between pass 1's file read
        # and this query legitimately shows index-newer-than-truth; a *persistent*
        # mismatch means a re-stage was missed, and the next reindex would silently
        # revert the index to the stale truth record.
        meta_mismatch: list[int] = []
        meta_rows = s.execute(sa_text(
            "SELECT id, title, description, summary FROM threads")).all()
        for tid, *idx_fields in meta_rows:
            rec = truth_meta.get(int(tid))
            if rec is None:
                continue  # count parity (shallow verify) owns missing records
            for field, idx_val in zip(("title", "description", "summary"), idx_fields):
                if (rec.get(field) or None) != (idx_val or None):
                    meta_mismatch.append(int(tid))
                    break

        # Knowledge layer: the kg truth log vs its table, by id.
        kg_line_ids = {
            rec.get("id") for rec in _iter_jsonl(d / KG_EVENTS_FILE)
            if rec.get("id") is not None
        }
        kg_row_ids = {r[0] for r in s.execute(sa_text("SELECT id FROM kg_events"))}
        kg_index_only = len(kg_row_ids - kg_line_ids)
        kg_truth_only = len(kg_line_ids - kg_row_ids)

        # Dangling references.
        dangling_links = s.execute(sa_text(
            "SELECT count(*) FROM thread_links l WHERE "
            "NOT EXISTS(SELECT 1 FROM threads t WHERE t.id = l.source_thread_id) "
            "OR NOT EXISTS(SELECT 1 FROM threads t WHERE t.id = l.target_thread_id)"
        )).scalar() or 0
        dangling_citations = s.execute(sa_text(
            "SELECT count(*) FROM topic_messages m "
            "WHERE m.archived_at IS NULL "  # tombstoned evidence is history, not a live ref
            "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = m.event_id)"
        )).scalar() or 0
        # A live citation whose recorded thread disagrees with the cited event's
        # actual thread: the column is derived from the event, so disagreement
        # means an unvalidated write or a stale snapshot seed. Reindex realigns
        # these (citation reconciliation), so a persistent count means rot.
        citation_thread_mismatch = s.execute(sa_text(
            "SELECT count(*) FROM topic_messages m JOIN events e ON e.id = m.event_id "
            "WHERE m.archived_at IS NULL AND m.thread_id != e.thread_id"
        )).scalar() or 0
        dangling_events = s.execute(sa_text(
            "SELECT count(*) FROM events e "
            "WHERE NOT EXISTS(SELECT 1 FROM threads t WHERE t.id = e.thread_id)"
        )).scalar() or 0
        dup_pairs = s.execute(sa_text(
            "SELECT count(*) FROM (SELECT 1 FROM events WHERE dedup_key IS NOT NULL "
            "GROUP BY thread_id, dedup_key HAVING count(*) > 1)"
        )).scalar() or 0

        # Search-surface parity. The FTS shadow (events_fts) and the FTS5 table
        # (event_search) are written in the same transaction as their events, so
        # below the watermark: no shadow row may point at a missing event (orphans),
        # and the two surfaces must hold the same row count. Coverage — indexable
        # events with no shadow row — is reported but not failed on: an event whose
        # payload yields no extractable text legitimately has no row, so a nonzero
        # count is a signal to investigate, not proof of drift.
        fts_orphans = fts_shadow_rows = fts5_rows = fts_uncovered = 0
        has_fts = s.execute(sa_text(
            "SELECT count(*) FROM sqlite_master WHERE name IN ('events_fts', 'event_search')"
        )).scalar() == 2
        if has_fts:
            fts_orphans = s.execute(sa_text(
                "SELECT count(*) FROM events_fts f WHERE f.event_id <= :wm "
                "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = f.event_id)"),
                {"wm": watermark}).scalar() or 0
            fts_shadow_rows = s.execute(sa_text(
                "SELECT count(*) FROM events_fts WHERE event_id <= :wm"),
                {"wm": watermark}).scalar() or 0
            fts5_rows = s.execute(sa_text(
                "SELECT count(*) FROM event_search WHERE event_id <= :wm"),
                {"wm": watermark}).scalar() or 0
            from .retrieval._extract import INDEXABLE_EVENT_TYPES

            types = ", ".join(f"'{t}'" for t in INDEXABLE_EVENT_TYPES)
            fts_uncovered = s.execute(sa_text(
                f"SELECT count(*) FROM events e WHERE e.id <= :wm "
                f"AND e.event_type IN ({types}) "
                "AND NOT EXISTS(SELECT 1 FROM events_fts f WHERE f.event_id = e.id)"),
                {"wm": watermark}).scalar() or 0

    ok = (
        not index_only and not missing and not key_mismatch
        and kg_index_only == 0
        and dangling_links == 0 and dangling_citations == 0 and dangling_events == 0
        and citation_thread_mismatch == 0
        and fts_orphans == 0 and fts_shadow_rows == fts5_rows
    )
    return {
        "ok": ok,
        "watermark": watermark,
        "events_index_only": len(index_only),
        "index_only_sample": index_only[:10],
        "events_missing_from_index": len(missing),
        "missing_sample": missing[:10],
        "events_key_mismatch": len(key_mismatch),
        "key_mismatch_sample": key_mismatch[:10],
        "events_superseded_twins": superseded,
        "thread_meta_mismatch": len(meta_mismatch),
        "thread_meta_sample": meta_mismatch[:10],
        "kg": {"index_only": kg_index_only, "truth_only": kg_truth_only},
        "dangling": {
            "link_endpoints": int(dangling_links),
            "citation_events": int(dangling_citations),
            "citation_thread_mismatch": int(citation_thread_mismatch),
            "event_threads": int(dangling_events),
        },
        "duplicate_content_pairs_index": int(dup_pairs),
        "fts": {
            "orphan_rows": int(fts_orphans),
            "shadow_rows": int(fts_shadow_rows),
            "fts5_rows": int(fts5_rows),
            "uncovered_indexable_events": int(fts_uncovered),
        },
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
