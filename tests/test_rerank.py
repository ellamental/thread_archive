"""Ranking + cross-encoder re-rank: the weighted lexical scorer and the gated
in-process cross-encoder head re-order.

The lexical ranker is tested directly (pure Python). The cross-encoder runs as a
real :class:`~.rerank.Reranker` built around a scripted scorer and handed to the
pipeline through its ``reranker`` argument — the product's own gating, scoring
and reordering execute; only the torch weights are stood in for.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

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


# ── head_is_strong (the result-side gate half) ───────────────────────────────
def test_strong_match_floor_is_two_thirds_min_one() -> None:
    assert rank.strong_match_floor(1) == 1
    assert rank.strong_match_floor(2) == 2
    assert rank.strong_match_floor(3) == 2
    assert rank.strong_match_floor(6) == 4


def test_head_is_strong_reads_the_top_hit_only() -> None:
    terms = ["watcher", "backlog", "daemon"]
    strong = _hit(1, "the watcher daemon backlog drained overnight")
    weak = _hit(2, "an unrelated message about breakfast")
    # strong top hit → True regardless of what ranks below it
    assert rank.head_is_strong([strong, weak], terms) is True
    # weak top hit → False even with a strong hit buried at #2
    assert rank.head_is_strong([weak, strong], terms) is False
    # empty pool / empty terms → never "strong"
    assert rank.head_is_strong([], terms) is False
    assert rank.head_is_strong([strong], []) is False


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


def test_rank_summary_yields_to_verbatim_evidence() -> None:
    """A stored summary is derived prose: at equal match it ranks below the
    verbatim record — user text and even tool output — while staying in the
    results (it's still the only doc carrying synthesis vocabulary)."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    summary = _hit(1, "reindex the store", ct="summary", occurred_at=now)
    user = _hit(2, "reindex the store", ct="user", occurred_at=now)
    tool_result = _hit(3, "reindex the store", ct="tool_result", occurred_at=now)
    ranked = rank.rank_search_results([summary, user, tool_result], ["reindex"], 3, now=now)
    assert [h["content_type"] for h in ranked] == ["user", "tool_result", "summary"]


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
class _ScriptedScorer:
    """A stand-in for a loaded CrossEncoder: records each (query, doc) pool it is
    handed, and returns scripted scores (a flat 0.0 by default, which leaves the
    caller's order intact)."""

    def __init__(self, scores=None) -> None:
        self.scores = scores
        self.pools: list[list] = []

    def predict(self, pairs, batch_size, show_progress_bar):
        self.pools.append(list(pairs))
        return self.scores if self.scores is not None else [0.0] * len(pairs)


def _unloadable() -> object:
    raise RuntimeError("no weights on disk")


def _live_reranker(monkeypatch, scores=None):
    """A real Reranker over a scripted cross-encoder — the product's own scoring
    and reordering run. Clears the suite's model-free pin, which stands every
    reranker down by design."""
    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)
    scorer = _ScriptedScorer(scores)
    return rerank.Reranker(model=scorer), scorer


def test_rerank_reorders_by_scores(monkeypatch) -> None:
    # Scripted model scores: item B most relevant, then C, then A.
    r, scorer = _live_reranker(monkeypatch, scores=[0.1, 0.9, 0.5])
    assert r.rerank("q", ["A", "B", "C"], get_text=lambda x: x) == ["B", "C", "A"]
    assert scorer.pools[0] == [["q", "A"], ["q", "B"], ["q", "C"]]


def test_rerank_fail_soft_returns_none(monkeypatch) -> None:
    # Reranker unavailable / errored → None, so the caller keeps its own order.
    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)
    broken = rerank.Reranker(load=_unloadable)
    assert broken.rerank("q", ["A", "B"], get_text=lambda x: x) is None
    # empty input is None too
    assert broken.rerank("q", [], get_text=lambda x: x) is None


def test_rerank_scores_none_without_model(monkeypatch) -> None:
    # No query / no docs short-circuits before any model load.
    r = rerank.Reranker(load=lambda: pytest.fail("model load attempted"))
    assert r.rerank_scores("", ["a"]) is None
    assert r.rerank_scores("q", []) is None


# ── the full gate through search() (model-free, real store) ──────────────────
def _seed_one_thread(archive_home, user_text: str) -> None:
    import json

    from thread_archive._importers import import_session_incremental
    from thread_archive._store import init_db

    init_db()
    f = archive_home / "s.jsonl"
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
         "sessionId": "s", "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "ok."}]}},
    ]
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    import_session_incremental(f, "proj:s")


def test_search_skips_rerank_on_strong_lexical_head(archive_home, monkeypatch) -> None:
    # Every query term lands literally in the seeded doc → the ranked head is
    # strong → the cross-encoder must not be consulted, and the hit must carry
    # the lexical verdict (_did_rerank False), not the by-meaning one.
    from thread_archive._retrieval import search

    reranker, scorer = _live_reranker(monkeypatch)
    _seed_one_thread(archive_home, "the launchd supervisor restarted the watcher daemon")
    hits = search("watcher daemon restarted", reranker=reranker)
    assert hits and scorer.pools == []
    assert hits[0]["_did_rerank"] is False


