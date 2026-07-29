"""SQLite embedded semantic search.

One in-DB ``event_vectors`` BLOB table holding 768-d nomic vectors, searched by an
in-process brute-force cosine KNN (numpy). No server, no native extension, no HNSW:
the vectors live in the archive SQLite DB beside the data. Vectors are float32 and
unit-normalized, so cosine = dot product; a query is a single BLAS matvec (~ms).

The KNN matrix is **mmap'd, not resident**: the corpus is packed into token-named
``.npy`` files (``<home>/vector-pack/``, derived + disposable) and
``np.load(mmap_mode='r')`` serves every content-type scope from the one pack via
row masks — RAM cost is page cache the OS can reclaim, not per-process RSS, so
the ceiling scales with disk instead of memory. Same float32 bits, same scores.
The pack is a **base + delta**: the large base is mmap'd from disk and the vectors
written since it was packed ride along as a small in-RAM tail, so a single new
vector costs a cheap delta read, not a full base rebuild (see :class:`_SplitMatrix`).

Long documents are **chunked**: a doc is embedded as one vector per
:data:`CHUNK_CHARS`-char slice (up to :data:`MAX_CHUNKS`), keyed
``(event_id, content_type, chunk)``, and the KNN max-pools per document — the
best-matching chunk speaks for the doc. Without this, everything past the embed
cap of a long message is semantically invisible (the embed provider truncates),
and the vocab-mismatch queries the vector arm exists for are exactly the ones
that can't fall back to keywords.

Populated from the ``events_fts`` shadow's ``user`` / ``text`` / ``title``
pools via the ``[embeddings]`` provider (:mod:`.embed`). Cached durably in a ``vectors.sqlite``
sidecar so the hours-long embed survives ``rm index.db && reindex``. Degrades to
lexical-only when the extra isn't installed or nothing's indexed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import numpy as np
from sqlalchemy import text as sa_text

from .._store import get_engine, get_session
from . import _probe
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


def _chunk_count(content: str) -> int:
    """How many vectors ``content`` needs — ``len(_chunk(content))`` without building
    the slices. This is the formula the drain's anti-join duplicates in SQL; keeping
    the Python side to one named definition is half of keeping the two identical."""
    return max(1, min(MAX_CHUNKS, (len(content) + CHUNK_CHARS - 1) // CHUNK_CHARS))


# Length-sorted encode window. The embedder pads every chunk in an encode batch to
# the longest one in it, so a batch that mixes a 30-char user turn with a 2048-char
# slice spends most of its compute on padding. Draining docs in the store's natural
# (event_id) order guarantees that mix — measured pad waste ~4/5 of the encode, and
# encode is ~19/20 of the whole embed. Length-sorting so each batch is
# length-homogeneous cuts that waste to near nothing and roughly halves the embed,
# the longest phase of a cold load. The sort is *windowed*, not global: the drain
# still walks the pending set newest-window-first — recency is what keeps
# recent-thread semantic recall current, and a large single pass writes the newest
# docs durably before the oldest — and only the order *within* a window of this many
# docs is length-permuted. Big enough that a window holds thousands of chunks (where
# the pad win saturates) and a whole cohost pass drains as one window; small enough
# to keep the recency grain fine.
_SORT_WINDOW = 2048


def _length_batched(pending: list, window: int) -> list:
    """Reorder ``pending`` rows (``(event_id, content_type, content)``) so each
    ``batch_size`` slice the drain encodes together is length-homogeneous — near-zero
    embed pad waste — while preserving newest-window-first recency at ``window``
    granularity. ``window <= 0`` leaves the order untouched. Stable within a window,
    so equal-length docs keep the incoming (recency) order."""
    if window <= 0 or len(pending) <= 1:
        return pending
    out: list = []
    for start in range(0, len(pending), window):
        block = pending[start:start + window]
        block.sort(key=lambda r: len(r[2]))
        out.extend(block)
    return out

# Embedded content-type pools: user → default pool; text → scoped assistant pool;
# title → the thread-meta doc (thread-level aboutness).
_USER_CONTENT_TYPES = ("user",)
_ASSISTANT_CONTENT_TYPES = ("text",)
_META_CONTENT_TYPES = ("title",)

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

# Serve the matrix without ever paying a pack rebuild on the request thread. A search
# returns the cached matrix immediately (stale is fine — the lexical arm covers the
# newest, not-yet-repacked vectors), probes the store's validity token at most once
# per cooldown, and does the refresh — usually just the small delta read, occasionally
# a full base rebuild — only in a single-flight background thread. Continuous ingest
# moves the token every few minutes; the old inline rebuild put that multi-second cost
# on whichever query raced the new token, and, unguarded, let a burst of concurrent
# queries all rebuild the same ~GB pack at once. A writer that can't wait out the
# cooldown calls ``reset_matrix_cache`` for immediate local effect — a stale window
# only ever costs a ranking slot.
_MATRIX_REFRESH_COOLDOWN_S = 60.0
_MATRIX_REFRESH_LOCK = threading.Lock()
_MATRIX_REFRESHING: set = set()
_matrix_checked_at: dict = {}  # {cache key: monotonic ts of the last staleness probe}

# Pack files for a superseded token are swept once they age out — a mapped-in
# reader elsewhere may still be serving queries off them (its unlinked inode
# stays valid; the age is grace, not correctness).
_PACK_STALE_AGE_S = 3600

# The pack is a base + delta: a large on-disk base pack (mmap) plus the vectors
# written since it was built, held in RAM. Continuous ingest moves the store token
# every few minutes, but a single new vector no longer invalidates the ~GB base — it
# lands in the delta, a cheap read. A fresh base (the full scan + np.vstack + write) is
# packed only when the delta grows past this many rows, so the expensive rebuild
# happens once per this-many new vectors, not once per new vector.
_DELTA_MAX_ROWS = 20_000

#: The positional arrays a complete base pack is made of. Named once because two
#: places must agree on the set: the writer, and the reuse check that decides a pack
#: is loadable. A pack missing any of them is not a broken pack to repair — it is an
#: older one, and since a pack is derived and disposable the reuse check simply
#: passes it over and the next build writes a complete one.
_PACK_ARRAYS = ("mat", "ids", "cts", "ts")


class _SplitMatrix:
    """The KNN matrix as base (on-disk, mmap'd — reclaimable page cache, shared across
    processes) + delta (the small in-RAM tail of vectors written since the base was
    packed). ``m @ q`` runs the matvec over both halves and concatenates; ``m[rows]``
    gathers across the split; ``m.shape`` reports the combined size. Row order is
    base-rows then delta-rows — the KNN and the corpus graph both group by document
    key, never by row position, so the split is invisible to them."""

    __slots__ = ("base", "delta")

    def __init__(self, base, delta) -> None:
        self.base = base
        self.delta = delta

    @property
    def shape(self) -> tuple:
        return (self.base.shape[0] + self.delta.shape[0], self.base.shape[1])

    def __len__(self) -> int:
        return self.base.shape[0] + self.delta.shape[0]

    def __matmul__(self, q):
        # base @ q streams the mmap; delta @ q is a small in-RAM multiply.
        return np.concatenate([np.asarray(self.base @ q), self.delta @ q])

    def __getitem__(self, idx):
        idx = np.asarray(idx)
        b = self.base.shape[0]
        below = idx < b
        if below.all():
            return np.asarray(self.base[idx])
        out = np.empty((idx.shape[0], self.base.shape[1]), dtype=self.delta.dtype)
        out[below] = self.base[idx[below]]
        above = ~below
        out[above] = self.delta[idx[above] - b]
        return out


def _pack_dir() -> Optional[Path]:
    """``<home>/vector-pack/`` beside the index — derived, disposable, rebuilt
    whenever the store token moves. None for a non-file DSN (the in-RAM path)."""
    db = get_engine().url.database
    return Path(db).parent / "vector-pack" if db else None


def _occurred_array(values: list) -> np.ndarray:
    """Pack ``occurred_at`` strings into a byte array a time scope can compare against.

    Bytes, not epoch integers, and that is the correctness argument rather than a
    convenience: the query this replaces compared the same column as SQLite TEXT,
    which is a plain bytewise comparison, so comparing the same bytes in numpy gives
    the same answer for every value the column can hold — including any that a
    timestamp parser would reject or silently reinterpret. Width is taken from the
    data (never fixed), because a value truncated to fit would compare as a prefix
    of itself and quietly move rows across a scope boundary.

    A row with no timestamp packs to empty, which sorts below every real value.
    :func:`_time_rows` excludes those explicitly rather than letting the ordering
    decide — an undated row is not in a time window, and empty bytes would otherwise
    read as "before everything", i.e. inside every ``until``."""
    return np.asarray([("" if v is None else str(v)).encode() for v in values], dtype="S")


def _corpus_arrays(s) -> tuple:
    """The full embedded corpus as arrays: (mat, ids, ct_codes, occurred, ct_names).
    Deterministic order (the PK) so two rival pack builders of the same token
    write byte-identical files.

    LEFT JOIN, so the row space stays exactly ``event_vectors``: an inner join would
    silently drop a vector whose event is missing, and the pack's arrays are all
    positional — one short array would misalign every id from that row on."""
    ids: list[int] = []
    cts: list[str] = []
    occ: list = []
    vecs: list[np.ndarray] = []
    result = s.execute(sa_text(
        "SELECT v.event_id, v.content_type, v.vec, e.occurred_at FROM event_vectors v "
        "LEFT JOIN events e ON e.id = v.event_id "
        "ORDER BY v.event_id, v.content_type, v.chunk"
    ))
    for r in result:
        ids.append(int(r[0]))
        cts.append(str(r[1]))
        vecs.append(np.frombuffer(r[2], dtype=np.float32))
        occ.append(r[3])
    ct_names = sorted(set(cts))
    codes = {c: i for i, c in enumerate(ct_names)}
    mat = np.vstack(vecs) if vecs else np.empty((0, _DIM), dtype=np.float32)
    ids_arr = np.asarray(ids, dtype=np.int64)
    ct_codes = np.asarray([codes[c] for c in cts], dtype=np.int16)
    return mat, ids_arr, ct_codes, _occurred_array(occ), ct_names


def _delta_arrays(s, watermark: int) -> tuple:
    """Vectors written since the base watermark (rowid > watermark), read live into
    RAM: (mat, ids, occurred, ct_names_list). Small by construction — the delta is
    folded into a fresh base once it passes :data:`_DELTA_MAX_ROWS` — so this is a
    cheap tail read, never the ~GB base scan. Ordered by rowid (the KNN groups by doc
    key, not row position, so order is for determinism, not correctness)."""
    ids: list[int] = []
    cts: list[str] = []
    occ: list = []
    vecs: list[np.ndarray] = []
    for r in s.execute(sa_text(
        "SELECT v.event_id, v.content_type, v.vec, e.occurred_at FROM event_vectors v "
        "LEFT JOIN events e ON e.id = v.event_id "
        "WHERE v.rowid > :wm ORDER BY v.rowid"
    ), {"wm": int(watermark)}):
        ids.append(int(r[0]))
        cts.append(str(r[1]))
        vecs.append(np.frombuffer(r[2], dtype=np.float32))
        occ.append(r[3])
    mat = np.vstack(vecs) if vecs else np.empty((0, _DIM), dtype=np.float32)
    return mat, np.asarray(ids, dtype=np.int64), _occurred_array(occ), cts


def _sweep_packs(d: Path, keep_tag: str) -> None:
    """Sweep aged-out pack files of superseded tokens, keeping the freshly written
    ``keep_tag``. A reader elsewhere may still be mapped onto an unlinked inode, so the
    age (:data:`_PACK_STALE_AGE_S`) is grace, not correctness."""
    for stray in d.iterdir():
        if f"-{keep_tag}." in stray.name:
            continue
        try:
            if time.time() - stray.stat().st_mtime > _PACK_STALE_AGE_S:
                stray.unlink()
        except OSError:  # racing another sweeper
            pass


def _mmap_base(d: Path, tag: str) -> tuple:
    """mmap a base pack's files back in: (mat[mmap], ids, ct_codes, occurred, ct_names).

    Only the vectors are mapped. The index arrays are small (tens of MB against the
    matrix's ~GB) and every query touches all of them, so paging them lazily would buy
    nothing and cost a fault per scope."""
    mat = np.load(d / f"mat-{tag}.npy", mmap_mode="r")
    ids_arr = np.load(d / f"ids-{tag}.npy")
    ct_codes = np.load(d / f"cts-{tag}.npy")
    occurred = np.load(d / f"ts-{tag}.npy")
    ct_names = json.loads((d / f"meta-{tag}.json").read_text())["ct_names"]
    return mat, ids_arr, ct_codes, occurred, ct_names


def _write_base(s, d: Path, base_token: tuple[int, int]) -> tuple:
    """Build and persist the full-corpus base pack at ``base_token`` — the ~GB blob
    read + np.vstack + write — then mmap it back. Files are token-named and published
    via os.replace, so a reader never mixes arrays from two builds and rival builders
    of the same token write byte-identical content (deterministic PK order); whichever
    replace lands last changes nothing."""
    mat, ids_arr, ct_codes, occurred, ct_names = _corpus_arrays(s)
    tag = f"{base_token[0]}-{base_token[1]}"
    d.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    paths = {k: d / f"{k}-{tag}.npy" for k in _PACK_ARRAYS}
    for k, arr in (("mat", mat), ("ids", ids_arr), ("cts", ct_codes), ("ts", occurred)):
        tmp = d / f"{k}-{tag}.npy.tmp.{pid}"
        with open(tmp, "wb") as f:  # a handle: np.save must not append '.npy'
            np.save(f, arr)
        os.replace(tmp, paths[k])
    meta_p = d / f"meta-{tag}.json"
    tmp = d / f"{meta_p.name}.tmp.{pid}"
    tmp.write_text(json.dumps({"ct_names": ct_names, "rows": len(ids_arr)}))
    os.replace(tmp, meta_p)
    _sweep_packs(d, tag)
    return _mmap_base(d, tag)


def _reusable_base(s, d: Path, cur_count: int) -> Optional[tuple[int, int]]:
    """The freshest on-disk base still usable as a clean prefix of the live store:
    every base row present (no delete at or below its watermark) and the new-rows delta
    within :data:`_DELTA_MAX_ROWS`. Returns its ``(base_count, watermark)`` or None to
    force a fresh base. Freshest = largest watermark = smallest delta. The clean-prefix
    count check catches deletes below the watermark (a re-embed); an
    in-place upsert that changes a base row without moving count or rowid is invalidated
    at its source (:func:`index_vectors` drops the pack metas)."""
    tags: list[tuple[int, int]] = []
    for meta_p in d.glob("meta-*.json"):
        try:
            bc_s, bw_s = meta_p.stem[len("meta-"):].split("-")
            bc, bw = int(bc_s), int(bw_s)
        except ValueError:  # a temp/foreign name that slipped the glob
            continue
        if all((d / f"{k}-{bc}-{bw}.npy").exists() for k in _PACK_ARRAYS):
            tags.append((bw, bc))
    for bw, bc in sorted(tags, reverse=True):  # largest watermark (smallest delta) first
        # Count only the tail above the watermark (bounded small for the freshest base),
        # not the whole base range. below == bc iff no base row was deleted (clean prefix).
        delta = s.execute(
            sa_text("SELECT count(*) FROM event_vectors WHERE rowid > :wm"), {"wm": bw}
        ).scalar() or 0
        if cur_count - delta == bc and delta <= _DELTA_MAX_ROWS:
            return (bc, bw)
    return None


def _ensure_pack(s, store_token: tuple[int, int]) -> tuple:
    """The full-corpus KNN matrix for ``store_token`` as base(mmap) + in-RAM delta.
    Reuses an on-disk base that is still a clean prefix of the store and layers the
    vectors written since as a small delta — so continuous ingest costs a delta read,
    not an 814MB rebuild — and packs a fresh base only when none is reusable. Falls back
    to full in-RAM arrays for a non-file DSN. Returns (mat, ids, ct_codes, ct_names);
    ``mat`` is a bare mmap/ndarray when the delta is empty, else a :class:`_SplitMatrix`.

    Runs entirely in the caller's session ``s`` — the base scan, the delta read, and the
    token that named them share one DB snapshot, so a concurrent insert can never land a
    row in both halves (a duplicate) or in neither (a gap)."""
    cur_count, cur_max_rowid = store_token
    d = _pack_dir()
    if d is None:
        return _corpus_arrays(s)  # non-file DSN: no disk base, full in-RAM arrays
    base = _reusable_base(s, d, cur_count)
    if base is None:
        base_mat, base_ids, base_codes, base_ts, base_names = _write_base(s, d, store_token)
        watermark = cur_max_rowid  # fresh base spans the whole snapshot → empty delta
    else:
        bc, bw = base
        try:
            base_mat, base_ids, base_codes, base_ts, base_names = _mmap_base(d, f"{bc}-{bw}")
        except FileNotFoundError:  # swept between selection and load — pack fresh
            base_mat, base_ids, base_codes, base_ts, base_names = _write_base(s, d, store_token)
            watermark = cur_max_rowid
        else:
            watermark = bw
    delta_mat, delta_ids, delta_ts, delta_cts = _delta_arrays(s, watermark)
    if delta_mat.shape[0] == 0:
        return base_mat, base_ids, base_codes, base_ts, base_names
    # Unify the content-type coding across base and delta (a delta may carry a type the
    # base lacked, or vice versa), then stitch the two row spaces into one.
    names = sorted(set(base_names) | set(delta_cts))
    uidx = {c: i for i, c in enumerate(names)}
    if names == base_names:
        base_codes_u = base_codes
    else:
        remap = np.asarray([uidx[n] for n in base_names], dtype=np.int16)
        base_codes_u = remap[base_codes]
    delta_codes_u = np.asarray([uidx[c] for c in delta_cts], dtype=np.int16)
    mat = _SplitMatrix(base_mat, delta_mat)
    ids_all = np.concatenate([base_ids, delta_ids])
    ct_codes_all = np.concatenate([base_codes_u, delta_codes_u])
    # Concatenating byte arrays of different widths promotes to the wider one rather
    # than truncating to the narrower, so a delta whose timestamps are shaped unlike
    # the base's stitches in without any value changing.
    ts_all = np.concatenate([base_ts, delta_ts])
    return mat, ids_all, ct_codes_all, ts_all, names


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
        before = s.execute(sa_text("SELECT count(*) FROM event_vectors")).scalar() or 0
        s.execute(sa_text(
            "INSERT INTO event_vectors (event_id, content_type, chunk, dim, vec) "
            "VALUES (:eid, :ct, :chunk, :dim, :vec) "
            "ON CONFLICT (event_id, content_type, chunk) "
            "DO UPDATE SET dim = excluded.dim, vec = excluded.vec"
        ), rows)
        s.commit()
        after = s.execute(sa_text("SELECT count(*) FROM event_vectors")).scalar() or 0
    _bump_version()
    # An in-place upsert (an existing key re-written) changes a row's content without
    # moving the store token — a base pack covering that row would be stale yet still
    # read as a clean prefix, so drop the pack metas to force a fresh base. A pure insert
    # (every row new) only extends the prefix; the delta picks it up, so the base stays
    # reusable. ``after - before`` is the count of genuinely-new rows.
    if len(rows) - (after - before) > 0:
        d = _pack_dir()
        if d is not None:
            for stray in d.glob("meta-*.json"):
                stray.unlink(missing_ok=True)
        # The corpus graph is built from these same vectors and named by the same
        # token, so it is stale for the same reason and invisible for the same
        # reason. Dropped here rather than aged out: it is served across processes,
        # where nothing else can know these rows moved.
        from . import graph_cache

        graph_cache.drop()
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
    phase=None,
    sort_window: int = _SORT_WINDOW,
) -> int:
    """Compute event vectors in-process from the FTS shadow (user/text/title
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

    ``phase`` is a :class:`~thread_archive._ops.load_runs.Phase` this drain reports
    into: the pending total up front (so the wait carries an ETA instead of being
    open-ended), a tick per doc, and the ``select`` / ``encode`` / ``write`` split.
    This is the longest phase of a cold load, and that split is what says *which*
    of the three the hours went to. Defaults to a null phase, so an untracked call
    runs the identical path.

    ``sort_window`` length-orders the docs *within* each window of that many before
    batching (see :data:`_SORT_WINDOW`): the embedder pads every text in an encode
    batch to the longest, so a length-homogeneous batch roughly halves the encode —
    the dominant cost — while the windowing keeps the drain newest-window-first.
    ``0`` restores the store's natural order.
    """
    if not is_available():
        return 0
    if phase is None:
        from .._ops.load_runs import NullPhase

        phase = NullPhase()
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
        "WHERE f.content_type IN ('user', 'text', 'title') "
        "AND f.content IS NOT NULL AND f.content != ''"
        f" GROUP BY f.event_id, f.content_type{missing}"
        f" ORDER BY f.event_id {order}" + limit
    )
    params: dict = {} if rebuild else {"mx": MAX_CHUNKS, "cc": CHUNK_CHARS}
    if max_events:
        params["cap"] = int(max_events)
    with phase.timed("select"), get_session() as s:
        pending = [(r.eid, r.ct, r.content) for r in s.execute(sql, params)]
    phase.total = len(pending)
    phase.count("chunks_pending", sum(_chunk_count(c) for _, _, c in pending))
    # Length-sort within recency windows so each encode batch is length-homogeneous
    # (the embedder pads to the batch's longest text): near-zero pad waste, newest
    # window still drained and durably written first.
    pending = _length_batched(pending, sort_window)
    # The model loads lazily inside the first embed call, so a cold load — tens of
    # seconds — would land inside the first ``encode`` and inflate it, which on a
    # short pass is most of the reported encode. Load it here under its own
    # sub-timing so the split stays honest. Duck-typed: an embedder stand-in need
    # only implement ``embed_documents``, and ``warm`` is idempotent.
    if pending:
        warm = getattr(embedder, "warm", None)
        is_loaded = getattr(embedder, "is_loaded", None)
        if warm is not None and not (is_loaded and is_loaded()):
            with phase.timed("model_load"):
                warm()

    total = 0
    batch: list[tuple[int, str, list[str]]] = []
    batch_chunks = 0

    def _flush() -> bool:
        nonlocal total, batch, batch_chunks
        if not batch:
            return True
        texts = [t for _, _, chunks in batch for t in chunks]
        with phase.timed("encode"):
            vecs = embedder.embed_documents(texts)
        if not vecs:
            logger.warning("vectors.index_events_local: embed returned None — stopping at %d", total)
            return False
        docs, pos = [], 0
        for eid, ct, chunks in batch:
            docs.append((eid, ct, vecs[pos:pos + len(chunks)]))
            pos += len(chunks)
        with phase.timed("write"):
            _write_doc_vectors(docs)
        total += len(docs)
        phase.advance(len(docs), work=len(texts))
        phase.count("chunks", len(texts))
        batch, batch_chunks = [], 0
        return True

    try:
        for eid, ct, content in pending:
            chunks = _chunk(content)
            batch.append((eid, ct, chunks))
            batch_chunks += len(chunks)
            if batch_chunks >= batch_size and not _flush():
                return total
        _flush()
        return total
    finally:
        # A drain leaves the torch allocator holding its peak batch, and on a
        # unified-memory box that peak is dirty anonymous memory the host can only
        # relieve by swapping. Released here rather than per ``_flush`` so the drain
        # pays one re-acquire instead of one per batch, and only when something was
        # actually embedded — the cohost's idle passes must stay free.
        if total:
            from .embed import release_accelerator_cache

            release_accelerator_cache()


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


def _matrix_key(cts: tuple[str, ...]) -> tuple:
    """Cache key for a content-type scope — canonicalized (sorted) so equivalent
    scopes in different orders share one entry."""
    return (id(get_engine()), tuple(sorted(cts)))


def _build_matrix_entry(cts: tuple[str, ...]) -> tuple:
    """Build the processed cache entry for ``cts``: assemble the matrix (base mmap +
    in-RAM delta, packing a fresh base only when none is reusable) and precompute the
    scope's row mask and per-doc grouping. A pure builder that touches no shared cache,
    so it is safe on the request thread (cold start) or a background refresh. The token,
    the base scan, and the delta read share one session snapshot, so the returned entry
    is self-consistent (no row in both halves, none in neither). Returns
    ``(token, ids, ctypes, mat, doc_inverse, doc_rep, scope_rows, occurred)``."""
    with get_session() as s:
        token = _validity_token(s)
        mat, ids_all, ct_codes_all, ts_all, ct_names = _ensure_pack(s, (token[1], token[2]))
    # The scope is a row mask over the one shared pack: sims run over the full
    # matrix (the matvec streams mmap pages) and gather down to these rows.
    want = np.asarray(
        [i for i, n in enumerate(ct_names) if n in set(cts)], dtype=np.int16
    )
    scope_rows = np.nonzero(np.isin(ct_codes_all, want))[0]
    ids_arr = ids_all[scope_rows]
    ts_arr = ts_all[scope_rows]
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
    return (token, ids_arr, ct_arr, mat, doc_inverse, doc_rep, scope_rows, ts_arr)


def _store_matrix_entry(key: tuple, entry: tuple) -> None:
    """Cache ``entry`` under ``key``, evicting the oldest scope only when the key is
    new — a refresh of an existing scope replaces in place, never evicting a rival."""
    if key not in _MATRIX_CACHE:
        while len(_MATRIX_CACHE) >= _MATRIX_CACHE_MAX:
            _MATRIX_CACHE.pop(next(iter(_MATRIX_CACHE)))
    _MATRIX_CACHE[key] = entry


def _refresh_matrix(key: tuple, cts: tuple[str, ...]) -> None:
    """Rebuild ``key``'s entry if the store's validity token has moved. Synchronous —
    the request path drives the async wrapper; the warm pass and tests call this."""
    cached = _MATRIX_CACHE.get(key)
    with get_session() as s:
        token = _validity_token(s)
    if cached is not None and cached[0] == token:
        return  # still fresh — nothing to rebuild
    _store_matrix_entry(key, _build_matrix_entry(cts))


def _refresh_matrix_async(key: tuple, cts: tuple[str, ...]) -> None:
    """Single-flight background refresh: at most one rebuild per key is in flight, so
    a burst of queries racing a moved token spawns one pack rebuild, not N."""
    with _MATRIX_REFRESH_LOCK:
        if key in _MATRIX_REFRESHING:
            return
        _MATRIX_REFRESHING.add(key)

    def _run() -> None:
        try:
            _refresh_matrix(key, cts)
        except Exception:  # noqa: BLE001 — a background refresh must never raise
            logger.exception("vectors: matrix background refresh failed")
        finally:
            with _MATRIX_REFRESH_LOCK:
                _MATRIX_REFRESHING.discard(key)

    threading.Thread(target=_run, name="matrix-refresh", daemon=True).start()


def is_refreshing() -> bool:
    """Whether a background matrix rebuild is in flight in this process — read by
    the contention sample, since a rebuild streams the whole pack off disk and
    competes with any search running beside it."""
    return bool(_MATRIX_REFRESHING)


def _load_matrix(cts: tuple[str, ...]):
    """The processed matrix for ``cts``, served without ever paying a pack rebuild on
    the request thread. A cached entry returns immediately — stale is acceptable, the
    lexical arm covers the freshest vectors — while staleness is probed at most once
    per :data:`_MATRIX_REFRESH_COOLDOWN_S` and any rebuild runs in a single-flight
    background thread. Only a cold cache (the process's first query for this scope,
    before the warm pass primes it) builds inline."""
    key = _matrix_key(cts)
    cached = _MATRIX_CACHE.get(key)
    if cached is not None:
        now = time.monotonic()
        if now - _matrix_checked_at.get(key, 0.0) >= _MATRIX_REFRESH_COOLDOWN_S:
            _matrix_checked_at[key] = now
            _refresh_matrix_async(key, tuple(sorted(cts)))
        return cached[1:]
    # The inline build — the one path that reads the whole pack on the request
    # thread. Flagged, not just timed: it is the difference between a search that
    # was slow and a search that was slow *because it was the first one*.
    _probe.flag("matrix_built")
    entry = _build_matrix_entry(cts)
    _store_matrix_entry(key, entry)
    _matrix_checked_at[key] = time.monotonic()
    return entry[1:]


def reset_matrix_cache() -> None:
    """Drop the process-local matrix cache so the next search rebuilds from the live
    store. For where a stale matrix would be *wrong*, not merely dated — reindex
    (dead rows must not be served)."""
    _MATRIX_CACHE.clear()
    _matrix_checked_at.clear()


def _time_rows(ts_arr, since: Optional[str], until: Optional[str]):
    """Row positions inside a time window, straight off the pack's timestamps.

    The scope this serves used to arrive as ``allowed_ids`` from a query over
    ``events``, and that query was the single most expensive thing a time-scoped
    search did — not because it was badly planned but because of what it had to
    materialize: ``since='180d'`` selects 3.6M event ids to mask a pack holding
    275k vectors, so 93% of the ids fetched name rows the matrix does not contain.
    The pack already knows every row's date; the window is a comparison over an
    array it has in hand, and it costs under a millisecond regardless of how wide
    the window is.

    Undated rows are excluded rather than ordered. They pack to empty bytes, which
    sorts below every real timestamp — so an ``until`` bound would otherwise sweep
    them all in on the grounds of being "before" it."""
    ok = ts_arr != b""
    if since:
        ok &= ts_arr >= since.encode()
    if until:
        ok &= ts_arr <= until.encode()
    return np.nonzero(ok)[0]


def _knn(qvec, cts: tuple[str, ...], cand: int, allowed_ids=None,
         since: Optional[str] = None, until: Optional[str] = None) -> list[tuple[int, str, float]]:
    """Brute-force cosine KNN over the cached matrix, **max-pooled per document
    before the top-k cut**: the matrix holds one row per chunk, and a doc's score
    is its best chunk's, so a long message matches on whichever slice is relevant.
    Pooling happens before the candidate cutoff — several strong chunks of one
    long doc must count as ONE candidate, not eat ``cand`` slots that should hold
    distinct documents. ``allowed_ids`` (an int64 ndarray) restricts candidates to
    those event ids *before* the top-k cut, so a scoped search (one thread, a time
    window) ranks within its scope instead of hoping the scope survives a
    corpus-wide top-k.

    ``since``/``until`` express a *purely* temporal scope, which the pack answers
    itself (see :func:`_time_rows`) with no id list at all. They compose with
    ``allowed_ids`` by intersection, so a caller that has both narrows by both."""
    # Split the matrix load off the arithmetic: serving a warm cached matrix is
    # free, while a cold one builds the pack inline (see :func:`_load_matrix`) and
    # reads the whole corpus off disk. Charging both to one number makes the
    # expensive case indistinguishable from a slow matvec.
    _t = time.perf_counter()
    ids, ct_arr, mat, doc_inverse, doc_rep, scope_rows, ts_arr = _load_matrix(cts)
    _probe.record("matrix_ms", _t)
    n = len(ids)
    if n == 0:
        return []
    _t = time.perf_counter()
    try:
        q = _normalize(qvec)
        # Full-matrix matvec (streams the mmap; same float32 bits as an in-RAM
        # multiply), gathered down to the scope's rows so everything below stays
        # scope-local exactly as before.
        sims = np.asarray(mat @ q, dtype=np.float32)[scope_rows]
        rows = np.arange(n)
        if since or until:
            rows = _time_rows(ts_arr, since, until)
            if len(rows) == 0:
                return []
        if allowed_ids is not None:
            keep = np.isin(ids[rows], allowed_ids)
            rows = rows[keep]
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
    finally:
        # In a finally so the empty-scope early return is charged too — the matvec
        # ran before it, and an unmeasured exit would read as a free search.
        _probe.record("knn_ms", _t)


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
    path: Optional[str] = None,
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

    # Non-emptiness only — an O(1) existence probe, not a full count(*) scan of the
    # covering index on every query (the actual pool sizing happens in the KNN).
    with get_session() as s:
        has_vectors = s.execute(sa_text("SELECT 1 FROM event_vectors LIMIT 1")).scalar()
    if not has_vectors:
        return None

    _t = time.perf_counter()
    try:
        if embedder is None:
            from .embed import default as _default_embedder

            embedder = _default_embedder()
        qvec = embedder.embed_query(query)
    except Exception as e:  # noqa: BLE001
        logger.debug("vectors: query embed failed: %s", e)
        return None
    finally:
        # In a finally so a failed embed still reports the time it burned — a
        # model that takes tens of seconds and then raises is the expensive case.
        _probe.record("embed_ms", _t)
    if not qvec:
        return None

    # A scoped search pre-masks the KNN so ranking happens *within* the scope — a
    # corpus-wide top-k could miss the scope entirely. What differs between scopes is
    # where the mask comes from: a thread, source or path scope needs facts only the
    # database holds and pays an id query for them; a time bound rides the pack.
    if thread_ids is not None and not thread_ids:
        return []  # an empty id-set scope matches nothing
    # A time bound is *not* in this list, and that is the point: the pack carries every
    # row's date, so a window is a comparison the KNN makes itself. Only the scopes
    # that need facts the pack does not hold — which thread, which source, which code
    # path — still cost an id query, and a search scoped by time alone now costs none.
    selective = (thread_id is not None or thread_ids is not None or bool(source)
                 or bool(path) or agents == "only")
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
        # ``agents='only'`` has to ride the mask: agent-run threads are a small
        # minority of the corpus, so a corpus-wide top-k would be almost entirely
        # rows the filter then throws away and the arm would return nothing.
        #
        # ``'exclude'`` is deliberately absent, and the asymmetry is the point. It
        # removes a minority the other way — most of the corpus survives it — so
        # ranking inside the mask and ranking outside it agree on the head, while
        # the clause itself is what makes this query cost seconds: a thread_type
        # lookup per row turns an index-only range scan over ``idx_events_occurred``
        # into one table probe per matched event, measured at 9.2s against 0.9s for a
        # six-month window. Hydration below re-applies the same filter, and that is
        # already the *only* thing keeping agent threads out of an unscoped search —
        # that path builds no mask at all. Leaving it out here makes a time-scoped
        # search cost what an unscoped one costs, and filter where it filters.
        if thread_id is None and thread_ids is None and agents == "only":
            awhere.append("e.thread_id IN (SELECT id FROM threads WHERE thread_type = 'system')")
        # The time bound is deliberately not repeated here. The KNN applies it to
        # every row it considers, so adding it would only narrow a list the KNN is
        # about to narrow anyway — and narrowing it *here* is what costs seconds,
        # since this is the query that has to walk ``events`` to do it.
        if source:
            ajoin += " JOIN threads t ON t.id = e.thread_id"
            awhere.append(_in_clause("t.source", source, "src", aparams, negate=False))
        if path:
            # The same code-axis scope the lexical arm applies, so both arms search
            # the same set of conversations rather than one of them ignoring it.
            from .code import path_scope_sql

            awhere.append(path_scope_sql(path, aparams, column="e.thread_id"))
        # Bulk-fetch the in-scope ids in one buffered round-trip, not row-by-row:
        # a broad time bound puts millions of ids in scope, and fetchone-per-row
        # through the ORM spends seconds on Python overhead the numpy mask doesn't need.
        _t = time.perf_counter()
        with get_session() as s:
            rows = s.execute(
                sa_text("SELECT e.id " + ajoin + " WHERE " + " AND ".join(awhere)), aparams,
            ).fetchall()
        _probe.record("scope_ms", _t)
        if not rows:
            return []
        allowed_ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    cand = max(limit * 3, 100)
    candidates = _knn(qvec, tuple(cts), cand, allowed_ids=allowed_ids, since=since, until=until)
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
    # Times come off the shadow row, which carries its own ``occurred_at`` kept in
    # step with ``events`` by the sync triggers and the rebuild — the same column
    # the lexical arm reads, so both arms date a hit the same way. Reaching into
    # ``events`` for it instead would mean one scattered rowid lookup per candidate
    # into a multi-million-row table, and the candidate list is three times the
    # pool: that join alone costs about as much as the rest of hydration together.
    if since:
        where.append("f.occurred_at >= :since")
        params["since"] = since
    if until:
        where.append("f.occurred_at <= :until")
        params["until"] = until
    join = "FROM events_fts f"
    if source:
        join += " JOIN threads t ON t.id = f.thread_id"
        where.append(_in_clause("t.source", source, "src", params, negate=False))

    sql = sa_text(
        "SELECT f.event_id, f.thread_id, f.event_type, f.content_type, "
        "f.content AS full_content, f.occurred_at " + join +
        " WHERE " + " AND ".join(where)
    )
    # Hydration — the candidate ids back into hits, over a wide IN clause. Timed
    # together with the row build below: both scale with the candidate pool, and
    # the query and the Python loop are one cost to the caller.
    _t = time.perf_counter()
    with get_session() as s:
        hit_rows = s.execute(sql, params).mappings().all()

    hydrated: list[tuple[float, EventHit]] = []
    for r in hit_rows:
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
    _probe.record("hydrate_ms", _t)
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
