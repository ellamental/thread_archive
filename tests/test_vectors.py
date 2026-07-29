"""Vector (semantic) search: the store + KNN + RRF fusion.

Tested with *synthetic* vectors so no embedding model is needed — the model-driven
embed path runs only when the [embeddings] extra is installed (covered by the
backfill embed, not the unit suite). Where a test needs the embed backend alive it
passes its own embedder through the ``embedder`` argument, so the real indexing and
query paths run over a stand-in model rather than real weights.
"""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from thread_archive._retrieval import _rrf_merge, vectors
from thread_archive._store import init_db


def _unit(*nonzero) -> np.ndarray:
    v = np.zeros(768, dtype=np.float32)
    for i, val in nonzero:
        v[i] = val
    return v


def _seed_thread(archive_home, user_text: str) -> None:
    """One real imported session, so the lexical arm has something to return."""
    import json

    from thread_archive._importers import import_session_incremental

    f = archive_home / "seed.jsonl"
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
         "sessionId": "s", "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "ok."}]}},
    ]
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    import_session_incremental(f, "proj:s")


class _FixedEmbedder:
    """An embedder that answers everything with one fixed vector, and records the
    queries it was given — the front-door stand-in for the torch model."""

    def __init__(self, vec=None) -> None:
        self.vec = list((vec if vec is not None else _unit((0, 1.0))))
        self.queries: list[str] = []

    def is_available(self) -> bool:
        return True

    def space_key(self) -> str:
        return "local:test"

    def embed_query(self, text):
        self.queries.append(text)
        return list(self.vec)

    def embed_documents(self, texts):
        return [list(self.vec) for _ in texts]


class _OrderRecordingEmbedder:
    """Records the length of every text handed to ``embed_documents``, in call
    order — so a test can read back the batch composition the drain built."""

    def __init__(self) -> None:
        self.seen_lengths: list[int] = []

    def is_available(self) -> bool:
        return True

    def space_key(self) -> str:
        return "local:test"

    def embed_documents(self, texts):
        self.seen_lengths.extend(len(t) for t in texts)
        return [[1.0] + [0.0] * 767 for _ in texts]


def test_length_batched_sorts_within_windows_preserving_recency() -> None:
    rows = [(i, "user", "x" * n) for i, n in enumerate([500, 10, 300, 20, 400])]
    # A window spanning the whole set: fully length-sorted.
    out = vectors._length_batched(list(rows), 999)
    assert [len(r[2]) for r in out] == [10, 20, 300, 400, 500]
    # Windowed: sorted within each pair, but window 0's docs all precede window 1's —
    # the newest-window-first grain the recency order relies on.
    out2 = vectors._length_batched(list(rows), 2)
    assert [len(r[2]) for r in out2] == [10, 500, 20, 300, 400]
    assert sorted(out2) == sorted(rows)              # a permutation — nothing lost or added
    assert vectors._length_batched(list(rows), 0) == rows  # disabled → identity


def test_length_batched_is_stable_within_a_window() -> None:
    # Equal-length docs keep their incoming (recency) order after the sort.
    rows = [("a", "user", "yy"), ("b", "user", "zz"), ("c", "user", "w")]
    out = vectors._length_batched(list(rows), 999)
    assert [r[0] for r in out] == ["c", "a", "b"]