def test_search_reranks_on_weak_lexical_head(archive_home, monkeypatch) -> None:
    # Only one of three terms lands (below the strong floor of 2) → the head is
    # weak — the vocab-mismatch shape the cross-encoder exists for — so it runs.
    # Auto-re-rank ships off (latency); rerank_auto=True exercises the gate here.
    from thread_archive._retrieval import SearchParams, search

    reranker, scorer = _live_reranker(monkeypatch)
    _seed_one_thread(archive_home, "the watcher process stopped overnight")
    hits = search("watcher vanishing mysteriously", reranker=reranker,
                  params=SearchParams(rerank_auto=True))
    assert hits and scorer.pools, "weak head should have gone through the cross-encoder"
    assert hits[0]["_did_rerank"] is True
    # The pipeline scores the query against a window of each candidate's text.
    assert all(pair[0] == "watcher vanishing mysteriously" for pair in scorer.pools[0])


def test_search_rerank_true_overrides_the_strong_head_skip(archive_home, monkeypatch) -> None:
    # An explicit rerank=True is a caller decision — it bypasses the shape gate
    # AND the strong-head skip (warm_models depends on this to prime the model).
    from thread_archive._retrieval import search

    reranker, scorer = _live_reranker(monkeypatch)
    _seed_one_thread(archive_home, "the launchd supervisor restarted the watcher daemon")
    hits = search("watcher daemon restarted", rerank=True, reranker=reranker)
    assert hits and scorer.pools
    assert hits[0]["_did_rerank"] is True


def test_search_stands_the_reranker_down_when_it_is_unavailable(archive_home, monkeypatch) -> None:
    # The gate consults the reranker it was given: an unavailable one sits out even
    # on the weak head that would otherwise trip it, and search still returns hits.
    from thread_archive._retrieval import search

    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)
    scorer = _ScriptedScorer()
    down = rerank.Reranker(model=scorer)
    monkeypatch.setenv("THREAD_ARCHIVE_RERANK", "off")  # the operator switch
    _seed_one_thread(archive_home, "the watcher process stopped overnight")
    hits = search("watcher vanishing mysteriously", reranker=down)
    assert hits and scorer.pools == []
    assert hits[0]["_did_rerank"] is False


# ── warm() preload contract (model-free) ─────────────────────────────────────
def test_warm_skips_the_loader_when_switched_off(monkeypatch) -> None:
    # Models switched off → warm() reports False and never touches the heavy loader.
    for cls in (rerank.Reranker, embed.Embedder):
        model = cls(load=lambda: pytest.fail("heavy model loader was consulted"))
        assert model.warm() is False


def test_warm_reports_the_load_outcome(monkeypatch) -> None:
    # A model that loads → True; a load that fails → False (the arm degrades lazily).
    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED", raising=False)
    for cls in (rerank.Reranker, embed.Embedder):
        assert cls(model=object()).warm() is True  # already loaded
        broken = cls(load=_unloadable)
        assert broken.warm() is False
        assert broken.warm() is False  # the cached failure holds


def test_warm_models_never_raises(monkeypatch) -> None:
    # A stage that blows up must not propagate — warming is best-effort startup work.
    class _ExplodingModel:
        """A model whose availability probe itself fails — the shape a broken torch
        install takes at startup."""

        def is_available(self):
            raise RuntimeError("torch exploded")

        def warm(self):
            raise RuntimeError("torch exploded")

    warm_models(embedder=_ExplodingModel(), reranker=_ExplodingModel())


def test_warm_models_primes_both_stages(archive_home) -> None:
    # The startup path: each model is preloaded once, then the throwaway search runs
    # the pipeline end to end to fill the caches the first real query reuses.
    class _RecordingModel:
        """A model that counts preloads."""

        def __init__(self) -> None:
            self.warms = 0

        def is_available(self) -> bool:
            return True

        def warm(self) -> bool:
            self.warms += 1
            return True

    _seed_one_thread(archive_home, "the launchd supervisor restarted the watcher daemon")
    embedder, reranker = _RecordingModel(), _RecordingModel()
    warm_models(embedder=embedder, reranker=reranker)
    assert embedder.warms == 1
    assert reranker.warms == 1


def test_warm_models_defaults_to_the_process_models(archive_home) -> None:
    # The startup call the MCP server actually makes, with no arguments: it primes
    # the process models, which the model-free pin leaves stood down (so nothing
    # heavy loads), and completes without raising.
    _seed_one_thread(archive_home, "the launchd supervisor restarted the watcher daemon")
    assert warm_models() is None
    assert embed.is_available() is False
    assert rerank.is_available() is False



def test_warm_models_primes_search_before_it_builds_the_graph(archive_home, monkeypatch) -> None:
    """Order is the whole point of the warm pass. The corpus graph is its longest
    stage and the only one no search blocks on — the coherence re-rank serves what is
    cached and leaves the ranking alone when nothing is — so building it first leaves
    every query arriving in that window paying full cold-search latency, which is the
    cost this function exists to move off the request path.

    Read off the ledger row the pass writes: its stage keys are recorded as the
    stages complete, so their order in the record is the order they ran in."""
    import json

    from thread_archive._config import resolve_paths

    monkeypatch.setenv("THREAD_ARCHIVE_COHERENCE", "on")  # else the graph stage sits out
    _seed_one_thread(archive_home, "the launchd supervisor restarted the watcher daemon")
    warm_models()

    ledger = resolve_paths().home / "retrieval-usage.jsonl"
    warms = [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]
    stages = list(next(r for r in reversed(warms) if r.get("kind") == "warm"))
    assert stages.index("search_ms") < stages.index("graph_ms")
