"""Unit coverage for the retrieval eval harness's pure logic.

The harness (``search_lab/retrieval_eval.py``) runs against the live archive (the
CI retrieval-gate row runs its ``--probes-only`` mode); what needs test
coverage is the logic that turns raw data into scores — the log-miner's
tool-name classifier and search->read pairing rules, and the multi-gold /
session-skip scoring loop.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_eval",
    Path(__file__).resolve().parent.parent / "search_lab" / "retrieval_eval.py",
)
retrieval_eval = importlib.util.module_from_spec(_SPEC)
sys.modules["retrieval_eval"] = retrieval_eval
_SPEC.loader.exec_module(retrieval_eval)


# ── classify_tool ────────────────────────────────────────────────────────────

def test_classify_tool_accepts_archive_and_legacy_families():
    for name, kind in [
        ("mcp__thread-archive__thread_search", "search"),
        ("mcp__thread-archive__thread_read", "read"),
        ("mcp_thread-archive_thread_search", "search"),
        ("mcp__thread-commands__thread_read", "read"),
        ("mcp__thread_commands__thread_search", "search"),
        ("thread-commands:thread_search", "search"),
        ("thread-commands_thread_read", "read"),
    ]:
        assert retrieval_eval.classify_tool(name) == kind, name


def test_classify_tool_rejects_lookalikes():
    for name in [
        "thread_search",  # unnamespaced: other systems use this name too
        "thread-search",
        "thread_read",
        "mcp__thread-commands__topic_search",
        "mcp__thread-commands__knowledge_search",
        "mcp__plugin_other-plugin_thread-other__thread_user_messages",
        "mcp__thread-commands__thread_recent",
        None,
        "",
    ]:
        assert retrieval_eval.classify_tool(name) is None, name


# ── resolve_read_refs ────────────────────────────────────────────────────────

def test_resolve_read_refs_canonicalizes_and_drops_unresolvable():
    table = {"3024": "01AAAAAAAAAAAAAAAAAAAAAAAA", "sess-uuid-7": "01BBBBBBBBBBBBBBBBBBBBBBBB"}
    events = [
        ("S1", "search", "q"),
        ("S1", "read", 3024),            # legacy int
        ("S1", "read", "sess-uuid-7"),   # provider session id
        ("S1", "read", 999999),          # resolves to nothing: dropped
    ]
    out = retrieval_eval.resolve_read_refs(events, lambda r: table.get(str(r)))
    assert out == [
        ("S1", "search", "q"),
        ("S1", "read", "01AAAAAAAAAAAAAAAAAAAAAAAA"),
        ("S1", "read", "01BBBBBBBBBBBBBBBBBBBBBBBB"),
    ]


def test_resolve_read_refs_enables_self_session_exclusion():
    # A read of the session's own thread must canonicalize to the session id
    # so pair_log_events can exclude it.
    ulid = "01CCCCCCCCCCCCCCCCCCCCCCCC"
    events = retrieval_eval.resolve_read_refs(
        [(ulid, "search", "q"), (ulid, "read", 555)], lambda r: ulid)
    assert retrieval_eval.pair_log_events(events) == []


# ── pair_log_events ──────────────────────────────────────────────────────────

def test_pairing_attributes_reads_to_most_recent_search():
    cases = retrieval_eval.pair_log_events([
        (1, "search", "first query"),
        (1, "read", 101),
        (1, "search", "second query"),
        (1, "read", 202),
        (1, "read", 203),
    ])
    by_query = {c["query"]: c for c in cases}
    assert by_query["first query"]["gold"] == [101]
    assert by_query["second query"]["gold"] == [202, 203]
    assert by_query["first query"]["sessions"] == [1]


def test_pairing_drops_reads_with_no_prior_search():
    assert retrieval_eval.pair_log_events([(1, "read", 101)]) == []


def test_pairing_drops_searches_with_no_reads():
    assert retrieval_eval.pair_log_events([(1, "search", "nothing followed")]) == []


def test_pairing_excludes_the_session_itself():
    assert retrieval_eval.pair_log_events([
        (7, "search", "q"),
        (7, "read", 7),
    ]) == []


def test_pairing_excludes_threads_already_read_before_the_search():
    # The agent opened 101 before searching — the search didn't find it.
    cases = retrieval_eval.pair_log_events([
        (1, "read", 101),
        (1, "search", "q"),
        (1, "read", 101),
        (1, "read", 102),
    ])
    assert cases == [{"query": "q", "gold": [102], "sessions": [1]}]


def test_pairing_keeps_sessions_independent():
    cases = retrieval_eval.pair_log_events([
        (1, "search", "q"),
        (2, "read", 555),  # different session: not q's click
        (1, "read", 100),
    ])
    assert cases == [{"query": "q", "gold": [100], "sessions": [1]}]


# ── behavior_report ──────────────────────────────────────────────────────────

def test_behavior_outcomes_clicked_reformulated_abandoned():
    report = retrieval_eval.behavior_report([
        ("S1", "search", "q1"),
        ("S1", "read", "T1"),      # q1: clicked
        ("S1", "search", "q2"),
        ("S1", "search", "q3"),    # q2: reformulated (no click before next search)
        ("S1", "read", "T2"),
        ("S1", "read", "T3"),      # q3: clicked, 2 reads
        ("S2", "search", "q4"),    # q4: abandoned (session trail ends)
    ])
    assert report["n_searches"] == 4
    assert report["n_sessions"] == 2
    assert report["clicked"] == 2
    assert report["reformulated"] == 1
    assert report["abandoned"] == 1
    assert report["reads_per_click"] == 1.5


def test_behavior_self_and_repeat_reads_are_not_clicks():
    report = retrieval_eval.behavior_report([
        ("S1", "read", "T1"),      # pre-search read: T1 is known
        ("S1", "search", "q1"),
        ("S1", "read", "S1"),      # own session
        ("S1", "read", "T1"),      # already seen
    ])
    assert report["clicked"] == 0
    assert report["abandoned"] == 1


def test_behavior_reads_without_search_are_ignored():
    report = retrieval_eval.behavior_report([("S1", "read", "T1")])
    assert report["n_searches"] == 0
    assert report["n_sessions"] == 0
    assert report["click_rate"] == 0.0


# ── evaluate: multi-gold, session-skip scoring ───────────────────────────────

def _ranker(*thread_ids):
    """A search function that returns this ranking for any query — the harness scores
    whatever ranker it is handed, so a scripted one exercises the scoring loop."""
    def search(query, **kw):
        return [{"thread_id": t} for t in thread_ids]
    return search


def test_evaluate_rank_is_first_gold_hit():
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [3, 5], "sessions": []}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None,
        search=_ranker(9, 5, 7))
    assert report["mrr"] == 0.5  # first gold (5) at rank 2
    assert report["success"][1] == 0.0
    assert report["success"][5] == 1.0
    assert report["recall"][1] == 0.0
    assert report["recall"][5] == 0.5  # one of two relevant threads recovered


def test_evaluate_true_recall_averages_per_case_gold_coverage():
    report = retrieval_eval.evaluate(
        [
            {"query": "q1", "gold": ["A", "B"], "sessions": []},
            {"query": "q2", "gold": ["C"], "sessions": []},
        ],
        limit=2, rerank=False, content_type=None, exclude_content_types=None,
        search=_ranker("A", "C"),
    )
    # q1 recovers 1/2 and q2 recovers 1/1: macro recall is (0.5 + 1.0) / 2.
    assert report["recall"][5] == 0.75
    assert report["success"][5] == 1.0


def test_evaluate_skips_originating_session_hits():
    # The session that issued the query quotes it verbatim and would
    # otherwise outrank the real gold.
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [5], "sessions": [42]}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None,
        search=_ranker(42, 5))
    assert report["mrr"] == 1.0


def test_evaluate_miss_scores_zero():
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [99], "sessions": []}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None,
        search=_ranker(1, 2, 3))
    assert report["mrr"] == 0.0
    assert all(v == 0.0 for v in report["success"].values())
    assert all(v == 0.0 for v in report["recall"].values())


def test_evaluate_respects_limit_after_skips():
    # Gold sits just past the limit once the session hit is skipped: no credit.
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [99], "sessions": [42]}],
        limit=2, rerank=False, content_type=None, exclude_content_types=None,
        search=_ranker(42, 1, 2, 99))
    assert report["mrr"] == 0.0


# ── nDCG: graded scoring over the candidate pool ─────────────────────────────

def test_ndcg_at_k_perfect_and_reversed():
    # Ideal order scores 1.0; the pool's own grades are the yardstick.
    assert retrieval_eval.ndcg_at_k([2, 1, 0], [2, 1, 0], 20) == pytest.approx(1.0)
    # Only rank-1 counted: a grade-0 doc on top scores nothing at k=1.
    assert retrieval_eval.ndcg_at_k([0, 2, 1], [2, 1, 0], 1) == 0.0
    # Empty pool (nothing relevant) is defined as 0.0, not a divide-by-zero.
    assert retrieval_eval.ndcg_at_k([0, 0], [0, 0], 10) == 0.0


def test_evaluate_ndcg_rewards_the_whole_graded_pool():
    # C(0) A(2) B(1): the gold (A) is at rank 2, but nDCG credits B's grade-1
    # at rank 3 too — the pool is scored, not just the one right answer.
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": ["A"], "sessions": [],
          "grades": {"A": 2, "B": 1, "C": 0}}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None,
        search=_ranker("C", "A", "B"))
    # DCG = 3/log2(3) + 1/log2(4); IDCG = 3/log2(2) + 1/log2(3).
    assert report["ndcg"][20] == pytest.approx(0.6590, abs=1e-4)
    assert report["ndcg"][1] == 0.0  # grade-0 doc at rank 1
    assert report["mrr"] == 0.5      # binary MRR unchanged: gold at rank 2


def test_evaluate_reports_per_difficulty_strata():
    # Cases that carry a difficulty are scored apart from the aggregate, so a
    # weak stratum (vague findability) can't hide inside an easy one (verbatim).
    report = retrieval_eval.evaluate(
        [{"query": "verb", "gold": ["A"], "sessions": [], "difficulty": "verbatim"},
         {"query": "vag-hit", "gold": ["A"], "sessions": [], "difficulty": "vague"},
         {"query": "vag-miss", "gold": ["Z"], "sessions": [], "difficulty": "vague"}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None,
        search=_ranker("A", "B"))
    pd = report["per_difficulty"]
    assert set(pd) == {"verbatim", "vague"}  # no bucket for difficulty-less cases
    assert pd["verbatim"] == {"n": 1, "mrr": 1.0, "success10": 1.0,
                              "recall10": 1.0, "ndcg10": 1.0}
    # vague: one hit at rank 1, one total miss → means of 0.5.
    assert pd["vague"]["n"] == 2
    assert pd["vague"]["mrr"] == 0.5 and pd["vague"]["success10"] == 0.5


def test_evaluate_has_no_strata_without_difficulty_labels():
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [5], "sessions": []}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None,
        search=_ranker(5))
    assert report["per_difficulty"] == {}


def test_evaluate_ndcg_falls_back_to_binary_without_a_pool():
    # No grades: gold stands in as binary relevance so nDCG stays defined for
    # the title/log protocols. Gold at rank 2 of a single-relevant pool.
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [5], "sessions": []}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None,
        search=_ranker(9, 5, 3))
    assert report["ndcg"][20] == pytest.approx(1.0 / math.log2(3), abs=1e-4)
    assert report["ndcg"][1] == 0.0


def test_evaluate_passes_scope_exclusions_through():
    seen = {}

    def recording_search(query, **kw):
        seen.update(kw)
        return []

    retrieval_eval.evaluate(
        [{"query": "q", "gold": [1], "sessions": []}],
        limit=5, rerank=False, content_type=None,
        exclude_content_types=retrieval_eval.EXCLUDE_META, search=recording_search)
    assert seen["exclude_content_types"] == retrieval_eval.EXCLUDE_META

    retrieval_eval.evaluate(
        [{"query": "q", "gold": [1], "sessions": []}],
        limit=5, rerank=False, content_type=None, exclude_content_types=None,
        search=recording_search)
    assert seen["exclude_content_types"] is None


def test_evaluate_scores_over_the_corpus_as_is_no_date_bound():
    # Determinism is the snapshot's job now, not a per-case bound: evaluate passes
    # the search no until/snapshot_id, whether or not a case carries an id.
    calls = []

    def recording_search(query, **kw):
        calls.append(kw)
        return []

    cases = [{"query": "q", "gold": [1], "sessions": [], "snapshot_id": "abc123"},
             {"query": "q2", "gold": [2], "sessions": []}]
    retrieval_eval.evaluate(
        cases, limit=5, rerank=False, content_type=None,
        exclude_content_types=None, search=recording_search)
    for kw in calls:
        assert "until" not in kw and "snapshot_id" not in kw


def test_evaluate_defaults_to_the_archives_own_search():
    """No ``search`` given means the harness measures the shipped pipeline."""
    report = retrieval_eval.evaluate(
        [], limit=5, rerank=False, content_type=None, exclude_content_types=None)
    assert report["n"] == 0


# ── rerank_probe (the --require-rerank liveness check) ───────────────────────
#
# Driven through a real Reranker with the scorer injected at its constructor
# seam, so the probe exercises the product's own availability gating and
# fail-soft scoring — only the torch weights are stood in for.

class _ScriptedScorer:
    def __init__(self, scores):
        self.scores = scores

    def predict(self, pairs, batch_size, show_progress_bar):
        return self.scores


def _reranker(monkeypatch, scores):
    from thread_archive._retrieval import rerank

    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)
    return rerank.Reranker(model=_ScriptedScorer(scores))


def test_rerank_probe_passes_a_discriminating_model(monkeypatch):
    assert retrieval_eval.rerank_probe(_reranker(monkeypatch, [0.9, 0.1])) is None


def test_rerank_probe_breaches_when_arm_is_switched_off(monkeypatch):
    from thread_archive._retrieval import rerank

    monkeypatch.setenv("THREAD_ARCHIVE_RERANK", "off")
    breach = retrieval_eval.rerank_probe(rerank.Reranker(model=_ScriptedScorer([0.9, 0.1])))
    assert breach is not None and "unavailable" in breach


def test_rerank_probe_breaches_when_the_model_cannot_load(monkeypatch):
    from thread_archive._retrieval import rerank

    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)

    def unloadable():
        raise RuntimeError("no weights on disk")

    breach = retrieval_eval.rerank_probe(rerank.Reranker(load=unloadable))
    assert breach is not None and "degraded" in breach


def test_rerank_probe_breaches_on_a_scrambled_model(monkeypatch):
    # Loaded, scoring, but ranks the decoy above the answer: the probe must
    # treat "alive but wrong" as dead — that is the silent production failure.
    breach = retrieval_eval.rerank_probe(_reranker(monkeypatch, [0.1, 0.9]))
    assert breach is not None and "discriminate" in breach


def test_rerank_probe_breaches_on_malformed_scores(monkeypatch):
    breach = retrieval_eval.rerank_probe(_reranker(monkeypatch, [0.9]))
    assert breach is not None and "malformed" in breach

    breach = retrieval_eval.rerank_probe(
        _reranker(monkeypatch, [float("nan"), 0.1]))
    assert breach is not None and "malformed" in breach


# --- early stop --------------------------------------------------------------


def _cases(n: int) -> list[dict]:
    return [{"query": f"q{i}", "gold": [i], "sessions": []} for i in range(n)]


def test_early_stop_halts_the_run_and_says_why():
    seen: list[int] = []

    def counting_ranker(query, **kw):
        seen.append(len(seen))
        return [{"thread_id": 0}]  # only case 0's gold ever ranks

    report = retrieval_eval.evaluate(
        _cases(10), limit=20, rerank=False, content_type=None,
        exclude_content_types=None, search=counting_ranker,
        early_stop=lambda p: "enough" if p.scored == 3 else None)
    assert report["aborted"] == "enough"
    assert report["scored"] == 3
    assert len(seen) == 3, "no search should run after the abort"


def test_an_aborted_report_averages_over_the_full_case_set():
    # Unscored cases count as zero, so the metrics are lower bounds rather than
    # averages over a prefix — which would read as ordinary numbers while being
    # computed on different cases.
    report = retrieval_eval.evaluate(
        _cases(10), limit=20, rerank=False, content_type=None,
        exclude_content_types=None, search=lambda q, **kw: [{"thread_id": 0}],
        early_stop=lambda p: "stop" if p.scored == 1 else None)
    assert report["n"] == 10
    assert report["mrr"] == 0.1  # one perfect case out of ten, not 1.0


def test_a_completed_run_reports_no_abort():
    report = retrieval_eval.evaluate(
        _cases(3), limit=20, rerank=False, content_type=None,
        exclude_content_types=None, search=lambda q, **kw: [{"thread_id": 0}],
        early_stop=lambda p: None)
    assert report["aborted"] is None
    assert report["scored"] == 3
    assert [c["query"] for c in report["per_case"]] == ["q0", "q1", "q2"]


def test_progress_bound_is_the_best_still_reachable():
    from search_lab.eval_core import EvalProgress

    p = EvalProgress(n=10, scored=4, sums={"mrr": 1.0}, query="q", case_rr=0.0)
    assert p.best_possible("mrr") == pytest.approx(0.7)  # 1.0 + 6 perfect, over 10
    done = EvalProgress(n=10, scored=10, sums={"mrr": 1.0}, query="q", case_rr=0.0)
    assert done.best_possible("mrr") == pytest.approx(0.1)


def test_evaluate_still_reports_latency_when_aborted():
    report = retrieval_eval.evaluate(
        _cases(10), limit=20, rerank=False, content_type=None,
        exclude_content_types=None, search=lambda q, **kw: [{"thread_id": 0}],
        early_stop=lambda p: "stop" if p.scored == 2 else None)
    assert report["latency_p50_ms"] >= 0.0