def test_drain_bills_the_cold_model_load_separately(archive_home) -> None:
    """A lazily-loaded model would otherwise cold-load inside the first embed call
    and be billed to ``encode`` — on a short pass, most of the reported encode."""
    import json

    from thread_archive import _api as ta
    from thread_archive._ops import load_runs

    class _LazyEmbedder(_OrderRecordingEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.loaded = False
            self.warms = 0

        def is_loaded(self) -> bool:
            return self.loaded

        def warm(self) -> bool:
            self.warms += 1
            self.loaded = True
            return True

    f = archive_home / "sess.jsonl"
    f.write_text(json.dumps(
        {"type": "user", "uuid": "u0", "timestamp": "2026-01-01T10:00:00Z", "cwd": "/p",
         "message": {"role": "user", "content": "a question worth embedding"}}) + "\n",
        encoding="utf-8")
    ta.import_path(f)

    emb = _LazyEmbedder()
    run = load_runs.LoadRun("embed", archive_home)
    with run.phase("embed") as ph:
        assert vectors.index_events_local(embedder=emb, phase=ph) == 1
    assert emb.warms == 1                              # warmed once, up front…
    assert "model_load" in ph.snapshot()["detail_s"]   # …and billed to its own line

    # An already-loaded model costs no warm and gets no model_load line.
    emb2 = _LazyEmbedder()
    emb2.loaded = True
    run2 = load_runs.LoadRun("embed", archive_home)
    with run2.phase("embed") as ph2:
        vectors.index_events_local(embedder=emb2, phase=ph2, rebuild=True)
    assert emb2.warms == 0
    assert "model_load" not in ph2.snapshot()["detail_s"]


def test_drain_length_sorts_within_window(archive_home) -> None:
    """The embed drain reorders pending docs by length within a recency window so
    each encode batch is length-homogeneous (near-zero pad waste), while a whole
    window still drains before the next. The set embedded is identical either way."""
    import json

    from thread_archive import _api as ta

    lengths = [500, 10, 300, 20, 400]  # jagged, in event_id (natural) order
    f = archive_home / "sess.jsonl"
    lines = [
        {"type": "user", "uuid": f"u{i}", "timestamp": f"2026-01-01T10:0{i}:00Z",
         "cwd": "/p", "message": {"role": "user", "content": "x" * n}}
        for i, n in enumerate(lengths)
    ]
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    ta.import_path(f)

    # sort_window=0 hands the embedder the jagged sequence as-is (padding waste).
    nat = _OrderRecordingEmbedder()
    assert vectors.index_events_local(embedder=nat, sort_window=0) == 5
    assert nat.seen_lengths == lengths

    # A window of 2: each pair length-sorts, first window's docs still lead.
    win = _OrderRecordingEmbedder()
    assert vectors.index_events_local(embedder=win, sort_window=2, rebuild=True) == 5
    assert win.seen_lengths == [10, 500, 20, 300, 400]

    # The default single window length-sorts the whole pass — the padding win.
    allw = _OrderRecordingEmbedder()
    assert vectors.index_events_local(embedder=allw, rebuild=True) == 5
    assert allw.seen_lengths == sorted(lengths)


def test_vector_store_upsert_and_knn(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    a = _unit((0, 1.0))            # points along dim 0
    b = _unit((1, 1.0))            # orthogonal to a
    c = _unit((0, 0.9), (1, 0.1))  # mostly like a

    assert vectors.index_vectors([(1, "user", a), (2, "user", b), (3, "user", c)]) == 3

    res = vectors._knn(a.tolist(), ("user",), cand=10)
    ids = [eid for eid, _, _ in res]
    assert ids[0] == 1          # exact match first
    assert ids[1] == 3          # c is nearer to a than the orthogonal b
    assert ids[-1] == 2

    # upsert (same key) replaces, not duplicates
    assert vectors.index_vectors([(1, "user", b)]) == 1
    assert vectors.get_status()["indexed"] == 3


def test_knn_pack_mmap_lifecycle(archive_home) -> None:
    """The KNN matrix serves from the on-disk pack: built once (mmap'd, not
    resident), reused while the store token holds, rebuilt when vectors change,
    and invalidated by the token-blind in-place upsert (meta unlink)."""
    init_db()
    vectors.ensure_index()
    a, b = _unit((0, 1.0)), _unit((1, 1.0))
    vectors.index_vectors([(1, "user", a), (2, "text", b)])

    res = vectors._knn(a.tolist(), ("user",), cand=10)
    assert [eid for eid, _, _ in res] == [1]
    d = vectors._pack_dir()
    mats = sorted(d.glob("mat-*.npy"))
    assert len(mats) == 1
    built = mats[0].stat().st_mtime_ns

    # scope change reuses the same pack (row mask, no rebuild)
    res = vectors._knn(a.tolist(), ("user", "text"), cand=10)
    assert [eid for eid, _, _ in res] == [1, 2]
    assert mats[0].stat().st_mtime_ns == built

    # a real vector write moves the token; the refresh rebuilds the pack and the
    # fresh matrix serves the new row (the request path serves stale until the
    # single-flight refresh lands — driven directly here).
    vectors.index_vectors([(3, "user", _unit((0, 0.5)))])
    vectors._refresh_matrix(vectors._matrix_key(("user",)), ("user",))
    res = vectors._knn(a.tolist(), ("user",), cand=10)
    assert [eid for eid, _, _ in res] == [1, 3]
    assert len(list(d.glob("mat-*.npy"))) >= 1

    # in-place upsert: count/maxrowid hold, but the metas are dropped so the
    # refresh rebuilds instead of serving the stale matrix
    vectors.index_vectors([(1, "user", b)])
    assert list(d.glob("meta-*.json")) == []
    vectors._refresh_matrix(vectors._matrix_key(("user",)), ("user",))
    res = vectors._knn(b.tolist(), ("user",), cand=10)
    assert [eid for eid, _, _ in res][0] == 1  # sees the upserted vector


def test_split_matrix_matmul_and_gather() -> None:
    """_SplitMatrix presents base + delta as one matrix: the matvec concatenates across
    the split, a row gather spans both halves in the requested order, and shape/len
    report the combined size — so nothing downstream sees the seam."""
    base = np.arange(6, dtype=np.float32).reshape(3, 2)      # rows 0,1,2
    delta = np.arange(6, 10, dtype=np.float32).reshape(2, 2)  # rows 3,4
    m = vectors._SplitMatrix(base, delta)
    assert m.shape == (5, 2)
    assert len(m) == 5
    q = np.asarray([1.0, 1.0], dtype=np.float32)
    assert np.array_equal(m @ q, np.concatenate([base @ q, delta @ q]))
    # gather across the split, order preserved
    gathered = m[np.asarray([4, 0, 3, 1])]
    assert np.array_equal(gathered, np.stack([delta[1], base[0], delta[0], base[1]]))
    # the all-base fast path
    assert np.array_equal(m[np.asarray([2, 0])], np.stack([base[2], base[0]]))


def test_pack_base_reused_delta_layered(archive_home) -> None:
    """A base pack is reused across token moves: new vectors ride in the in-RAM delta,
    so no fresh base is written, yet the KNN sees base + delta rows."""
    init_db()
    vectors.ensure_index()
    vectors.reset_matrix_cache()
    a, b = _unit((0, 1.0)), _unit((1, 1.0))
    key = vectors._matrix_key(("user",))
    vectors.index_vectors([(1, "user", a)])
    vectors._refresh_matrix(key, ("user",))  # builds the base (1 row)
    d = vectors._pack_dir()
    base_mats = sorted(p.name for p in d.glob("mat-*.npy"))
    assert len(base_mats) == 1

    # More vectors move the token; the refresh layers them as a delta, reusing the base.
    vectors.index_vectors([(2, "user", b), (3, "user", _unit((0, 0.5)))])
    vectors._refresh_matrix(key, ("user",))
    assert sorted(p.name for p in d.glob("mat-*.npy")) == base_mats  # no new base written
    ids = {eid for eid, _, _ in vectors._knn(a.tolist(), ("user",), cand=10)}
    assert ids == {1, 2, 3}  # base row + delta rows all searchable


def test_pack_folds_delta_when_it_grows_past_cap(archive_home) -> None:
    """Once the delta passes _DELTA_MAX_ROWS, a refresh folds it into a fresh base at the
    current token instead of layering an unbounded in-RAM tail."""
    init_db()
    vectors.ensure_index()
    vectors.reset_matrix_cache()
    key = vectors._matrix_key(("user",))
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    vectors._refresh_matrix(key, ("user",))  # base at 1 row
    d = vectors._pack_dir()
    assert len(list(d.glob("mat-*.npy"))) == 1

    original = vectors._DELTA_MAX_ROWS
    vectors._DELTA_MAX_ROWS = 1  # a delta of >1 row must now force a fold
    try:
        vectors.index_vectors([(2, "user", _unit((1, 1.0))), (3, "user", _unit((0, 0.5)))])
        vectors._refresh_matrix(key, ("user",))
    finally:
        vectors._DELTA_MAX_ROWS = original
    assert len(list(d.glob("mat-*.npy"))) == 2  # a fresh base was packed (delta 2 > cap 1)
    ids = {eid for eid, _, _ in vectors._knn(_unit((0, 1.0)).tolist(), ("user",), cand=10)}
    assert ids == {1, 2, 3}


def test_pack_rebuilds_fresh_base_on_dirty_prefix(archive_home) -> None:
    """A delete at or below the base watermark makes the base a dirty prefix; the next
    build discards it and packs fresh, so the deleted row is never served from the base."""
    from sqlalchemy import text as sa_text

    from thread_archive._store import get_session

    init_db()
    vectors.ensure_index()
    vectors.reset_matrix_cache()
    key = vectors._matrix_key(("user",))
    a, b = _unit((0, 1.0)), _unit((1, 1.0))
    vectors.index_vectors([(1, "user", a), (2, "user", b)])
    vectors._refresh_matrix(key, ("user",))  # base at 2 rows

    with get_session() as s:  # delete a base-range row (below the watermark)
        s.execute(sa_text("DELETE FROM event_vectors WHERE event_id = 1"))
        s.commit()
    vectors._bump_version()
    vectors.reset_matrix_cache()
    ids = [eid for eid, _, _ in vectors._knn(b.tolist(), ("user",), cand=10)]
    assert ids == [2]  # the deleted base row is gone, not served from the stale base


def test_parallel_search_stays_fast_and_never_rebuilds_on_request_path(archive_home) -> None:
    """thread_search under concurrency: with the matrix warmed, simultaneous searches all
    serve from the cached pack — none pays a base rebuild on its request thread (the
    regression that once serialized queries behind an 814MB rebuild and timed them out).
    The pack files on disk must be untouched by the burst, and it must finish well within
    a generous bound."""
    import json
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    from thread_archive import _api as ta

    init_db()
    emb = _FixedEmbedder()
    lines = []
    for i in range(16):
        lines.append({"type": "user", "uuid": f"u{i}", "timestamp": f"2026-01-01T10:{i:02d}:00Z",
                      "cwd": "/p", "message": {"role": "user", "content": f"vector search ranking {i}"}})
        lines.append({"type": "assistant", "uuid": f"a{i}", "timestamp": f"2026-01-01T10:{i:02d}:30Z",
                      "message": {"role": "assistant", "model": "claude-opus-4",
                                  "content": [{"type": "text", "text": f"semantic answer {i}"}]}})
    f = archive_home / "corpus.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    ta.import_path(f)
    assert vectors.index_events_local(embedder=emb) > 0

    # Warm the matrix (builds the base pack once), then snapshot the pack files.
    assert vectors.search("vector search ranking", embedder=emb) is not None
    d = vectors._pack_dir()
    before = {p.name: p.stat().st_mtime_ns for p in d.glob("*")}

    def _run(_) -> int:
        return len(vectors.search("vector search ranking", embedder=emb) or [])

    start = _time.perf_counter()
    with ThreadPoolExecutor(max_workers=5) as pool:
        counts = list(pool.map(_run, range(5)))
    elapsed = _time.perf_counter() - start

    assert all(c > 0 for c in counts)      # every concurrent search returned hits
    assert len(set(counts)) == 1           # …and agreed (deterministic under load)
    assert elapsed < 10.0                  # no serialized-rebuild stall / deadlock
    after = {p.name: p.stat().st_mtime_ns for p in d.glob("*")}
    assert after == before                 # the request path rebuilt nothing


def test_index_events_local_incremental_cap_and_order(archive_home) -> None:
    """The cohost's embed pass: incremental (anti-join), bounded by ``max_events``,
    newest-first. Runs on a stand-in embedder so no model is needed."""
    import json

    from thread_archive import _api as ta

    emb = _FixedEmbedder()

    f = archive_home / "sess.jsonl"
    lines = []
    for i in range(3):  # 3 user + 3 assistant-text = 6 embeddable events
        lines.append({"type": "user", "uuid": f"u{i}", "timestamp": f"2026-01-01T10:0{i}:00Z",
                      "cwd": "/p", "message": {"role": "user", "content": f"question {i}"}})
        lines.append({"type": "assistant", "uuid": f"a{i}", "timestamp": f"2026-01-01T10:0{i}:30Z",
                      "message": {"role": "assistant", "model": "claude-opus-4",
                                  "content": [{"type": "text", "text": f"answer {i}"}]}})
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    ta.import_path(f)

    assert vectors.get_status()["indexed"] == 0             # nothing embedded yet
    assert vectors.index_events_local(
        max_events=2, newest_first=True, embedder=emb) == 2  # capped pass
    assert vectors.get_status()["indexed"] == 2
    assert vectors.index_events_local(max_events=2, embedder=emb) == 2  # incremental: next gap
    assert vectors.index_events_local(embedder=emb) == 2                # drains the rest
    assert vectors.index_events_local(embedder=emb) == 0                # caught up → no-op


def test_knn_max_pools_chunks_per_document(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    a, b = _unit((0, 1.0)), _unit((1, 1.0))
    # doc 1: chunk 0 orthogonal to the query, chunk 1 an exact match; doc 2: close.
    assert vectors.index_vectors([(1, "text", 0, b), (1, "text", 1, a),
                                  (2, "text", 0, _unit((0, 0.9), (1, 0.1)))]) == 3
    res = vectors._knn(a.tolist(), ("text",), cand=10)
    # doc 1 appears once, scored by its best chunk (the exact match), and wins
    assert [(eid, round(sim, 2)) for eid, _, sim in res] == [(1, 1.0), (2, 0.99)]


def test_index_events_local_chunks_long_docs_and_tops_up(archive_home) -> None:
    """A doc longer than the chunk size gets one vector per chunk; a doc embedded
    under the old truncate-at-cap scheme (chunk 0 only) shows up as pending and is
    topped up in place."""
    import json

    from thread_archive import _api as ta

    emb = _FixedEmbedder()

    long_text = "x" * (vectors.CHUNK_CHARS * 2 + 100)  # → 3 chunks
    f = archive_home / "sess.jsonl"
    lines = [
        {"type": "user", "uuid": "u0", "timestamp": "2026-01-01T10:00:00Z", "cwd": "/p",
         "message": {"role": "user", "content": "short question"}},
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:01:00Z", "cwd": "/p",
         "message": {"role": "user", "content": long_text}},
    ]
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    ta.import_path(f)

    assert vectors.index_events_local(embedder=emb) == 2  # two docs…
    assert vectors.get_status()["indexed"] == 1 + 3       # …four vectors (1 + 3 chunks)
    assert vectors.index_events_local(embedder=emb) == 0  # caught up → no-op

    # Simulate the pre-chunking state: the long doc has only its chunk-0 vector.
    from sqlalchemy import text as sa_text

    from thread_archive._store import get_session
    with get_session() as s:
        s.execute(sa_text("DELETE FROM event_vectors WHERE chunk > 0"))
        s.commit()
    vectors._bump_version()
    assert vectors.index_events_local(embedder=emb) == 1  # the long doc is pending again
    assert vectors.get_status()["indexed"] == 4


def test_ensure_index_migrates_prechunk_table(archive_home) -> None:
    init_db()
    from sqlalchemy import text as sa_text

    from thread_archive._store import get_session
    with get_session() as s:
        s.execute(sa_text(
            "CREATE TABLE event_vectors (event_id INTEGER NOT NULL, content_type TEXT NOT NULL, "
            "dim INTEGER NOT NULL, vec BLOB NOT NULL, PRIMARY KEY (event_id, content_type))"
        ))
        s.execute(sa_text(
            "INSERT INTO event_vectors (event_id, content_type, dim, vec) VALUES (7, 'user', 768, :v)"
        ), {"v": _unit((0, 1.0)).tobytes()})
        s.commit()

    assert vectors.ensure_index() is True
    with get_session() as s:
        rows = s.execute(sa_text("SELECT event_id, content_type, chunk FROM event_vectors")).all()
    assert rows == [(7, "user", 0)]  # carried over as chunk 0
    # migrated table accepts chunked rows
    assert vectors.index_vectors([(7, "user", 1, _unit((1, 1.0)))]) == 1


def test_knn_pools_chunks_before_topk_cut(archive_home) -> None:
    """Several strong chunks of one long doc count as ONE candidate: with cand=2,
    doc 2 still surfaces even though doc 1's three chunks all outscore it."""
    init_db()
    vectors.ensure_index()
    a = _unit((0, 1.0))
    assert vectors.index_vectors([
        (1, "text", 0, a),
        (1, "text", 1, _unit((0, 0.99), (1, 0.14))),
        (1, "text", 2, _unit((0, 0.98), (1, 0.2))),
        (2, "text", 0, _unit((0, 0.9), (1, 0.44))),
    ]) == 4
    res = vectors._knn(a.tolist(), ("text",), cand=2)
    assert [eid for eid, _, _ in res] == [1, 2]
    assert round(res[0][2], 2) == 1.0  # doc 1 scored by its best chunk


def test_matrix_cache_canonical_key_and_bounded(archive_home) -> None:
    """Equivalent scopes in different orders share one cache entry, and the cache
    never grows past its bound (each entry is a full float32 matrix)."""
    init_db()
    vectors.ensure_index()
    assert vectors.index_vectors([(1, "user", _unit((0, 1.0)))]) == 1
    vectors._MATRIX_CACHE.clear()

    vectors._load_matrix(("user", "text"))
    vectors._load_matrix(("text", "user"))
    assert len(vectors._MATRIX_CACHE) == 1

    for cts in (("user",), ("text",), ("title",), ("user", "text"), ("user", "title")):
        vectors._load_matrix(cts)
    assert len(vectors._MATRIX_CACHE) <= vectors._MATRIX_CACHE_MAX


def test_exclude_all_embedded_types_sits_semantic_out(archive_home) -> None:
    """Excluding every embedded type empties the KNN scope → the arm returns None
    (lexical-only), instead of KNN-ing candidates the filter then discards."""
    init_db()
    vectors.ensure_index()
    assert vectors.index_vectors([(1, "user", _unit((0, 1.0)))]) == 1
    assert vectors.search("anything", exclude_content_types=["user", "text", "title"]) is None


def test_semantic_arm_sits_out_for_toolname_count_oldest(archive_home) -> None:
    """tool_name-scoped searches must not fuse semantic hits (tool docs aren't
    embedded, so every one would violate the filter); count and oldest are
    structural shapes the vector arm can only pollute."""
    from thread_archive import _retrieval as retrieval

    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])  # a pool for the arm to search
    # Observe at the embedder — the vector arm's first act is to embed the query, so
    # a query that never reaches it proves the arm sat out.
    emb = _FixedEmbedder()
    retrieval.search("some query", tool_name="Bash", embedder=emb)
    retrieval.search("some query", output="count", embedder=emb)
    retrieval.search("some query", sort="oldest", embedder=emb)
    assert emb.queries == []
    retrieval.search("some query", embedder=emb)
    assert emb.queries == ["some query"]


