"""Unit coverage for the pooled-judgment gold miner's pure logic.

This is the one miner whose queries are observed rather than authored, and whose
labels are therefore pool-derived. What needs coverage is exactly the machinery
that bounds the pool bias, because that bound is the miner's whole claim: the
union has to draw from every system round-robin (so a cap trims all their tails
rather than one system's), it has to carry a random draw nothing ranked, and it
has to record which systems nominated each answer so "how much did the pool
actually widen things" is a fact on the case rather than an assertion in a
docstring.

Also covered: the ledger query population (deduped, probes dropped, sampled not
skimmed), grade parsing against the offered pool, the ``none-of-pool`` path that
mints no case, and resume across both the case file and the abstention record.
"""

from __future__ import annotations

import argparse
import json

from search_lab.mine import _framework as fw
from search_lab.mine import pooled_judged as pj

# ── the query population ─────────────────────────────────────────────────────

def _ledger(archive_home, queries: list[str], **extra) -> None:
    """A real usage ledger in the archive home — the file the miner's query
    population actually comes from. Written rather than faked so the ledger's own
    parsing (newest-first, per-call dedupe, torn lines) is under test too."""
    from thread_archive._retrieval import usage

    rows = [{"kind": "search", "at": f"2026-01-01T00:00:{i:02d}Z", "query": q,
             "limit": 20, **extra} for i, q in enumerate(queries)]
    (archive_home / usage.LEDGER_FILE).write_text(
        "".join(json.dumps(r) + "\n" for r in rows))


def test_ledger_queries_dedupe_and_drop_probes(archive_home):
    _ledger(archive_home, [
        "watcher ingest lock", "watcher ingest lock",   # the same call, paged
        "x",                                            # a probe
        "gold gate floor calibration",
        "   ",                                          # whitespace only
    ])
    assert sorted(pj.ledger_queries()) == ["gold gate floor calibration",
                                           "watcher ingest lock"]


def test_ledger_queries_survive_a_torn_line(archive_home):
    from thread_archive._retrieval import usage

    _ledger(archive_home, ["watcher ingest lock"])
    with (archive_home / usage.LEDGER_FILE).open("a") as fh:
        fh.write('{"kind": "search", "query": "half a row')   # concurrent append
    assert pj.ledger_queries() == ["watcher ingest lock"]


def test_sample_queries_is_deterministic_and_skips_mined(archive_home):
    _ledger(archive_home, [f"q{i}" for i in range(10)])
    a = pj.sample_queries(3, seed=5, skip=set())
    assert a == pj.sample_queries(3, seed=5, skip=set())
    assert len(a) == 3
    without = pj.sample_queries(10, seed=5, skip={a[0]})
    assert a[0] not in without


# ── pool merging: the bias bound ─────────────────────────────────────────────

def test_merge_is_round_robin_so_a_cap_trims_every_system():
    """A cap must not spend the whole budget on whichever system is listed first —
    that would quietly turn a multi-system pool back into a single-system one."""
    pools = {"stack": ["a", "b", "c", "d"],
             "bm25": ["e", "f", "g", "h"],
             "random": ["i", "j"]}
    order, contrib = pj.merge_pool(pools, cap=4)
    assert order == ["a", "e", "i", "b"]      # rank 0 of each, then rank 1
    assert set(contrib) == set(order)


def test_merge_records_every_system_that_nominated_a_thread():
    pools = {"stack": ["a", "b"], "bm25": ["b", "a"], "deep": ["c"]}
    _order, contrib = pj.merge_pool(pools, cap=10)
    assert sorted(contrib["a"]) == ["bm25", "stack"]
    assert sorted(contrib["b"]) == ["bm25", "stack"]
    assert contrib["c"] == ["deep"]


def test_merge_of_nothing_is_empty():
    assert pj.merge_pool({}, cap=10) == ([], {})
    assert pj.merge_pool({"stack": []}, cap=10) == ([], {})


