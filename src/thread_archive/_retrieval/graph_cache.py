"""On-disk persistence for the corpus graph: ``<index dir>/corpus-graph/``.

The graph (:mod:`.embed_graph`) is a process-local cache, so every restart starts
with none and the coherence re-rank stands down until a build lands — measured
here at a p50 of 8.7s and a p90 of 21.6s. The cost is not the interesting part:
the build is off the request path by construction, and a search runs correctly
without the graph. What a restart actually produces is a **window in which the
same query returns a different order**, with nothing in the output to say so.
Persisting the graph closes that window; skipping a rebuild when the corpus has
not moved is the smaller, secondary win.

Derived and disposable, like the vector pack it is built from: files are named by
the store's validity token, published by ``os.replace``, and swept when
superseded. Nothing here is authoritative — every failure path returns "no
cache", which is precisely the state this module was written to improve on.

**A loaded graph is served stale on purpose.** Requiring a token match would make
this useless under continuous ingest, where the token moves every few minutes and
a restart would essentially never find its own graph. It is safe because it is
what the in-memory cache already does: :func:`.embed_graph.get` serves whatever
it has and refreshes in the background. A thread the graph has not seen yet
resolves to no community, so it takes no boost — the re-rank degrades toward a
no-op for the corpus's new tail rather than misordering it. :func:`max_age_s`
bounds how much tail that can be (roughly 1% of this corpus per day).

The build's shape is recorded alongside and checked on load: neighbor count,
similarity floor, content-type scope, and the **community engine**, because Leiden
and the Louvain fallback partition some regions differently and a graph built by
one is not a graph built by the other. A mismatch is a miss, not a repair.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

if TYPE_CHECKING:
    from .embed_graph import CorpusGraph

logger = logging.getLogger(__name__)

DIRNAME = "corpus-graph"

#: Bumped when the on-disk layout changes. Part of the recorded build shape, so an
#: older file is a miss rather than a misread.
FORMAT = 1

#: Minimum seconds between writes, per process. The centroid matrix is tens of MB
#: and a busy corpus moves the token every few minutes; the disk copy exists to be
#: *a* recent graph for the next restart, never the current one, so writing it on
#: every build would spend real I/O for no gain.
_SAVE_INTERVAL_S = 300.0

#: Grace before a superseded file is unlinked. A reader that has just chosen a tag
#: and not yet opened its centroids would otherwise lose the race; it degrades to a
#: rebuild, so this is politeness rather than correctness.
_SWEEP_GRACE_S = 300.0

_TTL_ENV = "THREAD_ARCHIVE_GRAPH_CACHE_TTL_S"

_last_saved: dict[str, float] = {}


def max_age_s() -> float:
    """How old a persisted graph may be and still be served, from
    ``THREAD_ARCHIVE_GRAPH_CACHE_TTL_S`` (``0`` disables persistence entirely).

    A bound on coverage, expressed in time: the graph is served stale, and what
    staleness costs is the threads created since it was built, which take no
    coherence boost until the refresh lands. At this corpus's growth (~1%/day) the
    default week bounds that at a few percent of the pool.
    """
    raw = os.environ.get(_TTL_ENV)
    if raw is None:
        return 7 * 86400.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 7 * 86400.0


def _dir() -> Optional[Path]:
    """``<index dir>/corpus-graph/``, or None for a non-file DSN (the in-RAM store
    has no disk to persist to). Deliberately not the vector pack's directory: that
    one is swept by tag, and a graph file sitting in it would read as a stray."""
    from .._store import get_engine

    db = get_engine().url.database
    return Path(db).parent / DIRNAME if db else None


def _tag(token) -> str:
    """The file tag for a validity token.

    Only the token's cross-process half — ``(count, max_rowid)`` — names the file.
    Its first element is an in-process write counter that is always 0 in a freshly
    started reader, so including it would mean no process could ever recognize
    another's graph, which is the entire point of writing one down.
    """
    return f"{int(token[1])}-{int(token[2])}"


def _parse_tag(stem: str, prefix: str) -> Optional[tuple[int, int]]:
    try:
        count, rowid = stem[len(prefix):].split("-")
        return (int(count), int(rowid))
    except ValueError:  # a temp or foreign name that slipped the glob
        return None


def _newest(d: Path) -> Optional[tuple[tuple[int, int], Path]]:
    """The freshest persisted graph by token order, which for a growing corpus is
    also the most recent. Ordered by the token rather than by mtime so two readers
    of the same directory always agree on which file is the freshest."""
    best: Optional[tuple[tuple[int, int], Path]] = None
    for p in d.glob("graph-*.json"):
        key = _parse_tag(p.stem, "graph-")
        if key is not None and (best is None or key > best[0]):
            best = (key, p)
    return best


def _valid(doc: Any, params: dict, centroids: np.ndarray) -> bool:
    """Whether a loaded document is a graph this process can use.

    Shape-checked rather than trusted: the arrays are positional and the community
    map is keyed by thread id, so a truncated or hand-edited file would not fail
    loudly — it would rank differently. A rejected cache costs one rebuild.
    """
    if not isinstance(doc, dict) or doc.get("params") != params:
        return False
    ids, community = doc.get("thread_ids"), doc.get("community")
    if not isinstance(ids, list) or not all(isinstance(t, str) for t in ids):
        return False
    if not isinstance(community, dict):
        return False
    if not all(isinstance(k, str) and isinstance(v, int) for k, v in community.items()):
        return False
    if not isinstance(doc.get("edges"), int):
        return False
    return centroids.ndim == 2 and centroids.shape[0] == len(ids)


def load(params: dict) -> Optional[tuple[tuple[int, int], "CorpusGraph"]]:
    """The newest usable persisted graph as ``((count, max_rowid), graph)``.

    ``None`` whenever there is nothing to trust — no directory, no file, a build
    shape that differs from ``params``, a document that does not check out, or one
    past :func:`max_age_s`. ``None`` is never an error: it means this process
    builds the graph, which is what it did before this module existed.

    Only the newest tag is considered. A build-shape change invalidates every
    older file equally, so falling back through them would only read more files to
    reject them.
    """
    ttl = max_age_s()
    if ttl <= 0:
        return None
    try:
        d = _dir()
        if d is None or not d.is_dir():
            return None
        found = _newest(d)
        if found is None:
            return None
        key, meta_p = found
        doc = json.loads(meta_p.read_text(encoding="utf-8"))
        stamped = doc.get("built_at") if isinstance(doc, dict) else None
        if not isinstance(stamped, str):
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamped)).total_seconds()
        if age > ttl or age < 0:  # stale, or a clock that moved backwards
            return None
        centroids = np.load(d / f"centroids-{key[0]}-{key[1]}.npy")
        if not _valid(doc, params, centroids):
            return None
        from .embed_graph import CorpusGraph

        graph = CorpusGraph(
            thread_ids=list(doc["thread_ids"]),
            centroids=np.ascontiguousarray(centroids, dtype=np.float32),
            community=dict(doc["community"]),
            edges=int(doc["edges"]),
        )
        logger.info(
            "embed_graph: loaded %d threads from disk (%.0fs old), skipping a build",
            len(graph.thread_ids), age,
        )
        _sweep(d, f"{key[0]}-{key[1]}")
        return key, graph
    except Exception:  # noqa: BLE001 — a bad cache must never break retrieval
        logger.debug("embed_graph: could not load the persisted graph", exc_info=True)
        return None


def save(token, graph: "CorpusGraph", params: dict, *, force: bool = False) -> bool:
    """Persist ``graph`` under ``token``'s tag; returns whether a write happened.

    The centroids land first and the document last, because the document is what
    :func:`load` looks for and it names the array file it came with — so a write
    interrupted anywhere leaves the previous graph readable and the partial one
    invisible, never a new document paired with old centroids.
    """
    if max_age_s() <= 0:
        return False
    try:
        d = _dir()
        if d is None:
            return False
        now = time.monotonic()
        room = str(d)
        if not force and now - _last_saved.get(room, float("-inf")) < _SAVE_INTERVAL_S:
            return False
        tag = _tag(token)
        d.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()

        cent_p = d / f"centroids-{tag}.npy"
        tmp = d / f"{cent_p.name}.tmp.{pid}"
        with open(tmp, "wb") as fh:  # a handle: np.save must not append '.npy'
            np.save(fh, np.ascontiguousarray(graph.centroids, dtype=np.float32))
        os.replace(tmp, cent_p)

        meta_p = d / f"graph-{tag}.json"
        tmp = d / f"{meta_p.name}.tmp.{pid}"
        tmp.write_text(json.dumps({
            "params": params,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "thread_ids": graph.thread_ids,
            "community": graph.community,
            "edges": graph.edges,
        }), encoding="utf-8")
        os.replace(tmp, meta_p)

        _last_saved[room] = now
        _sweep(d, tag)
        return True
    except Exception:  # noqa: BLE001 — persistence is an optimization; never raise
        logger.debug("embed_graph: could not persist the graph", exc_info=True)
        return False


def _sweep(d: Path, keep_tag: str) -> None:
    """Unlink everything that is not ``keep_tag``'s pair. Aged out rather than
    dropped on sight so a reader mid-load is not raced (see :data:`_SWEEP_GRACE_S`).

    Runs on load as well as on save. On save alone, a process that read a graph and
    never built one would leave the superseded pair — tens of MB — sitting until
    something happened to write, which on a quiet archive is never.

    A ``.tmp.`` file is swept on age regardless of its tag: it carries the tag of the
    graph it was going to become, so a writer that died mid-save would otherwise leave
    one that matches ``keep_tag`` forever.
    """
    for stray in d.iterdir():
        if f"-{keep_tag}." in stray.name and ".tmp." not in stray.name:
            continue
        try:
            if time.time() - stray.stat().st_mtime > _SWEEP_GRACE_S:
                stray.unlink()
        except OSError:  # racing another sweeper
            pass


def drop() -> None:
    """Discard every persisted graph — the next build has nothing to load.

    Called when the vectors underneath change without moving the store token (an
    in-place re-embed), which the tag cannot see and a served graph would
    therefore not reflect.
    """
    try:
        d = _dir()
        if d is None:
            return
        for stray in d.glob("*"):
            stray.unlink(missing_ok=True)
    except OSError:
        pass
    _last_saved.clear()


def describe() -> dict:
    """What is on disk, for an operator asking whether a restart will have a graph
    to serve: ``{"present": bool}`` plus the tag, age and size when there is one."""
    try:
        d = _dir()
        if d is None or not d.is_dir():
            return {"present": False}
        found = _newest(d)
        if found is None:
            return {"present": False}
        key, meta_p = found
        doc = json.loads(meta_p.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(doc["built_at"])).total_seconds()
        return {
            "present": True,
            "token": list(key),
            "built_at": doc["built_at"],
            "age_s": round(age, 1),
            "threads": len(doc.get("thread_ids") or []),
            "edges": doc.get("edges"),
            "engine": (doc.get("params") or {}).get("engine"),
            "bytes": sum(p.stat().st_size for p in d.glob(f"*-{key[0]}-{key[1]}.*")),
        }
    except Exception:  # noqa: BLE001 — a status read must never raise
        return {"present": False}
