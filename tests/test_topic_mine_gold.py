"""Unit coverage for the topic gold-miner's pure logic.

The two agents (survey + blind labelers) are operator-run and cost real tokens;
what needs coverage is the logic around them — survey/label verdict parsing,
gold derived from grade-2 (never trusted from the agent), the case assembly and
its snapshot binding, topic resolution by name/id, and the blindness property
(a labeler's brief never carries the survey agent's thread ids).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_EVALS = Path(__file__).resolve().parent.parent / "evals"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _EVALS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


topic_mine = _load("topic_mine_gold")


# ── parse_survey ─────────────────────────────────────────────────────────────

def test_parse_survey_keeps_wellformed_queries_and_facets():
    s = topic_mine.parse_survey(
        'here you go: {"facets": [{"facet": "A", "rough_volume": "large", '
        '"example_thread_ids": ["T1"]}], "queries": ['
        '{"query": "q1", "intended_facet": "A", "confounds": ["B", "C"]}]}')
    assert s["facets"][0]["facet"] == "A"
    assert s["queries"] == [{"query": "q1", "intended_facet": "A", "confounds": ["B", "C"]}]


def test_parse_survey_drops_malformed_queries():
    s = topic_mine.parse_survey(
        '{"queries": [{"query": "ok", "intended_facet": "A"}, '
        '{"query": "", "intended_facet": "A"}, '          # empty query
        '{"intended_facet": "A"}, '                       # no query
        '{"query": "x"}]}')                               # no facet
    assert [q["query"] for q in s["queries"]] == ["ok"]
    assert s["queries"][0]["confounds"] == []  # absent confounds default to []


def test_parse_survey_rejects_no_queries_or_bad_json():
    assert topic_mine.parse_survey('{"facets": []}') is None       # no queries key
    assert topic_mine.parse_survey('{"queries": []}') is None      # all dropped
    assert topic_mine.parse_survey("not json") is None


# ── parse_labels ─────────────────────────────────────────────────────────────

def test_parse_labels_coerces_grades_and_derives_gold():
    v = topic_mine.parse_labels(
        '{"grades": {"T1": 2, "T2": 0, "T3": 1, "T4": 2, "T5": 7, "T6": "x"}, '
        '"reasons": {"T1": "answers it", "T2": 3}}')
    assert v["grades"] == {"T1": 2, "T2": 0, "T3": 1, "T4": 2}  # 7 and "x" dropped
    assert v["gold"] == ["T1", "T4"]                            # grade-2, sorted
    assert v["reasons"] == {"T1": "answers it"}                 # non-string reason dropped


def test_parse_labels_no_grade_two_is_empty_gold():
    v = topic_mine.parse_labels('{"grades": {"T1": 0, "T2": 1}}')
    assert v["gold"] == []


def test_parse_labels_rejects_bad_shapes():
    assert topic_mine.parse_labels('{"reasons": {}}') is None    # no grades
    assert topic_mine.parse_labels("junk") is None


# ── case assembly + snapshot binding ─────────────────────────────────────────

def test_case_from_labels_builds_a_snapshot_bound_case():
    query = {"query": "q", "intended_facet": "A", "confounds": ["B"], "_topic": "suicide"}
    labels = {"grades": {"T1": 2, "T2": 0}, "gold": ["T1"], "reasons": {}}
    row = topic_mine.case_from_labels(query, labels, "snap-xyz")
    assert row["gold"] == ["T1"]
    assert row["grades"] == {"T1": 2, "T2": 0}   # full graded pool travels for nDCG
    assert row["sessions"] == []                 # authored query, no session to skip
    assert row["snapshot_id"] == "snap-xyz"
    assert row["protocol"] == "topic-mined"
    assert row["topic"] == "suicide"


def test_case_from_labels_drops_a_pool_with_no_gold():
    query = {"query": "q", "intended_facet": "A", "confounds": []}
    assert topic_mine.case_from_labels(query, {"grades": {"T": 0}, "gold": []}, "s") is None


# ── build prompts: blindness + content ───────────────────────────────────────

def test_label_prompt_is_blind_to_survey_gold_ids():
    # The labeler sees the query and its intent, never the survey agent's ids —
    # rediscovery is blind, so a case isn't "did search find what agent A had".
    query = {"query": "should AI be allowed to choose death",
             "intended_facet": "AI right-to-die", "confounds": ["bot-death grief"]}
    p = topic_mine.build_label_prompt(query, "py tool")
    assert "should AI be allowed to choose death" in p
    assert "AI right-to-die" in p
    assert "bot-death grief" in p          # confounds named as grade-0 traps
    assert "01" not in p                   # no ULID-shaped gold leaked in


def test_survey_prompt_seeds_topic_and_members():
    topic = {"id": "01TOPIC", "title": "suicide", "description": "the neutral hub",
             "citation_count": 3,
             "member_threads": [{"thread_id": "01MEM1", "title": "a real thread"}]}
    p = topic_mine.build_survey_prompt(topic, "py tool", n_queries=8)
    assert "suicide" in p and "the neutral hub" in p
    assert "01MEM1" in p and "a real thread" in p


# ── slugify ──────────────────────────────────────────────────────────────────

def test_slugify():
    assert topic_mine.slugify("Ella's Suicide / Ideation!!") == "ella-s-suicide-ideation"
    assert topic_mine.slugify("   ") == "topic"


# ── resolve_topic (over a seeded store) ──────────────────────────────────────

_seed_n = iter(range(1000))


def _seed_topic(title: str, *, archived: bool = False) -> str:
    # name/source_id are unique (the store enforces it); title is the match key
    # the resolver keys on, so distinct topics can legitimately share a title.
    from thread_archive._store import Thread, get_session
    uid = next(_seed_n)
    with get_session() as s:
        t = Thread(name=f"topic:{title}:{uid}", title=title, thread_type="topic",
                   source="cc", source_id=f"t:{title}:{uid}", archived=archived)
        s.add(t)
        s.flush()
        tid = t.id
        s.commit()
    return tid


def test_resolve_topic_by_id_and_exact_name(archive_home):
    from thread_archive._store import init_db
    init_db()
    tid = _seed_topic("suicide")
    assert topic_mine.resolve_topic(tid)["id"] == tid            # direct id
    assert topic_mine.resolve_topic("SUICIDE")["id"] == tid      # case-insensitive exact


def test_resolve_topic_unique_substring(archive_home):
    from thread_archive._store import init_db
    init_db()
    tid = _seed_topic("chronic suicidality")
    assert topic_mine.resolve_topic("chronic")["id"] == tid


def test_resolve_topic_ambiguous_and_missing_raise(archive_home):
    from thread_archive._store import init_db
    init_db()
    _seed_topic("suicide ideation")
    _seed_topic("suicide risk")
    with pytest.raises(SystemExit, match="matches 2 topics"):
        topic_mine.resolve_topic("suicide")
    with pytest.raises(SystemExit, match="no live topic"):
        topic_mine.resolve_topic("nonexistent-subject")


def test_resolve_topic_ignores_archived(archive_home):
    from thread_archive._store import init_db
    init_db()
    live = _seed_topic("grief")
    _seed_topic("grief", archived=True)  # a retired dupe must not create ambiguity
    assert topic_mine.resolve_topic("grief")["id"] == live