def test_semantic_arm_sits_out_on_a_store_it_cannot_serve(archive_home) -> None:
    """The vector store is SQLite-only. On another dialect the arm reports itself
    unavailable and search continues lexically rather than erroring."""
    from types import SimpleNamespace

    from thread_archive import _retrieval as retrieval
    from thread_archive._store._base import use_engine

    init_db()
    _seed_thread(archive_home, "the watcher daemon restarted overnight")

    class _OtherDialectEngine:
        dialect = SimpleNamespace(name="postgresql")

    with use_engine(_OtherDialectEngine()):
        assert vectors.is_available() is False
    # The lexical arm still answers (on the real store).
    assert retrieval.search("watcher daemon") is not None


def test_a_broken_semantic_arm_never_breaks_lexical_search(archive_home) -> None:
    """The vector arm is fail-soft by contract: an embedder whose vectors don't fit
    the indexed space blows up inside the KNN, and search must still return the
    lexical hits rather than propagate."""
    from thread_archive import _retrieval as retrieval

    init_db()
    vectors.ensure_index()
    _seed_thread(archive_home, "the watcher daemon restarted overnight")
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])

    wrong_width = _FixedEmbedder([1.0, 0.0, 0.0])  # not the store's 768 dims
    hits = retrieval.search("watcher daemon", embedder=wrong_width)
    assert hits and wrong_width.queries == ["watcher daemon"]