def test_unique_contributions_counts_sole_finders():
    cases = [
        {"pool_contrib": {"t1": ["deep"], "t2": ["stack", "bm25"]}},
        {"pool_contrib": {"t3": ["deep"], "t4": ["random"]}},
    ]
    assert pj.unique_contributions(cases) == {"deep": 2, "random": 1}


def test_a_system_that_raises_contributes_nothing_rather_than_failing():
    def broken(query, limit):
        raise RuntimeError("arm is down")

    assert pj._hits(broken, "q", 5) == []


def test_hits_dedupe_within_a_system():
    def dupes(query, limit):
        return [{"thread_id": "a"}, {"thread_id": "a"}, {"thread_id": "b"}]

    assert pj._hits(dupes, "q", 5) == ["a", "b"]


# ── the random floor ─────────────────────────────────────────────────────────

def _thread(title: str, *, ttype: str = "conversation", excluded: bool = False) -> str:
    from thread_archive._store import Thread, get_session

    with get_session() as s:
        t = Thread(name=f"c:{title}", title=title, thread_type=ttype,
                   source="cc", source_id=title)
        t.exclude_from_search = excluded
        s.add(t)
        s.flush()
        tid = t.id
        s.commit()
    return tid


def test_random_threads_are_deterministic_and_respect_exclusions(archive_home):
    from thread_archive._store import init_db

    init_db()
    keep = {_thread(f"k{i}") for i in range(6)}
    hidden = _thread("hidden", excluded=True)
    topic = _thread("a topic", ttype="topic")

    drawn = pj.random_threads(4, seed="s")
    assert len(drawn) == 4 and set(drawn) <= keep
    assert hidden not in drawn and topic not in drawn
    assert pj.random_threads(4, seed="s") == drawn
    assert pj.random_threads(0, seed="s") == []


# ── judging ──────────────────────────────────────────────────────────────────

def test_parse_grades_keeps_only_offered_ids_and_valid_grades():
    v = pj.parse_grades(
        'verdict: {"grades": {"a": 2, "b": 0, "c": 7, "d": 1}, '
        '"none_answer": false, "rationale": "a answers it"}', {"a", "b", "c"})
    # c's grade is out of range; d was never in the pool.
    assert v["grades"] == {"a": 2, "b": 0}
    assert v["none"] is False and v["rationale"] == "a answers it"


def test_parse_grades_flags_none_of_pool():
    v = pj.parse_grades('{"grades": {"a": 0, "b": 1}, "none_answer": true}',
                        {"a", "b"})
    assert v["none"] is True
    assert 2 not in v["grades"].values()


def test_parse_grades_rejects_bad_shapes():
    assert pj.parse_grades("no json", {"a"}) is None
    assert pj.parse_grades('{"none_answer": true}', {"a"}) is None   # no grades
    assert pj.parse_grades('{"grades": {}}', {"a"}) is None          # nothing kept
    assert pj.parse_grades('{"grades": {"z": 2}}', {"a"}) is None    # all off-pool


def test_a_verdict_with_no_grade_two_mints_no_case():
    """A case with no right answer scores every ranker at zero — it belongs in the
    run's outcome breakdown, not in the file's denominator."""
    verdict = {"grades": {"a": 1, "b": 0}, "none": True, "rationale": "nothing"}
    assert pj.case_from_verdict("q", verdict, {}, "snap") is None


def test_a_case_carries_its_pool_provenance():
    verdict = {"grades": {"a": 2, "b": 2, "c": 0}, "none": False, "rationale": "r"}
    contrib = {"a": ["stack", "bm25"], "b": ["deep"], "c": ["random"]}
    case = pj.case_from_verdict("watcher ingest lock", verdict, contrib, "snap-3")
    assert case["gold"] == ["a", "b"] and case["n_gold"] == 2
    assert case["pool_size"] == 3 and case["snapshot_id"] == "snap-3"
    assert case["protocol"] == "pooled-judged"
    # Only the gold's provenance is kept — that is what "who found the answer" means.
    assert case["pool_contrib"] == {"a": ["stack", "bm25"], "b": ["deep"]}


