"""Snapshot: freeze the corpus into a self-contained, immutable archive home.

A snapshot is a point-in-time copy of the JSONL truth with its index
materialized alongside, laid out as an ordinary archive home:

    <dest>/
      truth/          # copied from the source (drain-consistent), incl. the vector sidecar
      index.db        # rebuilt from <dest>/truth — the cached, materialized projection
      vector-pack/    # the mmap vector cache, rebuilt lazily on first search
      snapshot.json   # provenance: when, from where, counts, embedding space, versions
      load-runs.jsonl # how the build went: copy / index / verify phase timings

Truth is the record; the index is a disposable cache rebuilt from it, so the
snapshot survives an index-format change under future code (a ``reindex`` in
place rebuilds it). The copied truth carries the durable vector sidecar
(``truth/vectors.sqlite``), so the rebuild restores the embeddings without
re-running the hours-long embed — the same recovery primitive ``rm index.db &&
reindex`` relies on.

Why freeze at all: a frozen corpus makes search deterministic. The same code
returns the same results forever, because the corpus can't grow underneath the
measurement — which is exactly what a regression gate and a scored bench
want. The number moves only when the *code* moves, never because the live
archive gained threads. It also removes the need for the ``until`` date bound
the mined-gold eval otherwise carries: a gold mined against the snapshot can't
be outranked by a thread that landed after mining, because no such thread
exists in the frozen corpus.

Some corpora are born frozen — an eval harness builds a home from a fixed
dataset and nothing ever appends to it. Copying such a home to freeze it buys
nothing, so :func:`stamp_snapshot` writes the manifest in place: same contract,
same content-derived id, no copy.

Point an eval at one with ``THREAD_ARCHIVE_HOME=<dest>``: the shipped
``thread_archive eval`` and the dev bench under ``search_lab/`` both resolve the home
from the environment, so a snapshot needs no new plumbing to score against.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .._config import resolve_paths
from .backup import _chmod_private, _mkdir_private, mirror_dir
from .verify import verify

SNAPSHOT_MANIFEST = "snapshot.json"


def corpus_fingerprint() -> str:
    """A deterministic content hash of the open archive's corpus — the identity a
    mined eval case binds to.

    Hashes ``(thread_id, event_count, max_occurred_at)`` for every thread in
    ``id`` order, so it changes whenever a thread is added or removed, an event
    lands, or a timestamp shifts — any change that could move a ranking or a
    gold's validity. A pure re-snapshot of an unchanged corpus reproduces the
    same id (the golds stay valid); a corpus that has grown gets a new one (the
    golds mined against the old one are detectably stale). Cheap: one grouped
    scan of ``events``."""
    import hashlib

    from sqlalchemy import text as sa_text

    from .._store import use_session

    h = hashlib.sha256()
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT thread_id, count(*), max(occurred_at) FROM events "
            "GROUP BY thread_id ORDER BY thread_id"
        ))
        for tid, n, mx in rows:
            h.update(f"{tid}\t{n}\t{mx}\n".encode())
    return h.hexdigest()[:16]


def read_snapshot_id(home: Optional[str] = None) -> Optional[str]:
    """The ``snapshot_id`` recorded in ``<home>/snapshot.json``, or None when the
    home is not a snapshot (no manifest, unreadable, or a pre-id one). The
    binding a mined case is checked against — an eval refuses cases whose id does
    not match the home it runs over."""
    path = resolve_paths(home).home / SNAPSHOT_MANIFEST
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("snapshot_id")
    except (OSError, json.JSONDecodeError):
        return None


def _embedding_space() -> Optional[str]:
    """The current embedding space key, or None when embeddings are unavailable
    (the lexical-only install) — recorded so a reader knows which model the
    snapshot's vectors resolve in."""
    try:
        from .._retrieval.embed import is_available, space_key

        return space_key() if is_available() else None
    except Exception:
        return None