def test_semantic_hits_are_dated_from_the_shadow_row(archive_home) -> None:
    """The vector arm dates a hit — and cuts a ``since`` window — from the FTS
    shadow's own ``occurred_at``, never by joining ``events`` for it: that would be
    one scattered rowid lookup per candidate into a multi-million-row table, and
    the candidate list runs three times the pool wide. What makes it safe is that
    the shadow's copy *is* the event's, so this pins the agreement and both things
    that lean on it."""
    from datetime import datetime

    from sqlalchemy import text as sa_text

    from thread_archive._store import get_session

    init_db()
    _seed_thread(archive_home, "the vector arm dates its own hits")
    vectors.ensure_index()
    with get_session() as s:
        rows = s.execute(sa_text(
            "SELECT f.event_id, f.content_type, f.occurred_at, e.occurred_at AS ev "
            "FROM events_fts f JOIN events e ON e.id = f.event_id "
            "WHERE f.content_type IS NOT NULL AND f.occurred_at IS NOT NULL"
        )).mappings().all()
    assert rows
    assert all(str(r["occurred_at"]) == str(r["ev"]) for r in rows)
    vectors.index_vectors([(r["event_id"], r["content_type"], _unit((0, 1.0))) for r in rows])

    emb = _FixedEmbedder()
    hits = vectors.search("anything", embedder=emb, limit=50)
    assert hits
    dated = {r["event_id"]: str(r["occurred_at"]) for r in rows}
    for h in hits:
        assert h["occurred_at"] == datetime.fromisoformat(dated[h["event_id"]])

    stamps = sorted(set(dated.values()))
    assert len(stamps) > 1  # need two distinct stamps for the window to cut between
    late = vectors.search("anything", embedder=emb, limit=50, since=stamps[-1])
    assert late and len(late) < len(hits)
    assert all(h["occurred_at"] == datetime.fromisoformat(stamps[-1]) for h in late)


