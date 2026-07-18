"""Vector (semantic) search: the store + KNN + RRF fusion.

Tested with *synthetic* vectors so no embedding model is needed — the model-driven
embed path runs only when the [embeddings] extra is installed (covered by the
backfill embed, not the unit suite).
"""

from __future__ import annotations

import numpy as np

from thread_archive._retrieval import _rrf_merge, vectors
from thread_archive._store import init_db


def _unit(*nonzero) -> np.ndarray:
    v = np.zeros(768, dtype=np.float32)
    for i, val in nonzero:
        v[i] = val
    return v


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

    # a real vector write moves the token → a fresh pack serves the new row
    vectors.index_vectors([(3, "user", _unit((0, 0.5)))])
    res = vectors._knn(a.tolist(), ("user",), cand=10)
    assert [eid for eid, _, _ in res] == [1, 3]
    assert len(list(d.glob("mat-*.npy"))) >= 1

    # in-place upsert: count/maxrowid hold, but the metas are dropped so the
    # next load rebuilds instead of serving the stale matrix
    vectors.index_vectors([(1, "user", b)])
    assert list(d.glob("meta-*.json")) == []
    res = vectors._knn(b.tolist(), ("user",), cand=10)
    assert [eid for eid, _, _ in res][0] == 1  # sees the upserted vector


def test_index_events_local_incremental_cap_and_order(archive_home, monkeypatch) -> None:
    """The cohost's embed pass: incremental (anti-join), bounded by ``max_events``,
    newest-first. Stubs the embed backend so no model is needed."""
    import json

    from thread_archive import _api as ta
    from thread_archive._retrieval import embed as E

    monkeypatch.setattr(E, "is_available", lambda: True)
    monkeypatch.setattr(E, "embed_documents", lambda docs: [_unit((0, 1.0)) for _ in docs])

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

    assert vectors.get_status()["indexed"] == 0                             # nothing embedded yet
    assert vectors.index_events_local(max_events=2, newest_first=True) == 2  # capped pass
    assert vectors.get_status()["indexed"] == 2
    assert vectors.index_events_local(max_events=2) == 2                    # incremental: next gap
    assert vectors.index_events_local() == 2                                # drains the rest
    assert vectors.index_events_local() == 0                                # caught up → no-op


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


def test_index_events_local_chunks_long_docs_and_tops_up(archive_home, monkeypatch) -> None:
    """A doc longer than the chunk size gets one vector per chunk; a doc embedded
    under the old truncate-at-cap scheme (chunk 0 only) shows up as pending and is
    topped up in place."""
    import json

    from thread_archive import _api as ta
    from thread_archive._retrieval import embed as E

    monkeypatch.setattr(E, "is_available", lambda: True)
    monkeypatch.setattr(E, "embed_documents", lambda docs: [_unit((0, 1.0)) for _ in docs])

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

    assert vectors.index_events_local() == 2          # two docs…
    assert vectors.get_status()["indexed"] == 1 + 3   # …four vectors (1 + 3 chunks)
    assert vectors.index_events_local() == 0          # caught up → no-op

    # Simulate the pre-chunking state: the long doc has only its chunk-0 vector.
    from sqlalchemy import text as sa_text

    from thread_archive._store import get_session
    with get_session() as s:
        s.execute(sa_text("DELETE FROM event_vectors WHERE chunk > 0"))
        s.commit()
    vectors._bump_version()
    assert vectors.index_events_local() == 1          # the long doc is pending again
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

    for cts in (("user",), ("text",), ("title",), ("summary",), ("user", "title")):
        vectors._load_matrix(cts)
    assert len(vectors._MATRIX_CACHE) <= vectors._MATRIX_CACHE_MAX


def test_exclude_all_embedded_types_sits_semantic_out(archive_home) -> None:
    """Excluding every embedded type empties the KNN scope → the arm returns None
    (lexical-only), instead of KNN-ing candidates the filter then discards."""
    init_db()
    vectors.ensure_index()
    assert vectors.index_vectors([(1, "user", _unit((0, 1.0)))]) == 1
    assert vectors.search("anything", exclude_content_types=["user", "text", "title", "summary"]) is None


def test_semantic_arm_sits_out_for_toolname_count_oldest(archive_home, monkeypatch) -> None:
    """tool_name-scoped searches must not fuse semantic hits (tool docs aren't
    embedded, so every one would violate the filter); count and oldest are
    structural shapes the vector arm can only pollute."""
    from thread_archive import _retrieval as retrieval

    init_db()
    # Record at the vector arm's own boundary (vectors.search, public): a scoped
    # search must never reach it; an unscoped one does. None keeps fusion lexical.
    calls: list = []
    monkeypatch.setattr(vectors, "is_available", lambda: True)
    monkeypatch.setattr(vectors, "search",
                        lambda *a, **k: (calls.append(1), None)[1])
    retrieval.search("some query", tool_name="Bash")
    retrieval.search("some query", output="count")
    retrieval.search("some query", sort="oldest")
    assert calls == []
    retrieval.search("some query")
    assert calls == [1]


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


def test_encode_honors_availability_stub(monkeypatch):
    """embed's documented degrade contract — is_available() False means the
    vector arm sits out — must hold at the encode seam itself: query embedding
    reaches _encode directly (vectors.search), and a stubbed-off availability
    (conftest's model-free pin, --lexical-only) must never cold-load a model."""
    from thread_archive._retrieval import embed

    class _NoLoadSlot(embed.ModelSlot):
        """A slot that treats any load attempt as a test failure."""

        def get(self, construct, on_error):
            raise AssertionError("model load attempted")

    monkeypatch.setattr(embed, "is_available", lambda: False)
    monkeypatch.setattr(embed, "SLOT", _NoLoadSlot())
    assert embed.embed_query("anything at all") is None
    assert embed.embed_documents(["doc"]) is None
