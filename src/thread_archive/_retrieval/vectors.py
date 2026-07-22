"""SQLite embedded semantic search.

One in-DB ``event_vectors`` BLOB table holding 768-d nomic vectors, searched by an
in-process brute-force cosine KNN (numpy). No server, no native extension, no HNSW:
the vectors live in the archive SQLite DB beside the data. Vectors are float32 and
unit-normalized, so cosine = dot product; a query is a single BLAS matvec (~ms).

The KNN matrix is **mmap'd, not resident**: the full corpus is packed once into
token-named ``.npy`` files (``<home>/vector-pack/``, derived + disposable) and
``np.load(mmap_mode='r')`` serves every content-type scope from the one pack via
row masks — RAM cost is page cache the OS can reclaim, not per-process RSS, so
the ceiling scales with disk instead of memory. Same float32 bits, same scores.

Long documents are **chunked**: a doc is embedded as one vector per
:data:`CHUNK_CHARS`-char slice (up to :data:`MAX_CHUNKS`), keyed
``(event_id, content_type, chunk)``, and the KNN max-pools per document — the
best-matching chunk speaks for the doc. Without this, everything past the embed
cap of a long message is semantically invisible (the embed provider truncates),
and the vocab-mismatch queries the vector arm exists for are exactly the ones
that can't fall back to keywords.

Populated from the ``events_fts`` shadow's ``user`` / ``text`` / ``title`` /
``summary`` pools via the ``[embeddings]`` provider (:mod:`.embed`). Cached durably in a ``vectors.sqlite``
sidecar so the hours-long embed survives ``rm index.db && reindex``. Degrades to
lexical-only when the extra isn't installed or nothing's indexed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import numpy as np
from sqlalchemy import text as sa_text

from .._store import get_engine, get_session
from ._types import EventHit
from .embed import EMBEDDING_CHAR_CAP
from .fts import build_event_hit

logger = logging.getLogger(__name__)

_DIM = 768
_CREATE_VEC = (
    "CREATE TABLE event_vectors ("
    "event_id INTEGER NOT NULL, content_type TEXT NOT NULL, "
    "chunk INTEGER NOT NULL DEFAULT 0, dim INTEGER NOT NULL, "
    "vec BLOB NOT NULL, PRIMARY KEY (event_id, content_type, chunk))"
)

# Document chunking: one vector per CHUNK_CHARS-char slice, at most MAX_CHUNKS per
# doc (16k chars — beyond that is tool-dump territory the embedded pools exclude
# anyway). CHUNK_CHARS equals the embed provider's input cap so a chunk is
# embedded whole, never re-truncated. The expected-chunk-count formula
# ``min(MAX_CHUNKS, ceil(len/CHUNK_CHARS))`` is duplicated in SQL by the drain's
# anti-join (:func:`index_events_local`) — the two MUST stay identical or the
# cohost re-embeds the same docs forever.
CHUNK_CHARS = EMBEDDING_CHAR_CAP
MAX_CHUNKS = 8


def _chunk(content: str) -> list[str]:
    """Split ``content`` into the embed slices (non-overlapping, CHUNK_CHARS each,
    capped at MAX_CHUNKS). Always at least one chunk for non-empty content."""
    return [
        content[i:i + CHUNK_CHARS]
        for i in range(0, min(len(content), MAX_CHUNKS * CHUNK_CHARS), CHUNK_CHARS)
    ] or [content]

# Embedded content-type pools: user → default pool; text → scoped assistant pool;
# title/summary → the thread-meta docs (thread-level aboutness).
_USER_CONTENT_TYPES = ("user",)
_ASSISTANT_CONTENT_TYPES = ("text",)
_META_CONTENT_TYPES = ("title", "summary")

# Process-local matrix cache: {(engine_id, cts): (validity_token, ids, ctypes, mat,
# doc_inverse, doc_rep, scope_rows)}. Keys are canonicalized (sorted cts tuple) so
# equivalent scopes in different orders share one entry; ``mat`` is the shared mmap
# of the full pack, so an entry pins only the scope's small index arrays, and the
# bound (:data:`_MATRIX_CACHE_MAX`) guards scope proliferation.
# The token includes store-derived counters (row count + max rowid), not just the
# process-local write version: the embed cohost lives in the *watcher* process, so a
# long-lived search process (the MCP server) must notice out-of-process vector writes
# or its semantic arm freezes at whatever was embedded when its matrix first loaded.
_MATRIX_CACHE: dict = {}
_MATRIX_CACHE_MAX = 4
_write_version = 0

# Pack files for a superseded token are swept once they age out — a mapped-in
# reader elsewhere may still be serving queries off them (its unlinked inode
# stays valid; the age is grace, not correctness).
_PACK_STALE_AGE_S = 3600


def _pack_dir() -> Optional[Path]:
    """``<home>/vector-pack/`` beside the index — derived, disposable, rebuilt
    whenever the store token moves. None for a non-file DSN (the in-RAM path)."""
    db = get_engine().url.database
    return Path(db).parent / "vector-pack" if db else None


def _corpus_arrays(s) -> tuple:
    """The full embedded corpus as arrays: (mat, ids, ct_codes, ct_names).
    Deterministic order (the PK) so two rival pack builders of the same token
    write byte-identical files."""
    ids: list[int] = []
    cts: list[str] = []
    vecs: list[np.ndarray] = []
    result = s.execute(sa_text(
        "SELECT event_id, content_type, vec FROM event_vectors "
        "ORDER BY event_id, content_type, chunk"
    ))
    for r in result:
        ids.append(int(r[0]))
        cts.append(str(r[1]))
        vecs.append(np.frombuffer(r[2], dtype=np.float32))
    ct_names = sorted(set(cts))
    codes = {c: i for i, c in enumerate(ct_names)}
    mat = np.vstack(vecs) if vecs else np.empty((0, _DIM), dtype=np.float32)
    ids_arr = np.asarray(ids, dtype=np.int64)
    ct_codes = np.asarray([codes[c] for c in cts], dtype=np.int16)
    return mat, ids_arr, ct_codes, ct_names


def _ensure_pack(s, store_token: tuple[int, int]) -> tuple:
    """The full-corpus mmap pack for ``store_token`` — build if absent, then
    return (mat[mmap], ids, ct_codes, ct_names). Files are token-named, so a
    reader can never mix arrays from two builds, and rival concurrent builders
    write byte-identical content (deterministic ORDER BY) — whichever
    ``os.replace`` lands last changes nothing. Falls back to in-RAM arrays for
    a non-file DSN."""
    d = _pack_dir()
    if d is None:
        return _corpus_arrays(s)
    tag = f"{store_token[0]}-{store_token[1]}"
    paths = {k: d / f"{k}-{tag}.npy" for k in ("mat", "ids", "cts")}
    meta_p = d / f"meta-{tag}.json"
    if not (meta_p.exists() and all(p.exists() for p in paths.values())):
        mat, ids_arr, ct_codes, ct_names = _corpus_arrays(s)
        d.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        for k, arr in (("mat", mat), ("ids", ids_arr), ("cts", ct_codes)):
            tmp = d / f"{k}-{tag}.npy.tmp.{pid}"
            with open(tmp, "wb") as f:  # a handle: np.save must not append '.npy'
                np.save(f, arr)
            os.replace(tmp, paths[k])
        tmp = d / f"{meta_p.name}.tmp.{pid}"
        tmp.write_text(json.dumps({"ct_names": ct_names, "rows": len(ids_arr)}))
        os.replace(tmp, meta_p)
        for stray in d.iterdir():  # sweep aged-out packs of superseded tokens
            if f"-{tag}." in stray.name:
                continue
            try:
                if time.time() - stray.stat().st_mtime > _PACK_STALE_AGE_S:
                    stray.unlink()
            except OSError:  # racing another sweeper
                pass
    mat = np.load(paths["mat"], mmap_mode="r")
    ids_arr = np.load(paths["ids"])
    ct_codes = np.load(paths["cts"])
    ct_names = json.loads(meta_p.read_text())["ct_names"]
    return mat, ids_arr, ct_codes, ct_names


def is_available() -> bool:
    try:
        return get_engine().dialect.name == "sqlite"
    except Exception:
        return False


def ensure_index() -> bool:
    """Create the ``event_vectors`` table if absent; migrate a pre-chunking table
    (no ``chunk`` column) in place, existing vectors becoming chunk 0. Idempotent."""
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
        cols = {r[1] for r in s.execute(sa_text("PRAGMA table_info(event_vectors)"))}
        if "chunk" not in cols:
            # SQLite can't extend a PRIMARY KEY in place: rebuild the table around
            # the chunked key, carrying every existing vector over as chunk 0.
            s.execute(sa_text("ALTER TABLE event_vectors RENAME TO event_vectors_prechunk"))
            s.execute(sa_text(_CREATE_VEC))
            s.execute(sa_text(
                "INSERT INTO event_vectors (event_id, content_type, chunk, dim, vec) "
                "SELECT event_id, content_type, 0, dim, vec FROM event_vectors_prechunk"
            ))
            s.execute(sa_text("DROP TABLE event_vectors_prechunk"))
            s.commit()
            _bump_version()
            logger.info("vectors: migrated event_vectors to the chunked schema")
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
    """Upsert vector rows into ``event_vectors``. Each record is
    ``(event_id, content_type, vector)`` (chunk 0) or
    ``(event_id, content_type, chunk, vector)``."""
    if not is_available():
        return 0
    ensure_index()
    rows = []
    for rec in records:
        event_id, content_type, chunk, vec = rec if len(rec) == 4 else (rec[0], rec[1], 0, rec[2])
        arr = _normalize(np.asarray(vec, dtype=np.float32))
        if arr.shape[0] != _DIM:
            logger.warning("vectors: skipping event %s — dim %d != %d", event_id, arr.shape[0], _DIM)
            continue
        rows.append({"eid": int(event_id), "ct": str(content_type), "chunk": int(chunk),
                     "dim": int(arr.shape[0]), "vec": arr.tobytes()})
    if not rows:
        return 0
    with get_session() as s:
        s.execute(sa_text(
            "INSERT INTO event_vectors (event_id, content_type, chunk, dim, vec) "
            "VALUES (:eid, :ct, :chunk, :dim, :vec) "
            "ON CONFLICT (event_id, content_type, chunk) "
            "DO UPDATE SET dim = excluded.dim, vec = excluded.vec"
        ), rows)
        s.commit()
    _bump_version()
    # A pure in-place upsert moves neither row count nor max rowid, so the pack
    # token can't see it — drop the pack metas so the next load rebuilds.
    d = _pack_dir()
    if d is not None:
        for stray in d.glob("meta-*.json"):
            stray.unlink(missing_ok=True)
    return len(rows)


def _write_doc_vectors(docs: list[tuple[int, str, list]]) -> None:
    """Replace each doc's vector set: delete every chunk row for the
    ``(event_id, content_type)``, insert the new chunk vectors. One transaction —
    a doc is never left half-chunked."""
    with get_session() as s:
        for eid, ct, vecs in docs:
            s.execute(
                sa_text("DELETE FROM event_vectors WHERE event_id = :eid AND content_type = :ct"),
                {"eid": int(eid), "ct": str(ct)},
            )
            for chunk_idx, vec in enumerate(vecs):
                arr = _normalize(np.asarray(vec, dtype=np.float32))
                if arr.shape[0] != _DIM:
                    logger.warning("vectors: skipping event %s — dim %d != %d",
                                   eid, arr.shape[0], _DIM)
                    continue
                s.execute(sa_text(
                    "INSERT INTO event_vectors (event_id, content_type, chunk, dim, vec) "
                    "VALUES (:eid, :ct, :chunk, :dim, :vec)"
                ), {"eid": int(eid), "ct": str(ct), "chunk": chunk_idx,
                    "dim": int(arr.shape[0]), "vec": arr.tobytes()})
        s.commit()
    _bump_version()


def index_events_local(
    batch_size: int = 64,
    rebuild: bool = False,
    max_events: int | None = None,
    newest_first: bool = False,
    embedder=None,
) -> int:
    """Compute event vectors in-process from the FTS shadow (user/text/title/summary
    pools), one vector per :data:`CHUNK_CHARS` chunk (long docs get several).

    ``rebuild=False`` only embeds docs with fewer vectors than their content needs
    (anti-join on the chunk count — safe to re-run, and it picks up under-embedded
    long docs, those with fewer vectors than their content needs, as pending).
    ``max_events`` caps how many *docs*
    a single call embeds — the live cohost bounds each pass so a backlog drains
    over cycles without stalling ingest; ``newest_first`` drains the freshest gap
    first, which is what keeps recent-thread *semantic* recall current (the lexical
    arm already covers fresh threads). ``embedder`` is the model the vectors are
    computed with (default: the process embedder) — pass one to index into a
    different embedding space than the process default queries. Returns docs
    embedded. No-op (0) when the store isn't SQLite or the embedder is unavailable.
    """
    if not is_available():
        return 0
    if embedder is None:
        from .embed import default as _default_embedder

        embedder = _default_embedder()
    if not embedder.is_available():
        logger.info("vectors.index_events_local: embed backend unavailable — skipping")
        return 0
    ensure_index()

    # Pending = docs whose stored vector count is short of what their length needs.
    # The SQL mirrors _chunk()'s count — min(MAX_CHUNKS, ceil(len/CHUNK_CHARS)) —
    # over the same concatenated content; if the two formulas diverge the cohost
    # loops on the same docs forever, so change them together.
    missing = ("" if rebuild else
               " HAVING coalesce(v.nv, 0) < min(:mx, "
               "(length(group_concat(f.content, ' ')) + :cc - 1) / :cc)")
    order = "DESC" if newest_first else "ASC"
    limit = " LIMIT :cap" if max_events else ""
    sql = sa_text(
        "SELECT f.event_id AS eid, f.content_type AS ct, group_concat(f.content, ' ') AS content "
        "FROM events_fts f "
        "LEFT JOIN (SELECT event_id, content_type, count(*) AS nv FROM event_vectors "
        "           GROUP BY event_id, content_type) v "
        "  ON v.event_id = f.event_id AND v.content_type = f.content_type "
        "WHERE f.content_type IN ('user', 'text', 'title', 'summary') "
        "AND f.content IS NOT NULL AND f.content != ''"
        f" GROUP BY f.event_id, f.content_type{missing}"
        f" ORDER BY f.event_id {order}" + limit
    )
    params: dict = {} if rebuild else {"mx": MAX_CHUNKS, "cc": CHUNK_CHARS}
    if max_events:
        params["cap"] = int(max_events)
    with get_session() as s:
        pending = [(r.eid, r.ct, r.content) for r in s.execute(sql, params)]

    total = 0
    batch: list[tuple[int, str, list[str]]] = []
    batch_chunks = 0

    def _flush() -> bool:
        nonlocal total, batch, batch_chunks
        if not batch:
            return True
        texts = [t for _, _, chunks in batch for t in chunks]
        vecs = embedder.embed_documents(texts)
        if not vecs:
            logger.warning("vectors.index_events_local: embed returned None — stopping at %d", total)
            return False
        docs, pos = [], 0
        for eid, ct, chunks in batch:
            docs.append((eid, ct, vecs[pos:pos + len(chunks)]))
            pos += len(chunks)
        _write_doc_vectors(docs)
        total += len(docs)
        batch, batch_chunks = [], 0
        return True

    for eid, ct, content in pending:
        chunks = _chunk(content)
        batch.append((eid, ct, chunks))
        batch_chunks += len(chunks)
        if batch_chunks >= batch_size and not _flush():
            return total
    _flush()
    return total


@contextmanager
def _attach_side(conn, db_path):
    """ATTACH ``db_path`` as ``side`` for the block, detaching on exit.

    The connection comes from (and returns to) the engine pool, and an attached
    schema is per-connection state the pool's reset does not clear — so a DETACH
    that fails would hand every later borrower a connection with a stray ``side``
    schema. On a failed DETACH the DBAPI connection is invalidated (discarded
    from the pool) instead."""
    conn.exec_driver_sql("ATTACH DATABASE ? AS side", (str(db_path),))
    try:
        yield conn
    finally:
        try:
            conn.exec_driver_sql("DETACH DATABASE side")
        except Exception:  # pragma: no cover — detach failure is exotic
            conn.invalidate()


# A build file this much older than its last write belongs to a saver that died
# (a live CREATE TABLE ... AS SELECT keeps the mtime moving); sweep it.
_STALE_TMP_AGE_S = 3600


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
    import time
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
    # Build into a per-process temp sidecar and rename over the live one. The
    # rename bounds a crash (a mid-save death must not leave a gutted cache —
    # the embed it protects takes hours to redo); the pid-unique name bounds
    # concurrency (savers run unserialized — a backup racing the watcher's
    # cadence, two operator sessions — and a shared build file lets one saver
    # ATTACH the other's half-built database: a CREATE TABLE collision at best,
    # publishing a half-built sidecar at worst). Each saver builds its own file;
    # every rename that runs publishes a complete snapshot, last one wins.
    for stray in path.parent.glob(path.name + ".tmp*"):
        try:
            if time.time() - stray.stat().st_mtime > _STALE_TMP_AGE_S:
                stray.unlink()
        except OSError:  # racing another saver's sweep of the same stray
            pass
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.unlink(missing_ok=True)
    try:
        with get_engine().connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            with _attach_side(conn, tmp):
                conn.exec_driver_sql(
                    "CREATE TABLE side.event_vectors AS SELECT * FROM event_vectors"
                )
                conn.exec_driver_sql("CREATE TABLE side.vector_meta (space_key TEXT)")
                conn.exec_driver_sql("INSERT INTO side.vector_meta (space_key) VALUES (?)", (sk,))
                n = conn.exec_driver_sql("SELECT count(*) FROM side.event_vectors").scalar()
        # Same durability bar as the truth writers: the sidecar protects an embed
        # that takes hours to redo, so it must be on the platter — not just in the
        # page cache — before the rename publishes it, and the rename itself must
        # survive power loss (dir fsync).
        from .._truth.jsonl_log import _fsync_dir

        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a dead build for the sweep to age out
        raise
    _fsync_dir(Path(truth_dir))
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
        with _attach_side(conn, path):
            have = conn.exec_driver_sql("SELECT space_key FROM side.vector_meta LIMIT 1").scalar()
            if have != want:
                logger.info("vector sidecar space %r != current %r — re-embedding", have, want)
                return 0
            # A pre-chunking sidecar has no ``chunk`` column; its rows restore as
            # chunk 0 (the cohost then tops up long docs' remaining chunks).
            side_cols = {r[1] for r in conn.exec_driver_sql("PRAGMA side.table_info(event_vectors)")}
            chunk_col = "chunk" if "chunk" in side_cols else "0"
            conn.exec_driver_sql(
                "INSERT OR IGNORE INTO event_vectors (event_id, content_type, chunk, dim, vec) "
                f"SELECT event_id, content_type, {chunk_col}, dim, vec FROM side.event_vectors"
            )
            n = conn.exec_driver_sql("SELECT count(*) FROM event_vectors").scalar()
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
    key = (id(eng), tuple(sorted(cts)))
    with get_session() as s:
        token = _validity_token(s)
        cached = _MATRIX_CACHE.get(key)
        if cached is not None and cached[0] == token:
            return cached[1:]
        mat, ids_all, ct_codes_all, ct_names = _ensure_pack(s, (token[1], token[2]))

    # The scope is a row mask over the one shared pack: sims run over the full
    # matrix (the matvec streams mmap pages) and gather down to these rows.
    want = np.asarray(
        [i for i, n in enumerate(ct_names) if n in set(cts)], dtype=np.int16
    )
    scope_rows = np.nonzero(np.isin(ct_codes_all, want))[0]
    ids_arr = ids_all[scope_rows]
    ctypes = [ct_names[c] for c in ct_codes_all[scope_rows]]
    ct_arr = np.asarray(ctypes, dtype=object)
    # Per-document grouping for the KNN's chunk max-pool: rows sharing an
    # (event_id, content_type) are one document. ``doc_inverse`` maps each row to
    # its doc index; ``doc_rep`` gives one representative row per doc (to recover
    # the id/ctype). Precomputed here so the per-query pool is a single
    # ``np.maximum.at`` over the sims, not a python-level group-by.
    ct_codes = {c: i for i, c in enumerate(sorted(set(ctypes)))}
    if len(ids_arr):
        group_key = ids_arr * len(ct_codes) + np.asarray([ct_codes[c] for c in ctypes], dtype=np.int64)
        _, doc_rep, doc_inverse = np.unique(group_key, return_index=True, return_inverse=True)
    else:
        doc_rep = np.empty(0, dtype=np.int64)
        doc_inverse = np.empty(0, dtype=np.int64)
    if key not in _MATRIX_CACHE:
        while len(_MATRIX_CACHE) >= _MATRIX_CACHE_MAX:
            _MATRIX_CACHE.pop(next(iter(_MATRIX_CACHE)))
    _MATRIX_CACHE[key] = (token, ids_arr, ct_arr, mat, doc_inverse, doc_rep, scope_rows)
    return ids_arr, ct_arr, mat, doc_inverse, doc_rep, scope_rows


def _knn(qvec, cts: tuple[str, ...], cand: int, allowed_ids=None) -> list[tuple[int, str, float]]:
    """Brute-force cosine KNN over the cached matrix, **max-pooled per document
    before the top-k cut**: the matrix holds one row per chunk, and a doc's score
    is its best chunk's, so a long message matches on whichever slice is relevant.
    Pooling happens before the candidate cutoff — several strong chunks of one
    long doc must count as ONE candidate, not eat ``cand`` slots that should hold
    distinct documents. ``allowed_ids`` (an int64 ndarray) restricts candidates to
    those event ids *before* the top-k cut, so a scoped search (one thread, a time
    window) ranks within its scope instead of hoping the scope survives a
    corpus-wide top-k."""
    ids, ct_arr, mat, doc_inverse, doc_rep, scope_rows = _load_matrix(cts)
    n = len(ids)
    if n == 0:
        return []
    q = _normalize(qvec)
    # Full-matrix matvec (streams the mmap; same float32 bits as an in-RAM
    # multiply), gathered down to the scope's rows so everything below stays
    # scope-local exactly as before.
    sims = np.asarray(mat @ q, dtype=np.float32)[scope_rows]
    rows = np.arange(n)
    if allowed_ids is not None:
        rows = np.nonzero(np.isin(ids, allowed_ids))[0]
        if len(rows) == 0:
            return []
    # Max-pool chunk sims into per-doc scores (out-of-scope docs stay at -inf).
    doc_scores = np.full(len(doc_rep), -np.inf, dtype=np.float32)
    np.maximum.at(doc_scores, doc_inverse[rows], sims[rows].astype(np.float32))
    live = np.nonzero(doc_scores > -np.inf)[0]
    k = min(cand, len(live))
    if k < len(live):
        part = np.argpartition(-doc_scores[live], k - 1)[:k]
        pool = live[part]
    else:
        pool = live
    order = sorted(pool, key=lambda d: (-float(doc_scores[d]), int(ids[doc_rep[d]])))
    return [(int(ids[doc_rep[d]]), str(ct_arr[doc_rep[d]]), float(doc_scores[d])) for d in order]


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
    thread_id: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    limit: int = 20,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    exclude_content_types: Optional[list[str]] = None,
    source: Optional[list[str]] = None,
    thread_ids: Optional[list[str]] = None,
    agents: str = "exclude",
    embedder=None,
) -> Optional[list[EventHit]]:
    """Embedded semantic search: embed the query, brute-force cosine KNN, hydrate.

    Returns None when this isn't SQLite, the scope has no embedded pool, nothing's
    indexed, or the embed fails — so search degrades to the lexical arm.

    ``embedder`` is the model the query is embedded with (default: the process
    embedder). It must be the one that indexed the vectors — they're only
    comparable within a single embedding space (see ``space_key``).

    ``agents`` mirrors the lexical arm: 'exclude' (default) drops agent-run threads
    (``thread_type='system'``), 'include' keeps them, 'only' keeps nothing else.
    An explicit ``thread_id``/``thread_ids`` scope bypasses it, like the blacklist.
    'only' pre-masks the KNN (agent threads are a sliver of the embedded corpus —
    a corpus-wide top-k would rarely land in them); 'exclude' filters at hydration
    like the blacklist, leaning on the candidate over-fetch.
    """
    if not is_available() or not query or not query.strip():
        return None
    ensure_index()
    cts = _scope_content_types(content_types)
    # Excluded types come out of the KNN scope itself, not just the hydration
    # WHERE: dropping candidates after the top-k would waste slots on hits the
    # filter then discards, shrinking the effective pool.
    if cts and exclude_content_types:
        cts = [c for c in cts if c not in set(exclude_content_types)]
    if not cts:
        return None

    with get_session() as s:
        total = s.execute(sa_text("SELECT count(*) FROM event_vectors")).scalar() or 0
    if not total:
        return None

    try:
        if embedder is None:
            from .embed import default as _default_embedder

            embedder = _default_embedder()
        qvec = embedder.embed_query(query)
    except Exception as e:  # noqa: BLE001
        logger.debug("vectors: query embed failed: %s", e)
        return None
    if not qvec:
        return None

    # A scoped search (one thread / time window / source) pre-masks the KNN to the
    # in-scope event ids, so ranking happens *within* the scope — a corpus-wide
    # top-k could miss the scope entirely. Unscoped searches skip the id query.
    if thread_ids is not None and not thread_ids:
        return []  # an empty id-set scope matches nothing
    selective = (thread_id is not None or thread_ids is not None
                 or since is not None or until is not None or bool(source)
                 or agents == "only")
    allowed_ids = None
    if selective:
        awhere = []
        aparams: dict = {}
        ajoin = "FROM events e"
        if thread_id is not None:
            awhere.append("e.thread_id = :tid")
            aparams["tid"] = thread_id
        if thread_ids is not None:
            awhere.append(_in_clause("e.thread_id", thread_ids, "tids", aparams, negate=False))
        if thread_id is None and thread_ids is None:
            # No explicit thread scope: apply the agents filter to the mask so
            # ranking happens within the allowed pool (essential for 'only').
            if agents == "exclude":
                awhere.append("e.thread_id NOT IN (SELECT id FROM threads WHERE thread_type = 'system')")
            elif agents == "only":
                awhere.append("e.thread_id IN (SELECT id FROM threads WHERE thread_type = 'system')")
        if since:
            awhere.append("e.occurred_at >= :since")
            aparams["since"] = since
        if until:
            awhere.append("e.occurred_at <= :until")
            aparams["until"] = until
        if source:
            ajoin += " JOIN threads t ON t.id = e.thread_id"
            awhere.append(_in_clause("t.source", source, "src", aparams, negate=False))
        # Bulk-fetch the in-scope ids in one buffered round-trip, not row-by-row:
        # a broad time bound puts millions of ids in scope, and fetchone-per-row
        # through the ORM spends seconds on Python overhead the numpy mask doesn't need.
        with get_session() as s:
            rows = s.execute(
                sa_text("SELECT e.id " + ajoin + " WHERE " + " AND ".join(awhere)), aparams,
            ).fetchall()
        if not rows:
            return []
        allowed_ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
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
    elif thread_ids is not None:
        # A resolved id-set scope (e.g. a topic's member threads) — deliberate,
        # so it bypasses the blacklist like a single explicit thread_id.
        where.append(_in_clause("f.thread_id", thread_ids, "tids", params, negate=False))
    else:
        # Honor the per-thread search blacklist; an explicit thread scope bypasses it.
        where.append("f.thread_id NOT IN (SELECT id FROM threads WHERE exclude_from_search)")
        # Agent-run threads ride the same pattern (see the docstring).
        if agents == "exclude":
            where.append("f.thread_id NOT IN (SELECT id FROM threads WHERE thread_type = 'system')")
        elif agents == "only":
            where.append("f.thread_id IN (SELECT id FROM threads WHERE thread_type = 'system')")
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

    hydrated: list[tuple[float, EventHit]] = []
    for r in rows:
        sim = sim_by.get((r["event_id"], r["content_type"]))
        if sim is None:
            continue
        content = r["full_content"] or ""
        hit = build_event_hit(
            event_id=r["event_id"], thread_id=r["thread_id"], event_type=r["event_type"],
            content_type=r["content_type"], snippet=content[:300], full_content=content,
            occurred_at=r["occurred_at"],
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