def test_vectors_search_sits_out_when_unindexed(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    # Nothing indexed → returns None *before* trying to embed (no model needed).
    assert vectors.search("anything") is None


def test_rrf_merge_fuses_and_carries_semantic() -> None:
    lexical = [
        {"event_id": 1, "content_type": "user"},
        {"event_id": 2, "content_type": "user"},
    ]
    semantic = [
        {"event_id": 2, "content_type": "user", "_semantic": 0.91},
        {"event_id": 3, "content_type": "user", "_semantic": 0.80},
    ]
    merged = _rrf_merge([lexical, semantic], limit=5)
    ids = [h["event_id"] for h in merged]
    assert ids[0] == 2                 # in both lists → highest fused score
    assert set(ids) == {1, 2, 3}
    two = next(h for h in merged if h["event_id"] == 2)
    assert two["_semantic"] == 0.91    # semantic provenance carried onto the fused hit
    assert all("_rrf" in h for h in merged)


def test_encode_honors_the_off_switch(monkeypatch):
    """embed's documented degrade contract — is_available() False means the vector
    arm sits out — must hold at the encode seam itself: query embedding reaches
    _encode directly (vectors.search), and a switched-off arm (conftest's model-free
    pin, --lexical-only) must never cold-load a model."""
    from thread_archive._retrieval import embed

    monkeypatch.setenv("THREAD_ARCHIVE_EMBED", "off")
    e = embed.Embedder(load=lambda: pytest.fail("model load attempted"))
    assert e.is_available() is False
    assert e.embed_query("anything at all") is None
    assert e.embed_documents(["doc"]) is None
    # …and the process embedder the vector arm actually reaches for is stood down too.
    assert embed.is_available() is False
    assert embed.embed_query("anything at all") is None


def test_the_vector_arm_runs_beside_the_lexical_one(archive_home) -> None:
    """The two pool arms share only the query and the scope, so they run at once and
    the pool waits on the slower rather than on their sum.

    Two things have to hold for that to be safe, and neither shows up in a result:
    the arm has to leave the calling thread (or nothing overlaps), and the search's
    context has to travel with it. The timing probe is context-local, so a worker
    started without a copied context finds no probe and drops the vector arm's whole
    sub-split — silently, and only in production, where anyone is measuring."""
    import json
    import threading

    from thread_archive import _api as ta
    from thread_archive._retrieval import _probe, search

    class _ThreadRecordingEmbedder(_FixedEmbedder):
        """Records which thread each query was embedded on, and whether the search's
        probe was reachable from there."""

        def __init__(self) -> None:
            super().__init__()
            self.threads: list = []
            self.saw_probe: list[bool] = []

        def embed_query(self, text):
            self.threads.append(threading.current_thread())
            self.saw_probe.append(_probe.current() is not None)
            return super().embed_query(text)

    init_db()
    emb = _ThreadRecordingEmbedder()
    lines = []
    for i in range(4):
        lines.append({"type": "user", "uuid": f"u{i}", "cwd": "/p",
                      "timestamp": f"2026-01-01T10:0{i}:00Z",
                      "message": {"role": "user", "content": f"vector search ranking {i}"}})
    f = archive_home / "arms.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    ta.import_path(f)
    assert vectors.index_events_local(embedder=emb) > 0

    with _probe.install() as probe:
        hits = search("vector search ranking", limit=5, embedder=emb)

    assert hits
    assert emb.threads, "the vector arm never ran"
    caller = threading.current_thread()
    assert all(t is not caller for t in emb.threads), "the vector arm stayed on the caller"
    assert all(emb.saw_probe), "the arm thread could not see the installed probe"
    assert probe.embed_ms > 0, "the arm's stages did not reach the caller's probe"


def test_a_time_scoped_search_still_excludes_agent_threads(archive_home) -> None:
    """The scope mask no longer carries the ``agents='exclude'`` filter — it costs a
    table probe per matched event, and an unscoped search never applied it there
    anyway. Hydration is what enforces it, for scoped and unscoped alike; this pins
    that a time-scoped search is not the hole that opens if it ever stops.

    ``agents='only'`` keeps its mask clause, because a corpus-wide top-k would be
    almost entirely rows it then discards."""
    import json

    from thread_archive import _api as ta

    init_db()
    emb = _FixedEmbedder()
    lines = [
        {"type": "user", "uuid": "h1", "cwd": "/p", "timestamp": "2026-01-02T10:00:00Z",
         "message": {"role": "user", "content": "vector search ranking in the session"}},
    ]
    f = archive_home / "human.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    ta.import_path(f)

    agent = archive_home / "agent.jsonl"
    agent.write_text(json.dumps(
        {"type": "user", "uuid": "s1", "cwd": "/p", "timestamp": "2026-01-02T11:00:00Z",
         "message": {"role": "user", "content": "vector search ranking in the subagent"}},
    ) + "\n", encoding="utf-8")
    ta.import_path(agent)

    from sqlalchemy import text as sa_text

    from thread_archive._store import get_session
    with get_session() as s:  # mark the second session an agent run
        s.execute(sa_text("UPDATE threads SET thread_type = 'system' "
                          "WHERE source_id LIKE :sid"), {"sid": "%agent%"})
        s.commit()
    assert vectors.index_events_local(embedder=emb) > 0

    scoped = vectors.search("vector search ranking", since="2026-01-01", embedder=emb) or []
    types = _thread_types(t["thread_id"] for t in scoped)
    assert scoped, "the time-scoped vector arm returned nothing"
    assert "system" not in types, "an agent thread survived a time-scoped search"

    only = vectors.search("vector search ranking", since="2026-01-01",
                          agents="only", embedder=emb) or []
    assert only and _thread_types(t["thread_id"] for t in only) == {"system"}


def _thread_types(thread_ids) -> set:
    from sqlalchemy import text as sa_text

    from thread_archive._store import get_session

    ids = list(dict.fromkeys(thread_ids))
    if not ids:
        return set()
    with get_session() as s:
        rows = s.execute(
            sa_text("SELECT thread_type FROM threads WHERE id IN ("
                    + ",".join(f"'{i}'" for i in ids) + ")")).fetchall()
    return {r[0] for r in rows}


def _dated_events(stamps: list[str], tag: str = "a") -> list[int]:
    """One event per timestamp, returning their ids in order. ``tag`` names the
    carrying thread, so a test can call this twice without colliding."""
    from datetime import datetime

    from thread_archive._store import Event, Thread, get_session

    ids: list[int] = []
    with get_session() as s:
        t = Thread(name=f"conv:dated-{tag}", title="dated", thread_type="conversation",
                   source="cc", source_id=f"dated-{tag}")
        s.add(t)
        s.flush()
        for i, stamp in enumerate(stamps):
            e = Event(thread_id=t.id, stream_id=f"d{tag}{i}", event_type="user_message_sent",
                      payload={"content": f"dated message {i}"},
                      occurred_at=datetime.fromisoformat(stamp))
            s.add(e)
            s.flush()
            ids.append(e.id)
        s.commit()
    return ids


def test_a_time_window_is_read_off_the_pack_not_queried_from_events(archive_home) -> None:
    """The scope query this replaces had to name every in-scope event id — millions of
    them for a wide window, against a pack holding a fraction as many rows, because
    most events carry no vector at all. The pack already knows each row's date, so the
    window is a comparison over an array it holds. Same rows either way: that
    equivalence is the whole claim, and it is what this pins."""
    init_db()
    vectors.ensure_index()
    ids = _dated_events(["2026-01-01T10:00:00+00:00", "2026-03-01T10:00:00+00:00",
                         "2026-06-01T10:00:00+00:00"])
    a = _unit((0, 1.0))
    vectors.index_vectors([(i, "user", a) for i in ids])

    everything = [e for e, _, _ in vectors._knn(a.tolist(), ("user",), cand=10)]
    assert everything == sorted(ids)

    # The window cuts where the timestamps say, with no id list involved.
    windowed = [e for e, _, _ in vectors._knn(a.tolist(), ("user",), cand=10,
                                              since="2026-02-01T00:00:00+00:00")]
    assert windowed == sorted(ids[1:])
    both = [e for e, _, _ in vectors._knn(a.tolist(), ("user",), cand=10,
                                          since="2026-02-01T00:00:00+00:00",
                                          until="2026-04-01T00:00:00+00:00")]
    assert both == [ids[1]]

    # And it agrees with the id-mask path it replaced, which is the only thing that
    # makes the substitution safe — the two narrow the same pool to the same rows.
    masked = [e for e, _, _ in vectors._knn(
        a.tolist(), ("user",), cand=10, allowed_ids=np.asarray(ids[1:], dtype=np.int64))]
    assert masked == windowed

    # Composed, they intersect rather than one winning.
    narrowed = [e for e, _, _ in vectors._knn(
        a.tolist(), ("user",), cand=10, since="2026-02-01T00:00:00+00:00",
        allowed_ids=np.asarray([ids[0], ids[1]], dtype=np.int64))]
    assert narrowed == [ids[1]]


def test_an_undated_row_is_in_no_time_window_including_an_open_ended_one(
    archive_home,
) -> None:
    """A vector whose event is missing packs to an empty timestamp, and empty bytes
    sort below every real one. Ordering alone would therefore place it *before* any
    ``until`` bound and sweep it into every open-ended window — so exclusion is
    explicit. The id-query this replaces got the same answer for free: an event that
    isn't in ``events`` was never in its result."""
    init_db()
    vectors.ensure_index()
    dated = _dated_events(["2026-01-01T10:00:00+00:00"])
    a = _unit((0, 1.0))
    vectors.index_vectors([(dated[0], "user", a), (99_000_001, "user", a)])

    # Unscoped, both rows are candidates — the pack does not care about dates.
    assert len(vectors._knn(a.tolist(), ("user",), cand=10)) == 2
    # An `until` far past every real stamp is the trap: the undated row must not ride
    # in on being "before" it.
    until_only = [e for e, _, _ in vectors._knn(a.tolist(), ("user",), cand=10,
                                                until="2099-01-01T00:00:00+00:00")]
    assert until_only == dated
    since_only = [e for e, _, _ in vectors._knn(a.tolist(), ("user",), cand=10,
                                                since="2020-01-01T00:00:00+00:00")]
    assert since_only == dated


def test_a_pack_without_timestamps_is_passed_over_rather_than_loaded(archive_home) -> None:
    """A pack is derived and disposable, so an older one missing an array is not a
    thing to repair or migrate — the reuse check simply declines it and the next build
    writes a complete one. Without this the process would map an incomplete pack and
    fail on a file that was never there."""
    init_db()
    vectors.ensure_index()
    ids = _dated_events(["2026-01-01T10:00:00+00:00"])
    a = _unit((0, 1.0))
    vectors.index_vectors([(ids[0], "user", a)])
    vectors._knn(a.tolist(), ("user",), cand=10)  # builds the pack

    d = vectors._pack_dir()
    stamps = list(d.glob("ts-*.npy"))
    assert stamps, "a freshly written base carries its timestamps"

    from thread_archive._store import get_session

    with get_session() as s:
        count = s.execute(
            __import__("sqlalchemy").text("SELECT count(*) FROM event_vectors")).scalar()
        assert vectors._reusable_base(s, d, count) is not None
        for p in stamps:  # the shape of an older pack
            p.unlink()
        assert vectors._reusable_base(s, d, count) is None

    # And the next load rebuilds rather than raising on the missing file.
    vectors.reset_matrix_cache()
    assert [e for e, _, _ in vectors._knn(a.tolist(), ("user",), cand=10)] == ids
    assert list(d.glob("ts-*.npy"))


def test_timestamps_survive_the_base_plus_delta_stitch(archive_home) -> None:
    """The pack is a base plus an in-RAM tail of everything written since, and every
    array has to be stitched across both halves. A timestamp array that covered only
    the base would misalign against ids from the delta on — silently, and in a way
    that would move rows across scope boundaries rather than raise."""
    init_db()
    vectors.ensure_index()
    early = _dated_events(["2026-01-01T10:00:00+00:00"])
    a = _unit((0, 1.0))
    vectors.index_vectors([(early[0], "user", a)])
    vectors._knn(a.tolist(), ("user",), cand=10)  # base written for the first row alone

    late = _dated_events(["2026-09-01T10:00:00+00:00"], tag="b")
    vectors.index_vectors([(late[0], "user", a)])
    vectors.reset_matrix_cache()

    # The late row lives in the delta; its date has to come along with it.
    assert [e for e, _, _ in vectors._knn(a.tolist(), ("user",), cand=10,
                                          since="2026-06-01T00:00:00+00:00")] == late
    assert [e for e, _, _ in vectors._knn(a.tolist(), ("user",), cand=10,
                                          until="2026-06-01T00:00:00+00:00")] == early


def test_release_accelerator_cache_never_imports_torch_to_find_none() -> None:
    """The drain's memory release must cost nothing on a lexical-only install.

    A clean child is the honest environment: in-process, some earlier test has almost
    certainly imported torch already, so the branch this covers — no allocator was
    ever built — is unreachable without a fresh interpreter. Importing torch merely
    to discover there is nothing to release would cost far more than it could return,
    so the helper reports False off ``sys.modules`` and leaves it unimported.
    """
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys;"
         "from thread_archive._retrieval.embed import release_accelerator_cache as r;"
         "print(r(), 'torch' in sys.modules)"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["False", "False"]


def test_drain_releases_without_disturbing_its_count(archive_home) -> None:
    """The release runs in a ``finally``, so it sits on the drain's return path — a
    raise or a changed return there would corrupt every caller's embedded count. The
    idle pass matters as much as the working one: the watcher cohost drains every few
    seconds and mostly finds nothing, so a caught-up pass must stay free."""
    class _CountingEmbedder(_FixedEmbedder):
        calls = 0

        def embed_documents(self, texts):
            self.calls += 1
            return super().embed_documents(texts)

    init_db()
    _seed_thread(archive_home, "vector drain release")
    vectors.ensure_index()
    emb = _CountingEmbedder()

    assert vectors.index_events_local(embedder=emb) > 0
    assert emb.calls > 0
    # Caught up: nothing to embed, so the drain neither encodes nor releases.
    before = emb.calls
    assert vectors.index_events_local(embedder=emb) == 0
    assert emb.calls == before
