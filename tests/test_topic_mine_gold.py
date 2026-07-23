"""Unit coverage for the topic gold-miner's pure logic.

The two agent stages (survey + per-angle labelers) are operator-run and cost real
tokens; what needs coverage is the logic around them — survey/label verdict
parsing (angles + candidates), gold derived from grade-2 (never trusted from the
agent), the case assembly and its snapshot binding, topic resolution by name/id,
and that a labeler's brief carries the survey's candidate threads for its angle
(the labeler builds on them, it doesn't rediscover blind).
"""

from __future__ import annotations

import pytest

from thread_archive._mine import topic_mined as topic_mine


# ── parse_survey ─────────────────────────────────────────────────────────────

def test_parse_survey_keeps_angles_facets_and_candidates():
    s = topic_mine.parse_survey(
        'here you go: {"facets": [{"facet": "A", "rough_volume": "large", '
        '"example_thread_ids": ["T1"]}], "angles": ['
        '{"query": "q1", "intent": "A", "confounds": ["B", "C"], '
        '"candidates": [{"thread_id": "T1", "note": "answers it"}, "T2"]}]}')
    assert s["facets"][0]["facet"] == "A"
    (a,) = s["angles"]
    assert a["query"] == "q1" and a["intent"] == "A" and a["confounds"] == ["B", "C"]
    # candidates normalize: object kept, bare id string coerced to {thread_id, note}
    assert a["candidates"] == [{"thread_id": "T1", "note": "answers it"},
                               {"thread_id": "T2", "note": ""}]


def test_parse_survey_drops_malformed_angles():
    s = topic_mine.parse_survey(
        '{"angles": [{"query": "ok", "intent": "A"}, '
        '{"query": "", "intent": "A"}, '                  # empty query
        '{"intent": "A"}, '                               # no query
        '{"query": "x"}]}')                               # no intent
    assert [a["query"] for a in s["angles"]] == ["ok"]
    assert s["angles"][0]["confounds"] == [] and s["angles"][0]["candidates"] == []


def test_parse_survey_rejects_no_angles_or_bad_json():
    assert topic_mine.parse_survey('{"facets": []}') is None       # no angles key
    assert topic_mine.parse_survey('{"angles": []}') is None       # all dropped
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
    angle = {"query": "q", "intent": "A", "confounds": ["B"], "_topic": "suicide"}
    labels = {"grades": {"T1": 2, "T2": 0}, "gold": ["T1"], "reasons": {}}
    row = topic_mine.case_from_labels(angle, labels, "snap-xyz")
    assert row["gold"] == ["T1"]
    assert row["grades"] == {"T1": 2, "T2": 0}   # full graded pool travels for nDCG
    assert row["sessions"] == []                 # authored query, no session to skip
    assert row["snapshot_id"] == "snap-xyz"
    assert row["protocol"] == "topic-mined"
    assert row["topic"] == "suicide"
    assert row["intent"] == "A"


def test_case_from_labels_drops_a_pool_with_no_gold():
    angle = {"query": "q", "intent": "A", "confounds": []}
    assert topic_mine.case_from_labels(angle, {"grades": {"T": 0}, "gold": []}, "s") is None


# ── build prompts: informed labeler + content ────────────────────────────────

def test_label_prompt_carries_the_survey_candidates():
    # The labeler is NOT blind: it gets the survey's candidate threads for the
    # angle as a starting pool to verify and expand — better gold than blind
    # rediscovery, and no leakage (the labeler isn't the system under test).
    angle = {"query": "should AI be allowed to choose death",
             "intent": "AI right-to-die", "confounds": ["bot-death grief"],
             "candidates": [{"thread_id": "01SURVEYHIT", "note": "explicit right-to-die"}]}
    p = topic_mine.build_label_prompt(angle, "py tool")
    assert "should AI be allowed to choose death" in p
    assert "AI right-to-die" in p
    assert "bot-death grief" in p              # confounds named as grade-0 traps
    assert "01SURVEYHIT" in p                  # the survey's candidate id IS handed over
    assert "explicit right-to-die" in p        # with its note


def test_label_prompt_handles_no_candidates():
    angle = {"query": "q", "intent": "A", "confounds": [], "candidates": []}
    p = topic_mine.build_label_prompt(angle, "py tool")
    assert "discover them yourself" in p


def test_survey_prompt_seeds_topic_and_members():
    topic = {"id": "01TOPIC", "title": "suicide", "description": "the neutral hub",
             "citation_count": 3,
             "member_threads": [{"thread_id": "01MEM1", "title": "a real thread"}]}
    p = topic_mine.build_survey_prompt(topic, "py tool")
    assert "suicide" in p and "the neutral hub" in p
    assert "01MEM1" in p and "a real thread" in p
    assert "no target count" in p  # the agent decides how many angles


# ── slugify ──────────────────────────────────────────────────────────────────

def test_slugify():
    assert topic_mine.slugify("Anna's Auth / Rewrite!!") == "anna-s-auth-rewrite"
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
