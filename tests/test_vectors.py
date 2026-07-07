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