def _write_manifest(dest: Path, manifest: dict) -> Path:
    """Write ``snapshot.json`` atomically and durably (tmp + fsync + rename)."""
    from .._truth.jsonl_log import _fsync_dir

    path = dest / SNAPSHOT_MANIFEST
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _chmod_private(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(dest)
    return path


def snapshot(
    dest: str,
    *,
    home: Optional[str] = None,
    vectors: bool = False,
    verify_result: bool = True,
    force: bool = False,
) -> dict:
    """Freeze the archive at ``home`` into a self-contained, immutable home at ``dest``.

    Checkpoints the source (so mutable authored tables land in truth), refreshes
    the durable vector sidecar (so live-embedded vectors ride the copy), then
    mirrors ``truth/`` into ``<dest>/truth`` under the truth-write lock — a
    drain-consistent copy, so no append batch can land mid-traversal. The index
    is then materialized in place: ``reindex`` rebuilds ``<dest>/index.db`` from
    the copied truth and restores the vectors from the copied sidecar (no
    re-embed). The result is an ordinary archive home that any tool resolves via
    ``THREAD_ARCHIVE_HOME``.

    ``vectors=True`` additionally embeds any events the sidecar cache lacks
    (normally none — the source sidecar was just refreshed); the default restores
    the cache only. ``verify_result`` verifies the built snapshot (truth ==
    index) and records the verdict in the manifest. ``force`` overwrites a
    non-empty destination that is not already a snapshot; a re-snapshot over an
    existing snapshot home is always allowed (the mirror is incremental).

    Returns the manifest dict plus ``dest`` and the reindex/verify sub-results.
    """
    dest_path = Path(dest).expanduser()
    existing_manifest = dest_path / SNAPSHOT_MANIFEST
    if dest_path.exists() and any(dest_path.iterdir()):
        if not existing_manifest.exists() and not force:
            raise FileExistsError(
                f"{dest_path} exists and is not empty (and holds no {SNAPSHOT_MANIFEST}); "
                "pass force=True to snapshot into it anyway"
            )

    from .._api import close, open_archive, reindex
    from .._retrieval.vectors import save_vectors_sidecar
    from .._truth import checkpoint as _checkpoint
    from .._truth.jsonl_log import _read_manifest, _truth_write_lock, _try_rebalance_lock

    src_paths = resolve_paths(home)
    src_home = str(src_paths.home)

    open_archive(home)
    _checkpoint()  # flush overlays + metadata updates → truth is a complete copy source
    source_verify_ok = bool(verify(home=home)["ok"]) if verify_result else None
    # Refresh the durable sidecar so live-embedded vectors (written into index.db
    # only) are in truth/ before the copy; a no-op on a lexical-only store.
    save_vectors_sidecar(src_paths.truth_dir)

    dest_truth = dest_path / "truth"
    _mkdir_private(dest_path)
    _chmod_private(dest_path, 0o700)
    _mkdir_private(dest_truth)

    # Preflight: the copy needs room for the truth tree, and the reindex builds a
    # second copy of the index (~ the truth size again). Refuse early rather than
    # dying mid-build with a half-written snapshot.
    truth_bytes = sum(p.stat().st_size for p in src_paths.truth_dir.rglob("*") if p.is_file())
    free = _free_bytes(dest_path)
    need = int(truth_bytes * 2.3)  # copied truth + the materialized index it builds
    if free < need:
        raise RuntimeError(
            f"snapshot: {free / 1e9:.1f} GB free at {dest_path} < {need / 1e9:.1f} GB needed "
            "(copied truth + its rebuilt index) — free disk space or choose another destination"
        )

    # Building a snapshot is a load like any other, tracked in the home being
    # built: on a large corpus it runs for tens of minutes, and the copy that
    # opens it is the stretch with nothing else to watch. ``reindex`` opens its
    # own run inside the index phase, so the live state there is its finer-grained
    # one and the ledger ends up holding both records.
    from .load_runs import load_run

    with load_run("snapshot", home=dest_path, note=f"from {src_home}") as run:
        # Drain-consistent copy: hold the truth-write mutex for the traversal so no
        # append batch lands in (or rolls back out of) a file mid-copy. The rebalance
        # lock keeps a shard sweep from moving files under the traversal. delete=False:
        # a snapshot is additive into its own (fresh, or prior-snapshot) tree.
        with run.phase("copy") as ph:
            with _try_rebalance_lock():
                with _truth_write_lock():
                    copy_result = mirror_dir(src_paths.truth_dir, dest_truth, delete=False)
            ph.count("files", int(copy_result.get("files_copied") or 0))
            ph.count("bytes", int(copy_result.get("bytes_copied") or 0))

        # Materialize the index from the copied truth. reindex repoints the process
        # engine at dest; restore the source pin afterward so a caller (or the next
        # test) is left where it started.
        try:
            with run.phase("index"):
                reindex_result = reindex(home=str(dest_path), vectors=vectors)
            if verify_result:
                with run.phase("verify"):
                    snap_verify = verify(home=str(dest_path))
            else:
                snap_verify = None
            dest_manifest_version = _read_manifest(dest_truth).get("version")
            snapshot_id = corpus_fingerprint()
        finally:
            close()
            open_archive(src_home)

    manifest = {
        "kind": "thread-archive-snapshot",
        "snapshot_id": snapshot_id,
        "created_at": _now_iso(),
        "source_home": src_home,
        "archive_version": _archive_version(),
        "truth_format_version": dest_manifest_version,
        "embedding_space": _embedding_space(),
        "counts": {
            "threads": reindex_result.get("threads"),
            "events": reindex_result.get("events"),
            "kg_events": reindex_result.get("kg_events"),
            "vectors": reindex_result.get("vectors_restored", 0)
            + reindex_result.get("vectors_embedded", 0),
        },
        "source_verify_ok": source_verify_ok,
        "verify_ok": bool(snap_verify["ok"]) if snap_verify is not None else None,
    }
    _write_manifest(dest_path, manifest)

    # A snapshot is a durable archive home, not workspace — make sure it is
    # registered (the reindex's open already did, unless registration was
    # suppressed or throttled) and say what it is. Fail-soft like every
    # registry write: a bookkeeping miss must not fail a built snapshot.
    from .archives import register, set_role

    register(dest_path, force=True)
    try:
        set_role(str(dest_path), "snapshot")
    except (KeyError, ValueError, OSError):
        pass

    return {
        "dest": str(dest_path),
        "manifest": manifest,
        "copy": copy_result,
        "reindex": reindex_result,
        "verify": snap_verify,
    }


def stamp_snapshot(home: Optional[str] = None, *, force: bool = False) -> dict:
    """Record a snapshot manifest for a home that is *already* frozen, copying
    nothing. Returns the manifest.

    :func:`snapshot` exists to make a frozen home out of a live one: it copies
    truth, materializes the index beside the copy, and stamps the result. A corpus
    home an eval harness builds from a fixed dataset is born frozen — nothing
    appends to it — so the copy buys nothing and the only thing missing is the
    identity a mined case binds to. This writes that identity in place, under the
    same manifest contract, so ``mine`` and ``retrieval_eval --cases`` accept the
    home and their golds still bind to a corpus that cannot move under them.

    Re-stamping is the cadence after a rebuild: the id is a content fingerprint, so
    a rebuilt corpus takes a new one and golds mined against the old id read as
    stale rather than silently scoring against a corpus that has changed shape.

    Refuses the default archive home unless ``force`` — the live archive grows, and
    an id stamped over it would go on blessing golds the corpus has moved past.
    """
    from sqlalchemy import func, select

    from .._api import open_archive, status
    from .._config import default_home
    from .._store import KgEvent, use_session
    from .._truth.jsonl_log import _read_manifest

    paths = resolve_paths(home)
    dest = paths.home
    if not force and dest.expanduser().resolve() == default_home().expanduser().resolve():
        raise ValueError(
            f"refusing to stamp the live archive at {dest} as a snapshot: it grows, "
            "so the recorded id would stop describing the corpus. Freeze a copy "
            "with `thread_archive snapshot <dir>`, or pass force=True."
        )
    if not paths.index_path.exists():
        raise FileNotFoundError(f"no archive home at {dest} (no {paths.index_path})")

    # Reading the corpus repoints the process engine; leave the caller pinned where
    # it started, like `snapshot` does around its reindex.
    prev = str(resolve_paths(None).home)
    open_archive(str(dest))
    try:
        counts = status(home=str(dest))
        with use_session() as s:
            kg_events = s.execute(select(func.count()).select_from(KgEvent)).scalar() or 0
        snapshot_id = corpus_fingerprint()
        truth_format_version = _read_manifest(paths.truth_dir).get("version")
        embedding_space = _embedding_space()
    finally:
        if prev != str(dest):
            open_archive(prev)

    manifest = {
        "kind": "thread-archive-snapshot",
        "snapshot_id": snapshot_id,
        "created_at": _now_iso(),
        "source_home": str(dest),
        # Distinguishes a home stamped where it was built from one copied out of a
        # live archive: same contract, but there is no separate source to name.
        "in_place": True,
        "archive_version": _archive_version(),
        "truth_format_version": truth_format_version,
        "embedding_space": embedding_space,
        "counts": {
            "threads": counts["threads"],
            "events": counts["events"],
            "kg_events": int(kg_events),
            "vectors": counts["vectors_indexed"],
        },
        "source_verify_ok": None,
        "verify_ok": None,
    }
    _write_manifest(dest, manifest)

    from .archives import register, set_role

    register(dest, force=True)
    try:
        set_role(str(dest), "snapshot")
    except (KeyError, ValueError, OSError):
        pass
    return manifest


def _free_bytes(path: Path) -> int:
    """Free bytes on the filesystem that will hold ``path`` (walk up to the first
    existing ancestor — the leaf may not exist yet)."""
    import shutil

    cur = path
    while not cur.exists():
        cur = cur.parent
    return shutil.disk_usage(cur).free


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _archive_version() -> str:
    from .. import __version__

    return __version__
