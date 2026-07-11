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

import json
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


# ── operational health records (<home>/health.json) ──────────────────────────
# When verify / backup last ran and how they went — the staleness signal that
# tells a dead scheduled job apart from a healthy one. Deliberately OUTSIDE the
# truth dir: this is install-local operational state, so backups don't mirror it
# (a restored truth must not claim the source install's health history) and the
# backup can't dirty the tree it is mirroring.
def _health_path() -> Path:
    return resolve_paths().home / "health.json"


def _read_health() -> dict:
    try:
        return json.loads(_health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_health(key: str, record: dict) -> None:
    """Set ``key`` to ``record`` (stamped with ``at``) — a locked read-modify-
    write (exclusive flock on ``health.json.lock``, same discipline as the
    manifest's): concurrent completions (a verify racing a backup, the nightly's
    stages) each own different keys, and an unlocked whole-file replace would
    lose whichever writer published first. Advisory data, so a failed write
    logs and never breaks the operation it describes."""
    import fcntl
    from datetime import datetime, timezone

    try:
        p = _health_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p.with_name(f"{p.name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            health = _read_health()
            health[key] = {"at": datetime.now(timezone.utc).isoformat(), **record}
            tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(health, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        finally:
            os.close(fd)  # closing the fd releases the flock
    except OSError:
        import logging

        logging.getLogger(__name__).exception("could not record %s in health.json", key)


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

# Destination-side generation snapshots: hardlink copies of the mirror's state,
# taken before each backup run overwrites it. See _snapshot_generation.
_GENERATIONS_SUBDIR = ".generations"
_GEN_KEEP_RECENT = 7
_GEN_KEEP_MONTHS = 6


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
        # The generations subtree is destination-only state (hardlink snapshots
        # of prior mirror runs) — never a deletion candidate, and never counted
        # toward the deletion cap's denominator.
        dest_files = [
            dp for dp in dest.rglob("*")
            if not dp.is_dir() and dp.relative_to(dest).parts[0] != _GENERATIONS_SUBDIR
        ]
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


def _snapshot_generation(dest: Path) -> dict:
    """Hardlink-snapshot the mirror's current state into
    ``<dest>/.generations/<UTC stamp>/`` — taken *before* the mirror run
    overwrites it, so each generation is the destination as the previous run
    left it. The rolling mirror alone propagates destruction: same-size
    corruption, a mistaken ``--allow-shrink``, or a bad repair overwrites the
    only other copy on the next nightly run. Generations make that recoverable.

    Hardlinks make a generation nearly free (the truth is append-mostly, and
    the mirror only ever publishes destination files via whole-file rename —
    ``_atomic_copy`` — so a snapshot's linked inodes are never mutated by later
    runs; deletions just unlink the mirror's name). One generation per run —
    every overwrite of the mirror has a pre-state snapshot, so a second (bad)
    backup on the same day can't destroy the day's only pre-state. The
    snapshot is built under a dot-tmp name and renamed into place (the gens
    dir fsynced after), so a killed run never leaves a directory that looks
    like a complete generation and a published one survives power loss.

    Retention coalesces by day, never within one: every generation from the
    newest ``_GEN_KEEP_RECENT`` distinct UTC days is kept (pruning a same-day
    sibling would discard exactly the pre-bad-run state generations exist
    for), plus the newest generation of each distinct month until
    ``_GEN_KEEP_MONTHS`` months are covered. Failure to snapshot degrades to
    the pre-generations behavior (reported, never blocks the mirror itself)."""
    import logging
    import shutil
    from datetime import datetime, timezone

    out: dict = {"generation_created": None, "generations_pruned": 0}
    if not (dest / "manifest.json").exists():
        return out  # first run: nothing at the destination to preserve
    gens = dest / _GENERATIONS_SUBDIR
    gens.mkdir(exist_ok=True)
    for stale in gens.glob(".tmp-*"):  # a killed snapshot's half-built tree
        shutil.rmtree(stale, ignore_errors=True)
    now = datetime.now(timezone.utc)
    names = sorted((p.name for p in gens.iterdir() if p.is_dir()), reverse=True)
    name = now.strftime("%Y%m%dT%H%M%SZ")
    while name in names:  # two runs within a second — still one gen per run
        name += "x"
    tmp = gens / f".tmp-{name}"
    try:
        linked = 0
        for sp in dest.rglob("*"):
            if sp.is_dir():
                continue
            rel = sp.relative_to(dest)
            if rel.parts[0] == _GENERATIONS_SUBDIR:
                continue
            gp = tmp / rel
            gp.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(sp, gp)
            except OSError:  # filesystem without hardlinks — take the copy cost
                shutil.copy2(sp, gp)
            linked += 1
        os.rename(tmp, gens / name)
        from .truth.jsonl_log import _fsync_dir

        _fsync_dir(gens)  # the publishing rename itself must survive power loss
        out["generation_created"] = name
        out["generation_files"] = linked
        names.insert(0, name)
        names.sort(reverse=True)
    except OSError as e:  # snapshot is protection, not the backup itself
        shutil.rmtree(tmp, ignore_errors=True)
        out["generation_error"] = str(e)
        logging.getLogger(__name__).exception("backup: generation snapshot failed")
    # Coalesce by day, never within one (see docstring).
    days: list[str] = []
    for n in names:  # newest first
        if n[:8] not in days:
            days.append(n[:8])
    keep = {n for n in names if n[:8] in set(days[:_GEN_KEEP_RECENT])}
    months = {n[:6] for n in keep}
    for n in names:
        if n not in keep and n[:6] not in months and len(months) < _GEN_KEEP_MONTHS:
            keep.add(n)
            months.add(n[:6])
    for n in names:
        if n not in keep:
            shutil.rmtree(gens / n, ignore_errors=True)
            out["generations_pruned"] += 1
    out["generations_kept"] = len(keep)
    return out


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

    Before the mirror touches anything, the destination's current state is
    preserved as a hardlink generation under ``<dest>/.generations/``
    (:func:`_snapshot_generation`) — the recovery margin for destruction the
    in-run guards can't see. ``archive restore-drill`` proves the mirror (or a
    generation) actually restores.
    """
    open_archive(home)
    from .truth import checkpoint as _checkpoint
    from .truth.jsonl_log import _try_rebalance_lock

    _checkpoint()  # full: overlays + metadata-update backstop → truth is a complete restore set
    # Verify AFTER the checkpoint, so verify_ok describes the tree the mirror
    # actually copies — a verdict on the pre-checkpoint state could bless (or
    # smear) a different truth than the one being backed up.
    verify_ok = True
    if verify_first:
        verify_ok = bool(verify(home=home)["ok"])

    paths = resolve_paths(home)
    # Refresh the durable vector cache so live-embedded vectors (the watcher's
    # cohost writes them into index.db only) survive an index loss and ride the
    # mirror. No-op when the store holds no vectors — an empty save never
    # replaces a populated sidecar.
    from .retrieval.vectors import save_vectors_sidecar

    vectors_cached = save_vectors_sidecar(paths.truth_dir)
    dest_path = Path(dest).expanduser()
    dest_path.mkdir(parents=True, exist_ok=True)
    # A destination on the same filesystem as the truth dir protects against a
    # bad write, not against the disk: one device failure (or a stolen machine)
    # takes source, mirror, and every hardlink generation together. Report-only
    # — a same-disk mirror still beats none — but the flag rides the result and
    # the health record so `archive status` and anything watching health.json
    # can keep saying so until a real second copy exists.
    try:
        same_device = os.stat(dest_path).st_dev == os.stat(paths.truth_dir).st_dev
    except OSError:
        same_device = None
    # Delete-sync only when the source looks like a real, checkpointed truth dir —
    # a mirror of an empty/foreign source must never strip a good backup — and only
    # when it verified clean (a sick source mirrors additively; see docstring).
    delete = (paths.truth_dir / "manifest.json").exists() and verify_ok
    # Preserve the destination's pre-run state as a hardlink generation BEFORE
    # the mirror overwrites it — the recovery margin for anything the guards
    # can't see (same-size corruption, a mistaken --allow-shrink, a bad repair).
    generations = _snapshot_generation(dest_path)
    with _try_rebalance_lock() as held:
        # If a rebalance sweep is mid-flight, mirror additively (no deletions):
        # copies of both layouts are safe; stale-path deletion waits for the next run.
        result = _mirror_dir(
            paths.truth_dir, dest_path, delete=delete and held, allow_shrink=allow_shrink
        )
    result.update(generations)

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

    # Record the outcome in the home's health file (surfaced by `archive status`):
    # a backup agent that quietly stops running is indistinguishable from a
    # healthy one by its log files alone — the record's age is the signal.
    _record_health("backup_last", {
        "dest": str(dest_path),
        "ok": bool(verify_ok and result["mirror_complete"] and not result["deletions_skipped"]),
        "verify_ok": verify_ok,
        "mirror_complete": result["mirror_complete"],
        "files_copied": result["files_copied"],
        "same_device": same_device,
    })
    return {
        "truth_dir": str(paths.truth_dir),
        "dest": str(dest_path),
        "vectors_cached": vectors_cached,
        "verify_ok": verify_ok,
        "same_device": same_device,
        **result,
    }


def restore_drill(
    dest: str, *, home: Optional[str] = None, keep_home: bool = False
) -> dict:
    """Prove the backup actually restores: rebuild a full index from the mirror
    in a throwaway home and check what materialized against the mirror's own scan.

    ``verify --backup`` parses and counts the mirror; this is the missing last
    step — an end-to-end rehearsal of the recovery path (copy the truth dir,
    ``reindex`` from it: schema, per-thread load, kg replay, FTS, the vector
    sidecar restore, quick_check). ``ok`` means the mirror parsed clean, the
    rebuilt index materialized exactly the mirror's effective counts, the
    restored archive covers the live one (``coverage`` = rebuilt events / live
    events ≥ 0.98 — the mirror is minutes old when the scheduled drill runs, so
    materially lower means the backup restores to less than the archive it is
    supposed to protect), and a smoke pass (:func:`_drill_smoke`) proved the
    rebuilt archive actually *reads and searches*, not just materializes.
    Heavy (a full index build) — sized for the nightly 04:00 window, where
    ``archive nightly`` runs it after every backup.

    The drill home is a temp directory (``keep_home=True`` keeps it for
    inspection, e.g. to point a reader at the restored index); the live archive
    is reopened before returning, and the outcome lands in ``health.json``
    (``restore_drill_last``) so a drill that stops running looks stale."""
    import shutil
    import tempfile
    import time

    paths = open_archive(home)
    from .truth import scan_truth_counts

    dest_path = Path(dest).expanduser()
    if not (dest_path / "threads").exists():
        return {"dest": str(dest_path), "ok": False, "error": "not a truth mirror"}
    live_home = str(paths.home)
    started = time.monotonic()
    from sqlalchemy import func, select

    from .store import Event, get_session

    with get_session() as s:
        live_events = s.execute(select(func.count()).select_from(Event)).scalar() or 0
    scan = scan_truth_counts(truth_dir=dest_path)
    result: dict = {"dest": str(dest_path), "mirror": scan, "live_events": int(live_events)}
    drill_home = Path(tempfile.mkdtemp(prefix="thread-archive-restore-drill-"))
    try:
        # The generations subtree and any half-published mirror temp files are
        # destination bookkeeping, not truth — the drill restores the mirror.
        shutil.copytree(
            dest_path, drill_home / "truth",
            ignore=shutil.ignore_patterns(_GENERATIONS_SUBDIR, ".*.tmp-*"),
        )
        open_archive(str(drill_home))
        from .truth import reindex as _reindex

        try:
            counts = _reindex()
        except RuntimeError as e:  # a refused/failed rebuild IS the drill's finding
            result.update({"ok": False, "error": str(e)})
            counts = None
        if counts is not None:
            result["rebuilt"] = counts
            result["coverage"] = round(counts["events"] / (live_events or 1), 6)
            result["smoke"] = _drill_smoke(
                str(drill_home), expect_content=counts["events"] > 0
            )
            result["ok"] = (
                scan["parse_errors"] == 0
                and counts["events"] == scan["events_effective"]
                and counts["threads"] == scan["threads"]
                and result["coverage"] >= 0.98
                and result["smoke"]["ok"]
            )
    finally:
        close()
        if keep_home:
            result["drill_home"] = str(drill_home)
        else:
            shutil.rmtree(drill_home, ignore_errors=True)
        open_archive(live_home)
    result["seconds"] = round(time.monotonic() - started, 1)
    _record_health("restore_drill_last", {
        "dest": str(dest_path),
        "ok": bool(result.get("ok")),
        "events": scan["events_effective"],
        "coverage": result.get("coverage"),
        "seconds": result["seconds"],
    })
    return result


# Age gates for the escalated verify tiers `nightly` folds in on top of its
# nightly backup + shallow verify + restore drill.
_DEEP_EVERY_DAYS = 7
_HASHES_EVERY_DAYS = 30


def _health_is_due(key: str, every_days: float) -> bool:
    """True when health record ``key`` is missing, unparseable, stale, or was
    not ok — the age gate that replaces weekday/day-of-month schedule math: a
    missed (machine off) or failed escalated pass makes the *next* nightly run
    pick it up, instead of waiting for the calendar to come around again."""
    from datetime import datetime, timezone

    rec = _read_health().get(key)
    try:
        at = datetime.fromisoformat(rec["at"])
    except (TypeError, KeyError, ValueError):
        return True
    if not rec.get("ok"):
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds() >= every_days * 86400


def _notify(url: str, message: str) -> Optional[str]:
    """POST a notification (lab's ``/api/notify`` shape: ``{title, message}``).
    Fail-soft — returns an error string instead of raising: health.json and the
    job log are the durable record; the push is best-effort."""
    import urllib.request

    body = json.dumps({"title": "thread-archive", "message": message}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def nightly(
    dest: str,
    *,
    home: Optional[str] = None,
    notify_url: Optional[str] = None,
    allow_shrink: bool = False,
    drill: bool = True,
) -> dict:
    """The scheduled protection pipeline, as one command: ``backup`` → ``verify``
    (with age-gated escalation) → ``restore_drill`` — every night.

    Replaces a shell chain of the three commands. The differences that matter:

    - **Every stage runs** (no ``&&`` short-circuit): a failed backup must not
      also cost the night's integrity check and drill — each stage's outcome is
      recorded separately (its own health.json record plus ``nightly_last``),
      so an alert can say *which* stage broke while the others' green stays
      visible.
    - **Escalation is age-gated, not calendar-gated**: the deep verify (+ the
      mirror parse-scan) folds in when ``verify_deep_last`` is missing, older
      than ``_DEEP_EVERY_DAYS``, or failed; ``--hashes`` likewise on
      ``_HASHES_EVERY_DAYS``. A machine that was off on the scheduled day runs
      the escalated pass on its next nightly instead of a month later.
    - **The drill is nightly.** The restore path is code and the code changes
      daily; a restore-path regression must surface the next morning, not up
      to a month later. ~10 min of nice'd 4 a.m. work at current size.
    - **Failure notifies** (``notify_url``, lab's ``/api/notify`` shape) with
      the failed stage names. The "never ran at all" case is the monitor's to
      catch, from the staleness of the health.json records this writes.

    Returns per-stage results plus ``ok`` / ``failed_stages``.
    """
    open_archive(home)
    failed: list[str] = []
    result: dict = {"dest": str(Path(dest).expanduser())}

    try:
        b = backup(dest, home=home, allow_shrink=allow_shrink)
        backup_ok = bool(
            b["verify_ok"] and b["mirror_complete"] and not b["deletions_skipped"]
        )
    except Exception as e:
        b, backup_ok = {"error": f"{type(e).__name__}: {e}"}, False
    result["backup"] = b
    if not backup_ok:
        failed.append("backup")

    deep_due = _health_is_due("verify_deep_last", _DEEP_EVERY_DAYS)
    hashes_due = _health_is_due("verify_hashes_last", _HASHES_EVERY_DAYS)
    result["escalations"] = {"deep": deep_due, "hashes": hashes_due}
    try:
        v = verify(
            home=home, deep=deep_due, hashes=hashes_due,
            backup=str(dest) if deep_due else None,
        )
        verify_ok = bool(v["ok"])
    except Exception as e:
        v, verify_ok = {"error": f"{type(e).__name__}: {e}"}, False
    result["verify"] = v
    if not verify_ok:
        failed.append("verify")

    if drill:
        try:
            d = restore_drill(dest, home=home)
            drill_ok = bool(d.get("ok"))
        except Exception as e:
            d, drill_ok = {"error": f"{type(e).__name__}: {e}"}, False
        result["drill"] = d
        if not drill_ok:
            failed.append("restore-drill")

    result["ok"] = not failed
    result["failed_stages"] = failed
    _record_health("nightly_last", {
        "dest": result["dest"],
        "ok": result["ok"],
        "failed_stages": failed,
        "deep": deep_due,
        "hashes": hashes_due,
        "drill": drill,
    })
    # Family-monitor heartbeat: thread-monitor freshness-checks periodic jobs
    # via ~/.thread/logs (the shared heartbeat ground — it never reads sibling
    # products' private stores, so health.json alone is invisible to it).
    # Stamped on every completion whatever the outcome: mtime staleness means
    # "the job stopped running," the content carries how the last run went.
    # Fail-soft, and skipped entirely when the dir doesn't exist (an install
    # outside the thread family has no monitor to feed). The env override
    # exists so tests never stamp the real box's heartbeat.
    hb_dir = Path(
        os.environ.get("THREAD_ARCHIVE_HEARTBEAT_DIR")
        or Path.home() / ".thread" / "logs"
    )
    if hb_dir.is_dir():
        try:
            from datetime import datetime, timezone

            (hb_dir / "archive-nightly.heartbeat").write_text(
                json.dumps({
                    "at": datetime.now(timezone.utc).isoformat(),
                    "ok": result["ok"],
                    "failed_stages": failed,
                    "dest": result["dest"],
                }) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    if failed and notify_url:
        result["notify_error"] = _notify(
            notify_url,
            f"nightly backup pipeline FAILED at: {', '.join(failed)} — "
            "see `archive status`, health.json, and ~/.thread/archive/logs/backup-*.log",
        )
    return result


def _drill_smoke(home: str, *, expect_content: bool) -> dict:
    """Exercise the restored archive the way a reader would — the last gap
    between "the index materialized" and "the archive is usable." Reads the
    newest indexed thread and searches for a token drawn from the FTS shadow's
    own stored content (the exact corpus ``event_search`` matches against, so a
    hit is guaranteed when the search surface works). A rebuilt index whose
    read path or search surface is broken must fail the drill here, not on the
    first real restore. Runs against the drill home while it is still open."""
    import re as _re

    from sqlalchemy import text as _sa_text

    from .store import get_session as _get_session

    out: dict = {"ok": not expect_content, "read_ok": False, "search_ok": False}
    if not expect_content:
        return out
    try:
        with _get_session() as s:
            rows = s.execute(_sa_text(
                "SELECT thread_id, content FROM events_fts "
                "WHERE content != '' ORDER BY event_id DESC LIMIT 50"
            )).fetchall()
        tid = token = None
        for thread_id, content in rows:
            token = next(iter(_re.findall(r"[A-Za-z]{4,}", content or "")), None)
            if token:
                tid = thread_id
                break
        if tid is None:
            # No sampleable token (empty FTS surface would already fail the
            # count checks; all-non-Latin content just isn't sampleable here).
            out["ok"] = True
            out["skipped"] = "no sampleable token in the newest FTS rows"
            return out
        text = read_thread(tid, home=home, mode="chat", limit=20)
        out["read_ok"] = bool(text and text.strip())
        out["token"] = token
        out["search_ok"] = bool(search(token, home=home, limit=5, rerank=False))
        out["ok"] = out["read_ok"] and out["search_ok"]
    except Exception as e:  # a crash in the read/search path IS the finding
        out["error"] = f"{type(e).__name__}: {e}"
        out["ok"] = False
    return out


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
    align, nothing failed to parse, the search surface is in parity (shadow ↔
    FTS5 row counts match with no orphan rows — silently unsearchable content is
    loss in effect, so it's checked on this daily cadence too), and the live
    index carries the full declared schema (:func:`_verify_schema` — ``create_all``
    never retrofits columns/indexes/constraints onto existing tables, so an
    under-enforced index must be seen and reindexed). A negative event
    drift (truth > index) is the *safe* direction — ``archive reindex`` rebuilds
    the index from truth; a positive drift (index > truth) or any parse error is a
    real integrity problem. Parse errors split into ``parse_errors_torn_tail``
    (the residue of a crash mid-append) and ``parse_errors_interior``; either kind
    is cleared by ``archive repair``, which quarantines the damaged lines and
    restores any committed content they shadowed from the index.

    Every run records its outcome (``verify_last``: timestamp, ok, drift) in
    ``<home>/health.json``, surfaced by ``archive status`` — an integrity check
    that silently stops running must look stale, not healthy.

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
    fields (model names, metadata) is invisible to it. A *new* mismatch fails
    ``ok``: each run's counts are persisted in ``manifest.json`` and diffed
    against the previous run's baseline — an increase (or any mismatch on a
    baseline-less first run) means content changed underneath its key since the
    last look, and health must go red until it's seen. The stamped baseline
    absorbs the count, so an acknowledged (e.g. legitimately-repaired-in-place)
    mismatch fails exactly one run rather than pinning verify red forever.
    CPU-heavy (re-hashes every payload twice).

    ``backup`` scans a backup mirror of the truth directory with the same
    parse-and-count pass as the live truth (no watermark bound — the mirror is a
    point-in-time copy) and reports its counts against the live ones. The backup
    check fails ``ok`` only on parse errors in the mirror; a lower effective count
    is expected staleness (the mirror ages between runs) and is reported as
    ``coverage`` for trending. Combined with ``hashes``, the mirror gets the
    content-level hash scan too — an unchanged destination file is never
    re-copied, so rot at rest is otherwise invisible forever.
    ``restore_drill`` is the step beyond this: actually rebuild an index from
    the mirror.

    The SQLite file itself gets a ``PRAGMA quick_check`` — page-level index
    corruption is otherwise invisible until a query happens to touch a bad page.
    On the ``hashes`` cadence this upgrades to the full ``integrity_check``,
    which also verifies b-tree index content against the tables (the only check
    that catches a corrupted index silently returning wrong query results).
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
    # Self-check of the index file. The daily form is quick_check (reads every
    # page but skips index-content verification — a torn page still shows); the
    # ``hashes`` cadence upgrades to the full integrity_check, which also
    # verifies b-tree index content against the tables — the only check that
    # catches a corrupted index silently returning wrong query results.
    check_pragma = "integrity_check(10)" if hashes else "quick_check(10)"
    with get_session() as s:
        qc_rows = s.connection().connection.execute(f"PRAGMA {check_pragma}").fetchall()
    quick_check = "ok" if [r[0] for r in qc_rows] == ["ok"] else "; ".join(
        str(r[0]) for r in qc_rows
    )
    # Search-surface parity, on the daily cadence (deep re-checks it with more
    # detail): the FTS shadow and the FTS5 table commit in the same transaction
    # as their events, so below the watermark the two row counts must match and
    # no shadow row may point at a missing event. Both are index-internal drift
    # — a reindex rebuilds them — but silently unsearchable content is loss in
    # effect, so it must be *seen* daily, not only on the deep cadence.
    fts_shadow = fts5 = fts_orphans = 0
    with get_session() as s:
        conn = s.connection().connection
        has_fts = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name IN ('events_fts', 'event_search')"
        ).fetchone()[0] == 2
        if has_fts and watermark:
            fts_shadow = conn.execute(
                "SELECT count(*) FROM events_fts WHERE event_id <= ?", (watermark,)
            ).fetchone()[0]
            fts5 = conn.execute(
                "SELECT count(*) FROM event_search WHERE event_id <= ?", (watermark,)
            ).fetchone()[0]
            fts_orphans = conn.execute(
                "SELECT count(*) FROM events_fts f WHERE f.event_id <= ? "
                "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = f.event_id)",
                (watermark,),
            ).fetchone()[0]
    # Declared-schema parity (cheap PRAGMA introspection): a live index that
    # predates a model change runs under-enforced until a reindex — that gap
    # must be seen on the daily cadence, not discovered from its consequences.
    schema = _verify_schema()
    result = {
        "ok": (
            drift_threads == 0 and drift_events == 0 and truth["parse_errors"] == 0
            and quick_check == "ok"
            and fts_shadow == fts5 and fts_orphans == 0
            and schema["ok"]
        ),
        "schema": schema,
        "truth": truth,
        "index": {
            "threads": int(idx_threads),
            "events": int(idx_events),
            "quick_check": quick_check,
            "check": "integrity_check" if hashes else "quick_check",
        },
        "drift": {"threads": drift_threads, "events": drift_events},
        "fts": {
            "shadow_rows": int(fts_shadow),
            "fts5_rows": int(fts5),
            "orphan_rows": int(fts_orphans),
        },
    }
    if deep:
        result["deep"] = _verify_deep(watermark)
        result["ok"] = result["ok"] and result["deep"]["ok"]
    if hashes:
        result["hashes"] = _verify_hashes(watermark)
        # Detected corruption must fail health, not just be reported: any *new*
        # mismatch since the previous baseline (or any mismatch at all on a
        # first, baseline-less run) fails ``ok``. The baseline this run stamps
        # absorbs the count, so the failure fires once and the delta signal
        # stays meaningful — a legitimately-repaired payload's stale hash
        # doesn't keep verify red forever, but it is *seen* red once.
        h, delta = result["hashes"], result["hashes"].get("delta")
        if delta is not None:
            new_mismatches = delta["truth_mismatched"] > 0 or delta["index_mismatched"] > 0
        else:
            new_mismatches = bool(h["truth"]["mismatched"] or h["index"]["mismatched"])
        result["hashes"]["new_mismatches"] = new_mismatches
        result["ok"] = result["ok"] and not new_mismatches
    if backup is not None:
        result["backup"] = _verify_backup(Path(backup).expanduser(), truth)
        result["ok"] = result["ok"] and result["backup"]["ok"]
        if hashes and "scan" in result["backup"]:
            # Content-level rot detection on the mirror too: an unchanged
            # destination file is never re-copied (size+mtime skip), so silent
            # corruption at rest would otherwise persist forever while the
            # parse-and-count scan stays green. No watermark — the mirror is a
            # point-in-time copy. Report-only, same as the live hashes pass.
            result["backup"]["hashes"] = _hash_scan_truth_dir(
                Path(backup).expanduser(), watermark=None
            )

    # Record the outcome in the home's health file so `archive status` (and
    # anything watching it) can see when integrity was last checked and how it
    # went — a verify that silently stops running is indistinguishable from a
    # healthy one otherwise. Staleness of the timestamp is the primary signal: a
    # crash mid-verify leaves the previous record standing, and its age says so.
    _record_health("verify_last", {
        "ok": bool(result["ok"]),
        "deep": bool(deep),
        "hashes": bool(hashes),
        "drift_events": drift_events,
        "drift_threads": drift_threads,
        "parse_errors": truth["parse_errors"],
    })
    # The escalated tiers get their own records: ``verify_last`` is overwritten
    # by every shallow run, so these are what age-gated schedulers (``nightly``)
    # read to know when a deep / hashes pass last actually happened.
    if deep:
        _record_health("verify_deep_last", {"ok": bool(result["ok"])})
    if hashes:
        _record_health("verify_hashes_last", {"ok": bool(result["ok"])})
    return result


def _verify_schema() -> dict:
    """Live index schema vs the declared models — the under-enforcement check.

    Startup schema provisioning is ``create_all`` only: it creates *missing
    tables* but never retrofits new columns, indexes, or constraints onto
    existing ones, so a live index created before a model change can silently
    run under-enforced (e.g. the ``(thread_id, dedup_key)`` unique index absent
    → DB-level dedup off) until the next reindex. Introspects the live SQLite
    schema against ``Base.metadata`` directly — no stored version stamp to
    drift — and reports missing columns, named indexes, and unique constraints
    (matched by column set; SQLite realizes them as auto-named unique indexes).
    Extra live-side objects are ignored: an older/foreign index must still
    open. Anything missing fails ``verify``'s ``ok`` — the fix is ``archive
    reindex``, which builds a fresh index with the full declared schema."""
    from sqlalchemy import UniqueConstraint as _UC

    from .store import Base, get_session

    missing_tables: list[str] = []
    missing_columns: list[str] = []
    missing_indexes: list[str] = []
    missing_uniques: list[str] = []
    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3
        live_tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in Base.metadata.tables.values():
            if table.name not in live_tables:
                missing_tables.append(table.name)
                continue
            live_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table.name})")}
            missing_columns += [
                f"{table.name}.{c.name}" for c in table.columns if c.name not in live_cols
            ]
            index_list = conn.execute(f"PRAGMA index_list({table.name})").fetchall()
            live_index_names = {r[1] for r in index_list}
            missing_indexes += [
                str(idx.name) for idx in table.indexes if idx.name not in live_index_names
            ]
            live_unique_colsets = {
                frozenset(
                    r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})") if r[2]
                )
                for row in index_list
                if row[2]  # unique flag
            }
            for uc in table.constraints:
                if not isinstance(uc, _UC):
                    continue
                cols = frozenset(c.name for c in uc.columns)
                if cols not in live_unique_colsets:
                    missing_uniques.append(f"{table.name}({', '.join(sorted(cols))})")
    return {
        "ok": not (missing_tables or missing_columns or missing_indexes or missing_uniques),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "missing_unique_constraints": missing_uniques,
    }


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


def _hash_key_check(payload: object, dedup_key: str) -> Optional[bool]:
    """True = the payload re-hashes to the content hash embedded in its own
    ``dedup_key`` (the last ``:``-segment; see
    ``thread_import.event_builder.compute_dedup_key``); False = mismatch;
    None = the key carries no hash tail (nothing to validate against)."""
    import re as _re

    from thread_import.event_builder import compute_content_hash

    if not _re.match(r"^[0-9a-f]{16}$", dedup_key.rsplit(":", 1)[-1]):
        return None
    if not isinstance(payload, dict):
        return False
    return compute_content_hash(payload) == dedup_key.rsplit(":", 1)[-1]


def _hash_scan_truth_dir(truth_dir: Path, watermark: Optional[int]) -> dict:
    """Re-hash every event payload in a truth directory against its dedup_key's
    embedded content hash. The scan half of ``verify --hashes``, reusable
    against a backup mirror (``watermark=None`` — a point-in-time copy has no
    in-flight ingest to bound out). ``no_key`` counts events with no dedup_key
    at all: they carry nothing to validate against, so they are invisible to
    this check — the count keeps that coverage boundary visible."""
    from .truth.jsonl_log import THREADS_SUBDIR, _iter_jsonl

    checked = mismatched = skipped = no_key = 0
    sample: list[int] = []
    threads_dir = truth_dir / THREADS_SUBDIR
    if threads_dir.exists():
        for path in threads_dir.rglob("*.jsonl"):
            for rec in _iter_jsonl(path):
                if rec.get("type", "event") != "event":
                    continue
                ev_id, key = rec.get("id"), rec.get("dedup_key")
                if ev_id is None or (watermark is not None and ev_id > watermark):
                    continue
                if not key:
                    no_key += 1
                    continue
                verdict = _hash_key_check(rec.get("payload"), key)
                if verdict is None:
                    skipped += 1
                    continue
                checked += 1
                if not verdict:
                    mismatched += 1
                    if len(sample) < 10:
                        sample.append(int(ev_id))
    return {
        "checked": checked, "mismatched": mismatched,
        "unhashed_keys": skipped, "no_key": no_key, "mismatch_sample": sample,
    }


def _verify_hashes(watermark: int) -> dict:
    """Content-level self-validation: re-hash each stored payload against the
    content hash embedded in its own ``dedup_key`` (its last ``:``-segment; see
    ``thread_import.event_builder.compute_dedup_key``), on both the truth files
    and the index. A mismatch means the payload changed since its key was
    computed — corruption, or an in-place payload repair that didn't recompute
    the key. Events with a key whose tail isn't a hash are skipped and counted
    (``unhashed_keys``); events with no dedup_key at all are counted too
    (``no_key``) — they have zero content self-validation, and the count keeps
    that boundary visible.

    The caller (``verify``) fails ``ok`` when the mismatch count *increased*
    since the previous baseline (see ``new_mismatches``); the counts themselves
    are informational."""
    import json as _json

    from .store import get_session
    from .truth.jsonl_log import log_dir

    _check = _hash_key_check

    truth_scan = _hash_scan_truth_dir(log_dir(), watermark)

    index_checked = index_mismatched = index_skipped = 0
    index_sample: list[int] = []
    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3 — stream, don't materialize
        index_no_key = conn.execute(
            "SELECT count(*) FROM events WHERE dedup_key IS NULL AND id <= ?",
            (watermark,),
        ).fetchone()[0]
        cur = conn.execute(
            "SELECT id, dedup_key, payload FROM events "
            "WHERE dedup_key IS NOT NULL AND id <= ?", (watermark,)
        )
        for ev_id, key, payload_text in cur:
            try:
                payload = _json.loads(payload_text)
            except (TypeError, ValueError):
                payload = None
            verdict = _check(payload, key)
            if verdict is None:
                index_skipped += 1
                continue
            index_checked += 1
            if not verdict:
                index_mismatched += 1
                if len(index_sample) < 10:
                    index_sample.append(int(ev_id))

    result = {
        "truth": truth_scan,
        "index": {
            "checked": index_checked, "mismatched": index_mismatched,
            "unhashed_keys": index_skipped, "no_key": int(index_no_key),
            "mismatch_sample": index_sample,
        },
    }
    # The signal is the mismatch count *jumping* between runs, so persist this
    # run's counts in the manifest and surface the previous run's for comparison.
    # Locked read-modify-write: a checkpoint stamping its own keys concurrently
    # must not lose this baseline, nor vice versa.
    from datetime import datetime, timezone

    from .truth.jsonl_log import update_manifest

    baseline = {
        "at": datetime.now(timezone.utc).isoformat(),
        "truth_mismatched": truth_scan["mismatched"],
        "index_mismatched": index_mismatched,
    }
    previous: dict = {}

    def _stamp(m: dict) -> None:
        previous.update(m.get("hashes_baseline") or {})
        m["hashes_baseline"] = baseline

    update_manifest(log_dir(), _stamp)
    if previous:
        result["previous"] = previous
        result["delta"] = {
            "truth_mismatched": truth_scan["mismatched"] - int(previous.get("truth_mismatched", 0)),
            "index_mismatched": index_mismatched - int(previous.get("index_mismatched", 0)),
        }
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
    mismatch fail. Indexable events with no shadow row are re-extracted: one whose
    payload yields no searchable text legitimately has no row (reported as
    ``empty_extract_events``); one whose extraction yields text today is silently
    unfindable (``unindexed_events``) and fails — ``archive reindex`` rebuilds the
    surface.

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
        # events with no shadow row — is re-extracted event by event to split the
        # legitimately-empty (a payload that yields no searchable text has no row
        # by design) from the genuinely unindexed (extraction yields text today,
        # so the event is silently unfindable — real drift; `archive reindex`
        # rebuilds the surface). The gap set is small on a healthy archive, so
        # re-extracting only it stays cheap where re-extracting the corpus isn't.
        fts_orphans = fts_shadow_rows = fts5_rows = fts_empty_extract = 0
        fts_unindexed: list[int] = []
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
            from .retrieval._extract import INDEXABLE_EVENT_TYPES, extract_fts_content

            types = ", ".join(f"'{t}'" for t in INDEXABLE_EVENT_TYPES)
            cur = s.connection().connection.execute(  # raw sqlite3 — stream the gap set
                f"SELECT e.id, e.event_type, e.payload FROM events e "  # noqa: S608 — types from INDEXABLE_EVENT_TYPES
                f"WHERE e.id <= ? AND e.event_type IN ({types}) "
                "AND NOT EXISTS(SELECT 1 FROM events_fts f WHERE f.event_id = e.id)",
                (watermark,),
            )
            for ev_id, etype, payload_text in cur:
                try:
                    payload = _json.loads(payload_text) if isinstance(payload_text, str) else payload_text
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and extract_fts_content(etype, payload):
                    fts_unindexed.append(int(ev_id))
                else:
                    fts_empty_extract += 1

    ok = (
        not index_only and not missing and not key_mismatch
        and kg_index_only == 0
        and dangling_links == 0 and dangling_citations == 0 and dangling_events == 0
        and citation_thread_mismatch == 0
        and fts_orphans == 0 and fts_shadow_rows == fts5_rows and not fts_unindexed
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
            "unindexed_events": len(fts_unindexed),
            "unindexed_sample": fts_unindexed[:10],
            "empty_extract_events": int(fts_empty_extract),
        },
    }


def status(*, home: Optional[str] = None) -> dict:
    """Archive health: paths + thread/event/topic/link/FTS counts, plus the
    operational records — when the last checkpoint, verify, and backup ran and
    how they went (``last_verify`` / ``last_backup`` from ``<home>/health.json``,
    written by :func:`verify` / :func:`backup`). Staleness here is the signal
    that a scheduled integrity job quietly stopped running."""
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
    from .truth.jsonl_log import _read_manifest

    health = _read_health()
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
        "last_checkpoint_at": _read_manifest(paths.truth_dir).get("last_checkpoint_at"),
        "last_verify": health.get("verify_last"),
        "last_backup": health.get("backup_last"),
        "last_restore_drill": health.get("restore_drill_last"),
        "last_nightly": health.get("nightly_last"),
    }


def repair(*, home: Optional[str] = None, dry_run: bool = False) -> dict:
    """Quarantine unparseable truth lines and restore committed rows the truth
    lacks from the live index — the sanctioned path from a red ``verify`` back to
    green. See :func:`thread_archive.truth.repair.repair_truth`."""
    open_archive(home)
    from .truth import repair_truth

    return repair_truth(dry_run=dry_run)


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
