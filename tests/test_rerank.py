"""Ranking + cross-encoder re-rank: the weighted lexical scorer and the gated
in-process cross-encoder head re-order.

The lexical ranker is tested directly (pure Python). The cross-encoder is tested
with its scoring monkeypatched — the torch model path itself runs only when the
[embeddings] extra is installed (never in the unit suite), but the reorder logic,
gating, and fail-soft contract are all pure.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from thread_archive.retrieval import rank, rerank


def _hit(eid, content, ct="user", occurred_at=None, rrf=0.0):
    return {
        "event_id": eid, "thread_id": eid, "content_type": ct,
        "full_content": content, "snippet": content,
        "occurred_at": occurred_at, "_rrf": rrf,
    }


# ── search_terms ──────────────────────────────────────────────────────────────
def test_search_terms_preserves_identifiers_and_splits_words() -> None:
    assert rank.search_terms("memory leak") == ["memory", "leak"]
    # underscores are identifiers — kept intact, not split
    assert rank.search_terms("help_think") == ["help_think"]
    assert rank.search_terms("") == []


def test_search_terms_quoted_phrase_is_one_term() -> None:
    terms = rank.search_terms('auth "login flow"')
    assert "login flow" in terms and "auth" in terms


# ── should_rerank gate ────────────────────────────────────────────────────────
def test_should_rerank_gates_to_conceptual_multiterm() -> None:
    assert rank.should_rerank("memory leak", ["memory", "leak"]) is True
    # single term — too little signal to re-rank
    assert rank.should_rerank("authentication", ["authentication"]) is False
    # pipe-OR / quoted / code-identifier shapes the lexical arm already nails
    assert rank.should_rerank("memory | leak", ["memory", "leak"]) is False
    assert rank.should_rerank('"login flow"', ["login flow"]) is False
    assert rank.should_rerank("get_session pool", ["get_session", "pool"]) is False


# ── dedup ─────────────────────────────────────────────────────────────────────
def test_dedup_collapses_identical_same_thread_keeps_cross_thread() -> None:
    hits = [_hit(1, "same text"), _hit(1, "same text"), _hit(2, "same text")]
    hits[1]["thread_id"] = 1
    hits[2]["thread_id"] = 2
    out = rank.dedup_results(hits)
    # thread 1's duplicate collapses; thread 2's identical line survives
    assert len(out) == 2
    assert {h["thread_id"] for h in out} == {1, 2}


# ── lexical ranker ────────────────────────────────────────────────────────────
def test_rank_density_beats_long_dump() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    focused = _hit(1, "deduplication is the key idea here", occurred_at=now)
    buried = _hit(2, "deduplication " + ("x " * 4000), occurred_at=now)
    ranked = rank.rank_search_results([buried, focused], ["deduplication"], 2, now=now)
    assert ranked[0]["event_id"] == 1  # focused message beats the long dump


def test_rank_content_type_weight_prefers_user() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    user = _hit(1, "reindex the store", ct="user", occurred_at=now)
    tool = _hit(2, "reindex the store", ct="tool", occurred_at=now)
    ranked = rank.rank_search_results([tool, user], ["reindex"], 2, now=now)
    assert ranked[0]["content_type"] == "user"


def test_rank_fusion_rescues_semantic_only_hit() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    # neither contains the query term (vocab mismatch) → density 0 for both; the
    # one the vector arm ranked highly (_rrf=1.0) must win on the fusion term.
    semantic = _hit(1, "totally different words", occurred_at=now, rrf=1.0)
    other = _hit(2, "also unrelated text", occurred_at=now, rrf=0.0)
    ranked = rank.rank_search_results([other, semantic], ["concept"], 2, now=now)
    assert ranked[0]["event_id"] == 1


def test_rank_recency_is_mild_tiebreaker() -> None:
    now = datetime(2026, 1, 10, 12, 0, 0)
    old = _hit(1, "same content here", occurred_at=now - timedelta(days=30))
    new = _hit(2, "same content here", occurred_at=now)
    ranked = rank.rank_search_results([old, new], ["content"], 2, now=now)
    assert ranked[0]["event_id"] == 2  # recent wins when content score ties


# ── cross-encoder reorder (model-free) ───────────────────────────────────────
def test_rerank_reorders_by_scores(monkeypatch) -> None:
    # Stub the model scores: item B most relevant, then C, then A.
    monkeypatch.setattr(rerank, "rerank_scores", lambda q, docs: [0.1, 0.9, 0.5])
    items = ["A", "B", "C"]
    out = rerank.rerank("q", items, get_text=lambda x: x)
    assert out == ["B", "C", "A"]


def test_rerank_fail_soft_returns_none(monkeypatch) -> None:
    # Reranker unavailable / errored → None, so the caller keeps its own order.
    monkeypatch.setattr(rerank, "rerank_scores", lambda q, docs: None)
    assert rerank.rerank("q", ["A", "B"], get_text=lambda x: x) is None
    # empty input is None too
    assert rerank.rerank("q", [], get_text=lambda x: x) is None


def test_rerank_scores_none_without_model(monkeypatch) -> None:
    # No query / no docs short-circuits before any model load.
    assert rerank.rerank_scores("", ["a"]) is None
    assert rerank.rerank_scores("q", []) is None
