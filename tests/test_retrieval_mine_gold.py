"""Unit coverage for the query gold-miner's pure logic.

The mining agent itself (a headless multi-turn ``claude``) is operator-run and
costs real tokens; what needs coverage is everything that makes its output
trustworthy — verdict parsing, gold validation against the originating session,
the prompt's baked-in session skips, re-run dedupe — plus the eval-side contract:
a case carries the ``snapshot_id`` of the corpus it was mined against, and the
eval binds to it.

The miner lives in the package (``thread_archive._mine.query_mined``); the eval
hub it feeds still lives on the bench (``search_lab/retrieval_eval.py``), loaded by
path here for the snapshot-binding contract tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ``_mine`` is repo-only — the wheel excludes it (pyproject
# [tool.hatch.build.targets.wheel]), so an installed-package run has nothing to
# import here. Gate before the imports so that run skips the module instead of
# erroring at collection.
pytest.importorskip("thread_archive._mine", reason="_mine is repo-only (excluded from the wheel)")

from thread_archive._mine import query_mined as mine_gold  # noqa: E402

_LAB = Path(__file__).resolve().parent.parent / "search_lab"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _LAB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


retrieval_eval = _load("retrieval_eval")


# ── parse_verdict ────────────────────────────────────────────────────────────

def test_parse_verdict_tolerates_surrounding_prose():
    text = ('Here is my verdict:\n'
            '{"gold": ["A"], "grades": {"A": 2, "B": 1}, '
            '"confidence": "high", "rationale": "A answers it."}\nDone.')
    v = mine_gold.parse_verdict(text)
    assert v["gold"] == ["A"]
    assert v["grades"] == {"A": 2, "B": 1}
    assert v["confidence"] == "high"


def test_parse_verdict_rejects_missing_or_nonlist_gold():
    assert mine_gold.parse_verdict('{"grades": {"A": 2}}') is None
    assert mine_gold.parse_verdict('{"gold": "A"}') is None
    assert mine_gold.parse_verdict("no json here") is None
    assert mine_gold.parse_verdict("{broken") is None


def test_parse_verdict_drops_malformed_grades_keeps_gold():
    v = mine_gold.parse_verdict(
        '{"gold": [123], "grades": {"A": 7, "B": "x", "C": 0}}')
    assert v["gold"] == ["123"]  # ids normalized to strings
    assert v["grades"] == {"C": 0}


def test_parse_verdict_empty_gold_is_valid():
    assert mine_gold.parse_verdict('{"gold": []}')["gold"] == []


# ── validate_gold ────────────────────────────────────────────────────────────

def test_validate_gold_drops_unresolvable_session_and_dupes():
    known = {"a", "b", "sess"}
    resolve = lambda ref: ref if ref in known else None  # noqa: E731
    gold = mine_gold.validate_gold(
        ["a", "ghost", "sess", "b", "a"], sessions={"sess"}, resolve=resolve)
    assert gold == ["a", "b"]  # unresolvable, originating session, dupe all dropped


def test_validate_gold_resolves_refs_to_canonical_ids():
    gold = mine_gold.validate_gold(
        ["42"], sessions=set(),
        resolve=lambda ref: "T42" if ref == "42" else None)
    assert gold == ["T42"]


# ── build_prompt ─────────────────────────────────────────────────────────────

def _case():
    return {"query": "capture daemon restart loop", "at": "2026-07-01T12:00:00",
            "session": "S1", "event_id": 9, "sessions": ["S1"],
            "clicks": ["T7"]}


def test_build_prompt_bakes_in_session_skips_and_no_date_bound():
    p = mine_gold.build_prompt(_case(), "ctx here", "py mine.py tool")
    assert 'search --skip "S1"' in p
    assert "--until" not in p  # the corpus is the snapshot; no per-case date bound
    assert "capture daemon restart loop" in p
    assert "ctx here" in p
    assert "T7" in p


def test_build_prompt_handles_missing_context_and_clicks():
    case = {**_case(), "clicks": [], "sessions": []}
    p = mine_gold.build_prompt(case, "", "py mine.py tool")
    assert "(context unavailable)" in p
    assert "(none recorded)" in p
    assert '--skip "-"' in p


def test_build_prompt_offers_the_session_as_a_read_handle():
    # The originating session id is handed over as a plain read handle with the
    # lead-up framing, so the agent can pull more of the pre-search context itself
    # when the ±3-turn window is too thin.
    p = mine_gold.build_prompt(_case(), "ctx here", "py mine.py tool")
    assert "read S1 --mode chat" in p
    assert "lead-up" in p


# ── mined_queries (re-run dedupe) ────────────────────────────────────────────

def test_mined_queries_reads_existing_and_skips_junk(tmp_path):
    f = tmp_path / "cases.jsonl"
    f.write_text('{"query": "a", "gold": ["T"]}\n'
                 'not json\n'
                 '{"nogold": true}\n'
                 '{"query": "b", "gold": []}\n')
    assert mine_gold.mined_queries(f) == {"a", "b"}
    assert mine_gold.mined_queries(tmp_path / "absent.jsonl") == set()


# ── query_sites (conversation-only scope) ────────────────────────────────────

def test_query_sites_excludes_subagent_fleet_sessions(archive_home):
    """query_sites recovers each query's search site (session/event/date) for
    mining, and like mine_log_cases it only sees top-level conversation sessions.
    A subagent fleet/sweep session (archived as ``system``) is skipped, so
    sample_cases can never join a fleet query back into the sampled set."""
    from datetime import datetime, timezone

    from thread_archive._store import Event, Thread, get_session, init_db, use_session

    init_db()

    def seed(name: str, query: str, thread_type: str) -> None:
        with get_session() as s:
            t = Thread(name=f"sess:{name}", thread_type=thread_type,
                       source="claude-code", source_id=f"agent:{name}")
            s.add(t)
            s.flush()
            s.add(Event(thread_id=t.id, stream_id="trail",
                        event_type="tool_use_complete",
                        payload={"tool_name": "mcp__thread-archive__thread_search",
                                 "input": {"query": query}},
                        occurred_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)))
            s.commit()

    seed("working", "a specific lookup", "conversation")
    seed("fleet", "collect the whole vein", "system")

    with use_session() as s:
        sites = mine_gold.query_sites(s)

    assert set(sites) == {"a specific lookup"}


def test_cases_for_queries_preserves_order_and_drops_siteless(archive_home):
    """The vetted-seed path: cases_for_queries attaches each hand-picked query's
    search site (session/event/at) in the given order, carries its click gold,
    and silently drops a query that was never searched (no site to anchor on)."""
    from datetime import datetime, timezone

    from thread_archive._store import Event, Thread, get_session, init_db

    init_db()
    with get_session() as s:
        gold = Thread(name="conv:gold", title="the answer thread",
                      thread_type="conversation", source="cc", source_id="g")
        s.add(gold)
        s.flush()
        gold_id = gold.id
        sess = Thread(name="sess:work", thread_type="conversation",
                      source="cc", source_id="w")
        s.add(sess)
        s.flush()
        sess_id = sess.id
        clk = iter(range(10, 20))
        s.add(Event(thread_id=sess_id, stream_id="t",
                    event_type="tool_use_complete",
                    payload={"tool_name": "mcp__thread-archive__thread_search",
                             "input": {"query": "alpha"}},
                    occurred_at=datetime(2026, 1, 1, 12, next(clk),
                                         tzinfo=timezone.utc)))
        s.add(Event(thread_id=sess_id, stream_id="t",
                    event_type="tool_use_complete",
                    payload={"tool_name": "mcp__thread-archive__thread_read",
                             "input": {"thread_id": gold_id}},
                    occurred_at=datetime(2026, 1, 1, 12, next(clk),
                                         tzinfo=timezone.utc)))
        s.commit()

    cases = mine_gold.cases_for_queries(["alpha", "never-searched"])

    assert [c["query"] for c in cases] == ["alpha"]  # order kept, siteless dropped
    (c,) = cases
    assert c["clicks"] == [gold_id]
    assert sess_id in c["sessions"]  # originating session is skipped when scoring
    assert c["session"] == sess_id and c["event_id"] and c["at"]


# ── eval-side contract: snapshot_id binds a case file to its corpus ──────────

def test_load_case_file_carries_snapshot_id(tmp_path):
    f = tmp_path / "cases.jsonl"
    f.write_text(
        '{"query": "q1", "gold": ["A"], "sessions": ["S"], '
        '"snapshot_id": "abc123", "protocol": "agent-mined"}\n'
        '{"query": "q2", "gold": ["B"]}\n')
    cases = retrieval_eval.load_case_file(f)
    assert cases[0]["snapshot_id"] == "abc123"
    assert "snapshot_id" not in cases[1]


def test_load_case_file_carries_grades_pool(tmp_path):
    # The graded candidate pool survives the round-trip: keys stringified, values
    # coerced to int, and a case without a pool simply omits it.
    f = tmp_path / "cases.jsonl"
    f.write_text(
        '{"query": "q1", "gold": ["A"], "grades": {"A": 2, "B": 1, "C": 0}}\n'
        '{"query": "q2", "gold": ["B"]}\n')
    cases = retrieval_eval.load_case_file(f)
    assert cases[0]["grades"] == {"A": 2, "B": 1, "C": 0}
    assert "grades" not in cases[1]


def test_evaluate_never_passes_a_date_bound_to_search():
    """The snapshot binding replaced the per-case date bound: evaluate scores over
    the frozen corpus as-is and passes the search no ``until``/``snapshot_id``."""
    seen = []

    def fake_search(query, **kw):
        seen.append(kw)
        return [{"thread_id": "A"}]

    cases = [{"query": "q1", "gold": ["A"], "snapshot_id": "abc123"},
             {"query": "q2", "gold": ["A"]}]
    report = retrieval_eval.evaluate(
        cases, limit=10, rerank=None, content_type=None,
        exclude_content_types=None, search=fake_search)
    assert report["mrr"] == 1.0
    for kw in seen:
        assert "until" not in kw and "snapshot_id" not in kw


def test_require_matching_snapshot_binds_cases_to_the_home(archive_home):
    """--cases refuses unless the home is a snapshot whose id matches every case."""
    import json

    cases = [{"query": "q", "gold": ["A"], "snapshot_id": "snap-aaa"}]

    # Not a snapshot home at all → refuse.
    with pytest.raises(SystemExit, match="not a snapshot"):
        retrieval_eval._require_matching_snapshot(cases, "cases.jsonl")

    # A snapshot home whose id does not match the cases → refuse as stale.
    (archive_home / "snapshot.json").write_text(json.dumps({"snapshot_id": "snap-bbb"}))
    with pytest.raises(SystemExit, match="stale"):
        retrieval_eval._require_matching_snapshot(cases, "cases.jsonl")

    # Matching id → accepted (no raise).
    (archive_home / "snapshot.json").write_text(json.dumps({"snapshot_id": "snap-aaa"}))
    retrieval_eval._require_matching_snapshot(cases, "cases.jsonl")