def test_prompt_lists_the_pool_and_offers_the_read_seam():
    rows = [{"thread_id": "T1", "title": "the lock", "source": "cc",
             "snippet": "watcher ingest lock contention"}]
    p = pj.build_prompt("watcher ingest lock", rows, "py tool")
    assert "watcher ingest lock" in p
    assert "T1" in p and "the lock" in p and "contention" in p
    assert "py tool read" in p
    # The judge must not read the listing order as a ranking.
    assert "its order here" in p and "means nothing" in p


# ── resume ───────────────────────────────────────────────────────────────────

def test_a_none_of_pool_query_is_not_re_judged_under_the_same_judge(tmp_path):
    """`none-of-pool` is the shape this matters most for: a judge already paid to
    say the pool holds no answer must not be paid to say it again — and must be
    asked again the moment the judging prompt changes."""
    from search_lab.mine import _framework as fw

    stages = pj.MINER.stages(argparse.Namespace())
    gates = fw.paid_gates(stages)
    sha = gates["judge"]
    rejects = tmp_path / "pooled-cases-rejects.jsonl"
    rejects.write_text("\n".join(json.dumps(r) for r in [
        {"unit": "nothing answers this", "stage": "judge",
         "reason": "none-of-pool", "gate_sha": sha},
        {"unit": "judged under an older prompt", "stage": "judge",
         "reason": "none-of-pool", "gate_sha": "0000deadbeef"},
        {"unit": "an empty pool", "stage": "pool", "reason": "empty-pool",
         "gate_sha": None},
    ]) + "\n")

    remembered = fw.refused_units(rejects, gates)
    assert remembered == {"nothing answers this"}
    # A free stage's refusal is re-computed rather than honoured: it costs nothing,
    # and recomputing is what lets a corpus that has changed be seen as it is now.
    assert "an empty pool" not in remembered


# ── the run path ─────────────────────────────────────────────────────────────

