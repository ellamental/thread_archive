"""Unit coverage for the retrieval eval harness's pure logic.

The harness (``scripts/retrieval_eval.py``) runs against the live archive in
the CI retrieval-gate rows; what needs test coverage is the logic that turns
raw data into scores — the log-miner's tool-name classifier and search->read
pairing rules, and the multi-gold / session-skip scoring loop. The DB-touching
paths (case sampling, the mining SQL) are exercised by the gate rows
themselves.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_eval",
    Path(__file__).resolve().parent.parent / "scripts" / "retrieval_eval.py",
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
        "mcp__plugin_archive-librarian_thread-archive-librarian__thread_user_messages",
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

def _fake_hits(*thread_ids):
    return [{"thread_id": t} for t in thread_ids]


def test_evaluate_rank_is_first_gold_hit(monkeypatch):
    monkeypatch.setattr(retrieval_eval.api, "search",
                        lambda q, **kw: _fake_hits(9, 5, 3))
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [3, 5], "sessions": []}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None)
    assert report["mrr"] == 0.5  # first gold (5) at rank 2
    assert report["recall"][1] == 0.0
    assert report["recall"][5] == 1.0


def test_evaluate_skips_originating_session_hits(monkeypatch):
    # The session that issued the query quotes it verbatim and would
    # otherwise outrank the real gold.
    monkeypatch.setattr(retrieval_eval.api, "search",
                        lambda q, **kw: _fake_hits(42, 5))
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [5], "sessions": [42]}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None)
    assert report["mrr"] == 1.0


def test_evaluate_miss_scores_zero(monkeypatch):
    monkeypatch.setattr(retrieval_eval.api, "search",
                        lambda q, **kw: _fake_hits(1, 2, 3))
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [99], "sessions": []}],
        limit=20, rerank=False, content_type=None, exclude_content_types=None)
    assert report["mrr"] == 0.0
    assert all(v == 0.0 for v in report["recall"].values())


def test_evaluate_respects_limit_after_skips(monkeypatch):
    # Gold sits just past the limit once the session hit is skipped: no credit.
    monkeypatch.setattr(retrieval_eval.api, "search",
                        lambda q, **kw: _fake_hits(42, 1, 2, 99))
    report = retrieval_eval.evaluate(
        [{"query": "q", "gold": [99], "sessions": [42]}],
        limit=2, rerank=False, content_type=None, exclude_content_types=None)
    assert report["mrr"] == 0.0


def test_evaluate_passes_scope_exclusions_through(monkeypatch):
    seen = {}

    def spy(q, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(retrieval_eval.api, "search", spy)
    retrieval_eval.evaluate(
        [{"query": "q", "gold": [1], "sessions": []}],
        limit=5, rerank=False, content_type=None,
        exclude_content_types=retrieval_eval.EXCLUDE_META)
    assert seen["exclude_content_types"] == retrieval_eval.EXCLUDE_META

    retrieval_eval.evaluate(
        [{"query": "q", "gold": [1], "sessions": []}],
        limit=5, rerank=False, content_type=None, exclude_content_types=None)
    assert seen["exclude_content_types"] is None
