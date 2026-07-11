"""SQLite embedded semantic search.

One in-DB ``event_vectors`` BLOB table holding 768-d nomic vectors, searched by an
in-process brute-force cosine KNN (numpy). No server, no native extension, no HNSW:
the vectors live in the archive SQLite DB beside the data. Vectors are float32 and
unit-normalized, so cosine = dot product; at single-user scale the matrix loads once
and a query is a single BLAS matvec (~ms).

Populated from the ``events_fts`` shadow's ``user`` / ``text`` / ``title`` /
``summary`` pools via the ``[embeddings]`` provider (:mod:`.embed`). Cached durably in a ``vectors.sqlite``
sidecar so the hours-long embed survives ``rm index.db && reindex``. Degrades to
lexical-only when the extra isn't installed or nothing's indexed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import numpy as np
from sqlalchemy import text as sa_text

from ..store import get_engine, get_session
from .fts import build_event_hit

logger = logging.getLogger(__name__)

_DIM = 768
_CREATE_VEC = (
    "CREATE TABLE event_vectors ("
    "event_id INTEGER NOT NULL, content_type TEXT NOT NULL, dim INTEGER NOT NULL, "
    "vec BLOB NOT NULL, PRIMARY KEY (event_id, content_type))"
)

# Embedded content-type pools: user → default pool; text → scoped assistant pool;
# title/summary → the thread-meta docs (thread-level aboutness).
_USER_CONTENT_TYPES = ("user",)
_ASSISTANT_CONTENT_TYPES = ("text",)
_META_CONTENT_TYPES = ("title", "summary")

# Process-local matrix cache: {(engine_id, cts): (validity_token, ids, ctypes, mat)}.
# The token includes store-derived counters (row count + max rowid), not just the
# process-local write version: the embed cohost lives in the *watcher* process, so a
# long-lived search process (the MCP server) must notice out-of-process vector writes
# or its semantic arm freezes at whatever was embedded when its matrix first loaded.
_MATRIX_CACHE: dict = {}
_write_version = 0


def is_available() -> bool:
    try:
        return get_engine().dialect.name == "sqlite"
    except Exception:
        return False


def ensure_index() -> bool:
    """Create the ``event_vectors`` table if absent. Idempotent."""
    if not is_available():
        return False
    with get_session() as s:
        exists = s.execute(
            sa_text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": "event_vectors"}
        ).scalar()
        if not exists:
            s.execute(sa_text(_CREATE_VEC))
            s.commit()
    return True


def _normalize(v) -> "np.ndarray":
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0.0 else v


def _scope_content_types(content_types: Optional[list[str]]) -> Optional[list[str]]:
    embedded = _USER_CONTENT_TYPES + _ASSISTANT_CONTENT_TYPES + _META_CONTENT_TYPES
    if content_types:
        cts = [c for c in content_types if c in embedded]
    else:
        cts = list(embedded)
    return cts or None


def index_vectors(records) -> int:
    """Upsert ``(event_id, content_type, vector)`` rows into ``event_vectors``."""
    if not is_available():
        return 0
    ensure_index()
    rows = []
    for event_id, content_type, vec in records:
        arr = _normalize(np.asarray(vec, dtype=np.float32))
        if arr.shape[0] != _DIM:
            logger.warning("vectors: skipping event %s — dim %d != %d", event_id, arr.shape[0], _DIM)
            continue
        rows.append({"eid": int(event_id), "ct": str(content_type),
                     "dim": int(arr.shape[0]), "vec": arr.tobytes()})
    if not rows:
        return 0
    with get_session() as s:
        s.execute(sa_text(
            "INSERT INTO event_vectors (event_id, content_type, dim, vec) "
            "VALUES (:eid, :ct, :dim, :vec) "
            "ON CONFLICT (event_id, content_type) DO UPDATE SET dim = excluded.dim, vec = excluded.vec"
        ), rows)
        s.commit()
    _bump_version()
    return len(rows)


def index_events_local(
    batch_size: int = 64,
    rebuild: bool = False,
    max_events: int | None = None,
    newest_first: bool = False,
) -> int:
    """Compute event vectors in-process from the FTS shadow (user/text/title/summary pools).

    ``rebuild=False`` only embeds events missing a vector (anti-join — safe to
    re-run). ``max_events`` caps how many events a single call embeds — the live
    cohost bounds each pass so a backlog drains over cycles without stalling ingest;
    ``newest_first`` drains the freshest gap first, which is what keeps recent-thread
    *semantic* recall current (the lexical arm already covers fresh threads). No-op
    (0) when the store isn't SQLite or the embed backend is absent.
    """
    if not is_available():
        return 0
    from .embed import embed_documents
    from .embed import is_available as embed_available

    if not embed_available():
        logger.info("vectors.index_events_local: embed backend unavailable — skipping")
        return 0
    ensure_index()

    missing = "" if rebuild else " AND v.event_id IS NULL"
    order = "DESC" if newest_first else "ASC"
    limit = " LIMIT :cap" if max_events else ""
    sql = sa_text(
        "SELECT f.event_id AS eid, f.content_type AS ct, group_concat(f.content, ' ') AS content "
        "FROM events_fts f "
        "LEFT JOIN event_vectors v ON v.event_id = f.event_id AND v.content_type = f.content_type "
        "WHERE f.content_type IN ('user', 'text', 'title', 'summary') "
        "AND f.content IS NOT NULL AND f.content != ''"
        + missing +
        f" GROUP BY f.event_id, f.content_type ORDER BY f.event_id {order}" + limit
    )
    params = {"cap": int(max_events)} if max_events else {}
    with get_session() as s:
        pending = [(r.eid, r.ct, r.content) for r in s.execute(sql, params)]

    total = 0
    for start in range(0, len(pending), batch_size):
        chunk = pending[start:start + batch_size]
        vecs = embed_documents([c for _, _, c in chunk])
        if not vecs:
            logger.warning("vectors.index_events_local: embed returned None — stopping at %d", total)
            break
        total += index_vectors((eid, ct, vec) for (eid, ct, _), vec in zip(chunk, vecs))
    return total


def save_vectors_sidecar(truth_dir, space_key: Optional[str] = None) -> int:
    """Persist ``event_vectors`` to ``<truth_dir>/vectors.sqlite`` — a durable cache so
    the expensive embed survives ``rm index.db && reindex``. Tagged with the embedding
    space_key so a model change invalidates it.

    A no-op (0) when the live store has no vectors at all: an empty save must never
    replace a populated sidecar — the cache exists precisely to survive the states
    (fresh index, vectors not yet restored) that would otherwise gut it."""
    if not is_available():
        return 0
    import os
    from pathlib import Path

    from .embed import space_key as _sk
    with get_session() as s:
        exists = s.execute(
            sa_text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": "event_vectors"}
        ).scalar()
        live = s.execute(sa_text("SELECT count(*) FROM event_vectors")).scalar() if exists else 0
    if not live:
        return 0
    sk = space_key or _sk()
    path = Path(truth_dir) / "vectors.sqlite"
    # Build into a temp sidecar and rename over the live one: a crash mid-save must
    # not leave a gutted cache (the embed it protects takes hours to redo).
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    with get_engine().connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.exec_driver_sql("ATTACH DATABASE ? AS side", (str(tmp),))
        try:
            conn.exec_driver_sql("CREATE TABLE side.event_vectors AS SELECT * FROM event_vectors")
            conn.exec_driver_sql("CREATE TABLE side.vector_meta (space_key TEXT)")
            conn.exec_driver_sql("INSERT INTO side.vector_meta (space_key) VALUES (?)", (sk,))
            n = conn.exec_driver_sql("SELECT count(*) FROM side.event_vectors").scalar()
        finally:
            conn.exec_driver_sql("DETACH DATABASE side")
    os.replace(tmp, path)
    logger.info("vectors: saved %s vectors to sidecar (space=%s)", n, sk)
    return int(n or 0)


def load_vectors_sidecar(truth_dir, space_key: Optional[str] = None) -> int:
    """Restore cached vectors from ``<truth_dir>/vectors.sqlite`` iff its space_key
    matches the current embedding space. Returns rows after restore (0 if none usable)."""
    if not is_available():
        return 0
    from pathlib import Path

    from .embed import space_key as _sk
    path = Path(truth_dir) / "vectors.sqlite"
    if not path.exists():
        return 0
    ensure_index()
    want = space_key or _sk()
    with get_engine().connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.exec_driver_sql("ATTACH DATABASE ? AS side", (str(path),))
        try:
            have = conn.exec_driver_sql("SELECT space_key FROM side.vector_meta LIMIT 1").scalar()
            if have != want:
                logger.info("vector sidecar space %r != current %r — re-embedding", have, want)
                return 0
            conn.exec_driver_sql(
                "INSERT OR IGNORE INTO event_vectors (event_id, content_type, dim, vec) "
                "SELECT event_id, content_type, dim, vec FROM side.event_vectors"
            )
            n = conn.exec_driver_sql("SELECT count(*) FROM event_vectors").scalar()
        finally:
            conn.exec_driver_sql("DETACH DATABASE side")
    _bump_version()
    logger.info("vectors: restored %s vectors from sidecar", n)
    return int(n or 0)


def _bump_version() -> None:
    global _write_version
    _write_version += 1


def _validity_token(s) -> tuple:
    """Cheap cross-process staleness probe for the matrix cache. Catches inserts
    (max rowid grows) and deletes (count shrinks); an in-place upsert of an existing
    row is invisible, which the in-process ``_write_version`` covers."""
    row = s.execute(
        sa_text("SELECT count(*), coalesce(max(rowid), 0) FROM event_vectors")
    ).one()
    return (_write_version, int(row[0]), int(row[1]))


def _load_matrix(cts: tuple[str, ...]):
    eng = get_engine()
    key = (id(eng), cts)
    with get_session() as s:
        token = _validity_token(s)
        cached = _MATRIX_CACHE.get(key)
        if cached is not None and cached[0] == token:
            return cached[1], cached[2], cached[3]

        where = "content_type IN (" + ",".join(":c" + str(i) for i in range(len(cts))) + ")"
        params = {"c" + str(i): c for i, c in enumerate(cts)}
        ids: list[int] = []
        ctypes: list[str] = []
        vecs: list[np.ndarray] = []
        result = s.execute(
            sa_text("SELECT event_id, content_type, vec FROM event_vectors WHERE " + where), params,
        )
        for r in result:
            ids.append(int(r[0]))
            ctypes.append(str(r[1]))
            vecs.append(np.frombuffer(r[2], dtype=np.float32))
    mat = np.vstack(vecs) if vecs else np.empty((0, _DIM), dtype=np.float32)
    ids_arr = np.asarray(ids, dtype=np.int64)
    ct_arr = np.asarray(ctypes, dtype=object)
    _MATRIX_CACHE[key] = (token, ids_arr, ct_arr, mat)
    return ids_arr, ct_arr, mat


def _knn(qvec, cts: tuple[str, ...], cand: int, allowed_ids=None) -> list[tuple[int, str, float]]:
    """Brute-force cosine KNN over the cached matrix. ``allowed_ids`` (an int64
    ndarray) restricts candidates to those event ids *before* the top-k cut, so a
    scoped search (one thread, a time window) ranks within its scope instead of
    hoping the scope survives a corpus-wide top-k."""
    ids, ct_arr, mat = _load_matrix(cts)
    n = len(ids)
    if n == 0:
        return []
    q = _normalize(qvec)
    sims = mat @ q
    if allowed_ids is not None:
        keep = np.nonzero(np.isin(ids, allowed_ids))[0]
        if len(keep) == 0:
            return []
        sub_sims = sims[keep]
        k = min(cand, len(keep))
        part = np.argpartition(-sub_sims, k - 1)[:k] if k < len(keep) else np.arange(len(keep))
        pool = keep[part]
    else:
        k = min(cand, n)
        pool = np.argpartition(-sims, k - 1)[:k] if k < n else np.arange(n)
    order = sorted(pool, key=lambda i: (-float(sims[i]), int(ids[i])))
    return [(int(ids[i]), str(ct_arr[i]), float(sims[i])) for i in order]


def _in_clause(column: str, values: list, prefix: str, params: dict, negate: bool) -> str:
    names = []
    for i, v in enumerate(values):
        key = prefix + str(i)
        params[key] = v
        names.append(":" + key)
    op = " NOT IN (" if negate else " IN ("
    return column + op + ",".join(names) + ")"


def search(
    query: str,
    thread_id: Optional[int] = None,
    content_types: Optional[list[str]] = None,
    limit: int = 20,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    exclude_content_types: Optional[list[str]] = None,
    source: Optional[list[str]] = None,
) -> Optional[list[dict]]:
    """Embedded semantic search: embed the query, brute-force cosine KNN, hydrate.

    Returns None when this isn't SQLite, the scope has no embedded pool, nothing's
    indexed, or the embed fails — so search degrades to the lexical arm.
    """
    if not is_available() or not query or not query.strip():
        return None
    ensure_index()
    cts = _scope_content_types(content_types)
    if not cts:
        return None

    with get_session() as s:
        total = s.execute(sa_text("SELECT count(*) FROM event_vectors")).scalar() or 0
    if not total:
        return None

    try:
        from .embed import embed_query
        qvec = embed_query(query)
    except Exception as e:  # noqa: BLE001
        logger.debug("vectors: query embed failed: %s", e)
        return None
    if not qvec:
        return None

    # A scoped search (one thread / time window / source) pre-masks the KNN to the
    # in-scope event ids, so ranking happens *within* the scope — a corpus-wide
    # top-k could miss the scope entirely. Unscoped searches skip the id query.
    selective = thread_id is not None or since is not None or until is not None or bool(source)
    allowed_ids = None
    if selective:
        awhere = []
        aparams: dict = {}
        ajoin = "FROM events e"
        if thread_id is not None:
            awhere.append("e.thread_id = :tid")
            aparams["tid"] = thread_id
        if since:
            awhere.append("e.occurred_at >= :since")
            aparams["since"] = since
        if until:
            awhere.append("e.occurred_at <= :until")
            aparams["until"] = until
        if source:
            ajoin += " JOIN threads t ON t.id = e.thread_id"
            awhere.append(_in_clause("t.source", source, "src", aparams, negate=False))
        with get_session() as s:
            allowed = [int(r[0]) for r in s.execute(
                sa_text("SELECT e.id " + ajoin + " WHERE " + " AND ".join(awhere)), aparams,
            )]
        if not allowed:
            return []
        allowed_ids = np.asarray(allowed, dtype=np.int64)
    cand = max(limit * 3, 100)
    candidates = _knn(qvec, tuple(cts), cand, allowed_ids=allowed_ids)
    if not candidates:
        return []
    sim_by: dict[tuple[int, str], float] = {(eid, ct): sim for eid, ct, sim in candidates}
    event_ids = sorted({eid for eid, _, _ in candidates})

    where = [_in_clause("f.event_id", event_ids, "e", {}, negate=False)]
    params: dict = {"e" + str(i): v for i, v in enumerate(event_ids)}
    if thread_id is not None:
        where.append("f.thread_id = :tid")
        params["tid"] = thread_id
    else:
        # Honor the per-thread search blacklist; an explicit thread scope bypasses it.
        where.append("f.thread_id NOT IN (SELECT id FROM threads WHERE exclude_from_search)")
    if exclude_content_types:
        where.append(_in_clause("f.content_type", exclude_content_types, "xct", params, negate=True))
    if since:
        where.append("e.occurred_at >= :since")
        params["since"] = since
    if until:
        where.append("e.occurred_at <= :until")
        params["until"] = until
    join = "FROM events_fts f JOIN events e ON e.id = f.event_id"
    if source:
        join += " JOIN threads t ON t.id = f.thread_id"
        where.append(_in_clause("t.source", source, "src", params, negate=False))

    sql = sa_text(
        "SELECT f.event_id, f.thread_id, f.event_type, f.content_type, "
        "f.content AS full_content, e.occurred_at " + join +
        " WHERE " + " AND ".join(where)
    )
    with get_session() as s:
        rows = s.execute(sql, params).mappings().all()

    hydrated: list[tuple[float, dict]] = []
    for r in rows:
        sim = sim_by.get((r["event_id"], r["content_type"]))
        if sim is None:
            continue
        ts = 0
        oa = r["occurred_at"]
        if oa:
            try:
                ts = int(datetime.fromisoformat(str(oa)).timestamp())
            except ValueError:
                ts = 0
        content = r["full_content"] or ""
        hit = build_event_hit(
            event_id=r["event_id"], thread_id=r["thread_id"], event_type=r["event_type"],
            content_type=r["content_type"], snippet=content[:300], full_content=content,
            occurred_at_ts=ts,
        )
        hit["_semantic"] = round(float(sim), 4)
        hydrated.append((sim, hit))

    hydrated.sort(key=lambda t: (-t[0], t[1]["event_id"]))
    return [h for _, h in hydrated[:limit]]


def get_status() -> dict:
    if not is_available():
        return {"available": False}
    with get_session() as s:
        exists = s.execute(
            sa_text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": "event_vectors"}
        ).scalar()
        count = s.execute(sa_text("SELECT count(*) FROM event_vectors")).scalar() if exists else 0
    return {"available": True, "indexed": int(count or 0), "table": "event_vectors", "dim": _DIM}
