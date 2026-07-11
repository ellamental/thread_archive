"""Vector (semantic) search: the store + KNN + RRF fusion.

Tested with *synthetic* vectors so no embedding model is needed — the model-driven
embed path runs only when the [embeddings] extra is installed (covered by the
backfill embed, not the unit suite).
"""

from __future__ import annotations

import numpy as np

from thread_archive.retrieval import _rrf_merge, vectors
from thread_archive.store import init_db


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


def test_index_events_local_incremental_cap_and_order(archive_home, monkeypatch) -> None:
    """The cohost's embed pass: incremental (anti-join), bounded by ``max_events``,
    newest-first. Stubs the embed backend so no model is needed."""
    import json

    import thread_archive as ta
    from thread_archive.retrieval import embed as E

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

    import thread_archive as ta
    from thread_archive.retrieval import embed as E

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

    from thread_archive.store import get_session
    with get_session() as s:
        s.execute(sa_text("DELETE FROM event_vectors WHERE chunk > 0"))
        s.commit()
    vectors._bump_version()
    assert vectors.index_events_local() == 1          # the long doc is pending again
    assert vectors.get_status()["indexed"] == 4


def test_ensure_index_migrates_prechunk_table(archive_home) -> None:
    init_db()
    from sqlalchemy import text as sa_text

    from thread_archive.store import get_session
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
