"""Ranking + cross-encoder re-rank: the weighted lexical scorer and the gated
in-process cross-encoder head re-order.

The lexical ranker is tested directly (pure Python). The cross-encoder is tested
with its scoring monkeypatched — the torch model path itself runs only when the
[embeddings] extra is installed (never in the unit suite), but the reorder logic,
gating, and fail-soft contract are all pure.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from thread_archive._retrieval import embed, rank, rerank, warm_models


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


def test_search_terms_strips_backtick_fencing() -> None:
    # Backticks are markdown a user wraps around an identifier — not part of the
    # token. Stripped, so the density scorer credits the bare identifier in prose,
    # not only backtick-wrapped occurrences (`thread_search` must rank like it).
    assert rank.search_terms("`thread_search`") == ["thread_search"]
    assert rank.search_terms("`get_session`") == ["get_session"]
    assert rank.search_terms("`thread:foo`") == ["thread", "foo"]


def test_search_terms_quoted_phrase_is_one_term() -> None:
    terms = rank.search_terms('auth "login flow"')
    assert "login flow" in terms and "auth" in terms


def test_search_terms_drops_stopwords() -> None:
    # Function words don't count toward density or the K/N quality verdict.
    assert rank.search_terms("how did we fix the auth bug") == ["fix", "auth", "bug"]
    # …but an all-stopword query keeps its terms (weak signal beats none)
    assert rank.search_terms("what is this") == ["what", "is", "this"]
    # quoted phrases survive even when made of stopwords
    assert "what is" in rank.search_terms('"what is" auth')


# ── should_rerank gate ────────────────────────────────────────────────────────
def test_should_rerank_gates_to_conceptual_multiterm() -> None:
    assert rank.should_rerank("memory leak", ["memory", "leak"]) is True
    # single term — too little signal to re-rank
    assert rank.should_rerank("authentication", ["authentication"]) is False
    # pipe-OR / quoted / identifier-dominated shapes the lexical arm already nails
    assert rank.should_rerank("memory | leak", ["memory", "leak"]) is False
    assert rank.should_rerank('"login flow"', ["login flow"]) is False
    assert rank.should_rerank("get_session pool", ["get_session", "pool"]) is False
    # a conceptual question that merely *mentions* an identifier still reranks
    assert rank.should_rerank(
        "why thread_search misses old threads",
        ["why", "thread_search", "misses", "old", "threads"],
    ) is True


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


# ── warm() preload contract (model-free) ─────────────────────────────────────
class _RecordingSlot(rerank.ModelSlot):
    """A slot that counts get() calls — swapped in whole (SLOT is the public
    seam) to prove the heavy loader is never consulted."""

    def __init__(self) -> None:
        super().__init__()
        self.gets = 0

    def get(self, construct, on_error):
        self.gets += 1
        return object()


def test_warm_skips_the_loader_when_unavailable(monkeypatch) -> None:
    # Extra absent → warm() reports False and never touches the (heavy) model loader.
    for mod in (rerank, embed):
        slot = _RecordingSlot()
        monkeypatch.setattr(mod, "is_available", lambda: False)
        monkeypatch.setattr(mod, "SLOT", slot)
        assert mod.warm() is False
        assert slot.gets == 0


def test_warm_reports_the_load_outcome(monkeypatch) -> None:
    # Available + a model loads → True; available + load fails (None) → False (degrade lazily).
    for mod in (rerank, embed):
        monkeypatch.setattr(mod, "is_available", lambda: True)
        monkeypatch.setattr(mod.SLOT, "model", object())     # already loaded
        assert mod.warm() is True
        monkeypatch.setattr(mod.SLOT, "model", None)
        monkeypatch.setattr(mod.SLOT, "load_failed", True)   # cached failure
        assert mod.warm() is False


def test_warm_models_never_raises(monkeypatch) -> None:
    # A stage that blows up must not propagate — warming is best-effort startup work. Every
    # stage is stubbed to raise (and the dummy search stubbed out) so the suite stays model-free.
    from thread_archive import _api as api

    def _raise(*a, **k):
        raise RuntimeError("torch exploded")

    monkeypatch.setattr(embed, "warm", _raise)
    monkeypatch.setattr(rerank, "warm", _raise)
    monkeypatch.setattr(api, "search", _raise)
    warm_models()  # returns None, swallows every stage failure
