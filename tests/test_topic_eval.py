"""Unit coverage for the topic-tool click protocol's pure logic.

Same philosophy as the thread eval's tests: the DB-touching paths (mining SQL,
ref resolution, the live-topic filter) are exercised by running the harness
against the live archive; what needs unit coverage is the logic that turns a
trail into outcomes — tool classification, ref extraction, the per-search
outcome pairing (found / created / nothing), and the scoring loop.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "topic_eval",
    Path(__file__).resolve().parent.parent / "scripts" / "topic_eval.py",
)
topic_eval = importlib.util.module_from_spec(_SPEC)
sys.modules["topic_eval"] = topic_eval
_SPEC.loader.exec_module(topic_eval)


# ── classify_tool / extract_refs ─────────────────────────────────────────────

def test_classify_accepts_librarian_and_legacy_families():
    for name, kind in [
        ("mcp__thread-archive-librarian__topic_search", "search"),
        ("mcp__plugin_archive-librarian_thread-archive-librarian__topic_get", "open"),
        ("mcp__thread-archive-librarian__topic_create", "create"),
        ("mcp__thread-archive-librarian__topic_cite", "open"),
        ("mcp__thread-archive-librarian__topic_merge", "open"),
        ("thread-commands:topic_search", "search"),
        ("mcp__thread-commands__topic_read", "open"),
        ("mcp__thread-commands__topic_update", "open"),
    ]:
        assert topic_eval.classify_tool(name) == kind, name


def test_classify_rejects_lookalikes():
    for name in [
        "topic_search",  # unnamespaced: other systems use this name too
        "topic_get",
        "mcp__thread-archive__thread_search",
        "mcp__other-kg__topic_search",
        None,
        "",
    ]:
        assert topic_eval.classify_tool(name) is None, name


def test_extract_refs_covers_all_id_fields_in_order():
    assert topic_eval.extract_refs(
        {"from_id": 2, "into_id": "01A", "quote": "x"}) == [2, "01A"]
    assert topic_eval.extract_refs({"topic_id": 7}) == [7]
    assert topic_eval.extract_refs({"source_id": 1, "target_id": 2}) == [1, 2]
    assert topic_eval.extract_refs({"title": "no ids"}) == []


# ── pair_topic_events ────────────────────────────────────────────────────────

def test_pairing_found_created_nothing():
    rows = topic_eval.pair_topic_events([
        ("S1", "search", "auth"),
        ("S1", "open", "T1"),          # auth: found T1
        ("S1", "search", "deploy"),
        ("S1", "create", None),        # deploy: created instead
        ("S1", "open", "T2"),          # ...opening the fresh topic: no credit
        ("S2", "search", "orphan"),    # orphan: nothing
    ])
    by_query = {r["query"]: r for r in rows}
    assert by_query["auth"]["outcome"] == "found"
    assert by_query["auth"]["gold"] == ["T1"]
    assert by_query["deploy"]["outcome"] == "created"
    assert by_query["orphan"]["outcome"] == "nothing"


def test_pairing_open_labels_most_recent_search_and_skips_seen():
    rows = topic_eval.pair_topic_events([
        ("S1", "open", "T1"),          # touched before any search: known
        ("S1", "search", "q1"),
        ("S1", "open", "T1"),          # already seen: not q1's find
        ("S1", "open", "T2"),
        ("S1", "search", "q2"),
        ("S1", "open", "T3"),
    ])
    by_query = {r["query"]: r for r in rows}
    assert by_query["q1"]["gold"] == ["T2"]
    assert by_query["q2"]["gold"] == ["T3"]


def test_pairing_create_after_open_keeps_found():
    # The create only marks searches that hadn't found anything yet.
    rows = topic_eval.pair_topic_events([
        ("S1", "search", "q"),
        ("S1", "open", "T1"),
        ("S1", "create", None),
    ])
    assert rows[0]["outcome"] == "found"
    assert rows[0]["gold"] == ["T1"]


def test_pairing_sessions_are_independent():
    rows = topic_eval.pair_topic_events([
        ("S1", "search", "q"),
        ("S2", "open", "T9"),          # other session: not q's find
    ])
    assert rows[0]["outcome"] == "nothing"


# ── subject_uptake ───────────────────────────────────────────────────────────

_TYPES = {"T1": "topic", "T2": "topic", "C1": "conversation"}


def test_uptake_counts_topic_reads_that_follow_a_search():
    report = topic_eval.subject_uptake([
        ("S1", "search", "q1"),
        ("S1", "read", "C1"),          # conversation read: not uptake
        ("S1", "read", "T1"),          # topic read after q1: uptake
        ("S1", "read", "T2"),          # second topic read: same search window
        ("S1", "search", "q2"),        # no topic read follows
    ], _TYPES)
    assert report["n_searches"] == 2
    assert report["searches_with_topic_read"] == 1
    assert report["uptake_rate"] == 0.5
    assert report["topic_reads_after_search"] == 2
    assert report["sessions_with_uptake"] == 1


def test_uptake_topic_read_without_prior_search_is_cold():
    report = topic_eval.subject_uptake([
        ("S1", "read", "T1"),          # tree navigation / remembered topic
        ("S2", "search", "q"),
    ], _TYPES)
    assert report["topic_reads_cold"] == 1
    assert report["topic_reads_after_search"] == 0
    assert report["searches_with_topic_read"] == 0


def test_uptake_sessions_are_independent():
    report = topic_eval.subject_uptake([
        ("S1", "search", "q"),
        ("S2", "read", "T1"),          # other session: cold there, not uptake
    ], _TYPES)
    assert report["searches_with_topic_read"] == 0
    assert report["topic_reads_cold"] == 1


def test_uptake_empty_trail():
    report = topic_eval.subject_uptake([], {})
    assert report["n_searches"] == 0
    assert report["uptake_rate"] == 0.0


# ── evaluate ─────────────────────────────────────────────────────────────────

def test_evaluate_ranks_first_gold_hit():
    report = topic_eval.evaluate(
        [{"query": "q", "gold": ["T3", "T5"]}],
        lambda q, limit: ["T9", "T5", "T3"], limit=20)
    assert report["mrr"] == 0.5
    assert report["recall"][1] == 0.0
    assert report["recall"][5] == 1.0


def test_evaluate_miss_scores_zero():
    report = topic_eval.evaluate(
        [{"query": "q", "gold": ["T1"]}], lambda q, limit: ["T2"], limit=20)
    assert report["mrr"] == 0.0
    assert all(v == 0.0 for v in report["recall"].values())