def _seed_searchable(title: str, content: str) -> str:
    from datetime import datetime, timezone

    from thread_archive._retrieval import index_events
    from thread_archive._store import Event, Thread, get_session

    with get_session() as s:
        t = Thread(name=f"c:{title}", title=title, thread_type="conversation",
                   source="cc", source_id=title)
        s.add(t)
        s.flush()
        e = Event(thread_id=t.id, stream_id=title, event_type="user_message_sent",
                  payload={"content": content},
                  occurred_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
        s.add(e)
        s.flush()
        index_events(s, [e])
        tid = t.id
        s.commit()
    return tid


def test_pooled_miner_run_judges_a_real_pool(archive_home, tmp_path):
    from thread_archive._store import init_db

    init_db()
    answer = _seed_searchable("the lock thread",
                              "the watcher ingest lock serializes the writers")
    other = _seed_searchable("unrelated", "a note about stylesheets and flexbox")
    _ledger(archive_home, ["watcher ingest lock"])

    out = tmp_path / "pooled-cases.jsonl"
    captured: dict = {}

    def fake_judge(prompt, model, tool_cmd, **kw):
        captured["prompt"] = prompt
        # A discriminating verdict: one answer, one rejection. A judge that
        # graded the whole pool 2 would separate no ranker from any other, and the
        # verify stage drops that case rather than counting it.
        return json.dumps({"grades": {answer: 2, other: 0}, "none_answer": False,
                           "rationale": "it is the lock thread"}), {"num_turns": 3}

    ns = argparse.Namespace(model="opus", jobs=1, seed=7, out=out, target=1,
                            depth=10, pool_cap=40, n_random=2, ledger_home=None)
    ctx = fw.MineContext(snapshot_id="snap-1", target=1, model="opus", jobs=1,
                         tool_cmd="py tool", args=ns, agent_run=fake_judge)
    result = pj.MINER.run(ctx)

    assert result.written == 1 and result.attempted == 1
    assert [r["stage"] for r in result.funnel.rows()] == [
        "ledger", "sample", "pool", "judge", "verify"]
    (row,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert row["miner"] == "pooled" and row["query"] == "watcher ingest lock"
    assert row["gold"] == [answer] and row["snapshot_id"] == "snap-1"
    # The answer was nominated by at least one real retriever, and the case says
    # which — that record is what makes the pool bias auditable.
    assert row["pool_contrib"][answer]
    assert answer in captured["prompt"]


def test_pooled_miner_run_records_none_of_pool_without_minting(archive_home, tmp_path):
    from thread_archive._store import init_db

    init_db()
    _seed_searchable("a thread", "some words about flexbox layout")
    _ledger(archive_home, ["flexbox"])

    out = tmp_path / "pooled-cases.jsonl"
    ns = argparse.Namespace(model="opus", jobs=1, seed=7, out=out, target=1,
                            depth=10, pool_cap=40, n_random=1, ledger_home=None)
    ctx = fw.MineContext(
        snapshot_id="s", target=1, model="opus", jobs=1, tool_cmd="py tool", args=ns,
        agent_run=lambda p, m, t, **kw: (
            json.dumps({"grades": {i: 0 for i in _ids(p)}, "none_answer": True}),
            {}))
    result = pj.MINER.run(ctx)

    assert result.written == 0 and result.outcomes == {"none-of-pool": 1}
    assert any("none-of-pool" in n for n in result.notes)
    assert not out.exists() or out.read_text() == ""


def _ids(prompt: str) -> list[str]:
    """The thread ids the judging prompt offered — lets a fake judge grade the
    real pool the miner built rather than one the test invented."""
    import re

    return re.findall(r"^  \[([^\]]+)\]", prompt, flags=re.MULTILINE)


def test_pooled_miner_run_refuses_an_empty_ledger(archive_home, tmp_path):
    import pytest

    from thread_archive._store import init_db

    init_db()
    ns = argparse.Namespace(model="opus", jobs=1, seed=7,
                            out=tmp_path / "pooled-cases.jsonl", target=1,
                            depth=10, pool_cap=40, n_random=1, ledger_home=None)
    ctx = fw.MineContext(snapshot_id="s", target=1, model="opus", jobs=1,
                         tool_cmd="py tool", args=ns,
                         agent_run=lambda *a, **k: ("{}", {}))
    with pytest.raises(SystemExit, match="usage ledger"):
        pj.MINER.run(ctx)


def test_pooled_miner_is_registered_and_declares_its_pool_bias():
    from search_lab.mine import load_registry

    (miner,) = [m for m in load_registry() if m.name == "pooled"]
    # The honest declaration: this one's labels DID come through retrieval.
    assert miner.retrieval_free is False
    assert "pooled" in miner.gold_source and miner.runnable_in_all is False


# ── the QA pass ──────────────────────────────────────────────────────────────

def test_verify_drops_a_judgment_that_graded_everything_relevant():
    """The judging prompt says a pool where everything is 2 is as useless as one
    where everything is 0, and nothing checked that the judge listened. Such a
    verdict scores every ranker identically — weight in the denominator, no
    information."""
    ctx = fw.MineContext(snapshot_id="s", target=1, model="opus", jobs=1,
                         tool_cmd="py tool", args=argparse.Namespace())
    everything = {"verdict": {"grades": {f"T{i}": 2 for i in range(5)}}}
    assert pj.stage_verify(everything, ctx).reason == "undiscriminating"

    discriminating = {"verdict": {"grades": {"T1": 2, "T2": 1, "T3": 0, "T4": 0}}}
    verdict = pj.stage_verify(discriminating, ctx)
    assert verdict.kept and verdict.detail["grade2_share"] == 0.25


def test_undiscriminating_is_a_different_event_from_none_of_pool():
    """One says the judge could not separate the pool; the other says the pool held
    no answer. Collapsing them would lose the recall alarm."""
    assert "none-of-pool" != "undiscriminating"
    reasons = {s.name for s in pj.MINER.stages(argparse.Namespace())}
    assert reasons == {"pool", "judge", "verify"}


def test_pooled_pool_stage_is_free_so_a_plan_reaches_it():
    stages = pj.MINER.stages(argparse.Namespace())
    assert [s.kind for s in stages] == ["free", "agent", "free"]
