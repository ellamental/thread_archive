"""Unit coverage for the gold-mining harness's pure logic.

The mining agent itself (a headless multi-turn ``claude``) is operator-run
and costs real tokens; what needs coverage is everything that makes its
output trustworthy — verdict parsing, gold validation against the date bound
and the originating session, the prompt's baked-in corpus bound, re-run
dedupe — plus the eval-side contract: a case's ``until`` must reach the
search under evaluation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_EVALS = Path(__file__).resolve().parent.parent / "evals"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _EVALS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mine_gold = _load("retrieval_mine_gold")
retrieval_eval = sys.modules["retrieval_eval"]  # loaded by the script itself


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

def _validators(existing: dict[str, str]):
    """resolve/first_event_at over a {tid: first_event_iso} fixture."""
    return (lambda ref: ref if ref in existing else None,
            lambda tid: existing.get(tid))


def test_validate_gold_enforces_date_bound_and_sessions():
    resolve, first_at = _validators({
        "old": "2026-06-01T00:00:00", "new": "2026-07-15T00:00:00",
        "sess": "2026-05-01T00:00:00"})
    gold = mine_gold.validate_gold(
        ["old", "new", "ghost", "sess", "old"],
        until="2026-07-01T00:00:00", sessions={"sess"},
        resolve=resolve, first_event_at=first_at)
    assert gold == ["old"]  # post-date, unresolvable, session, dupe all dropped


def test_validate_gold_resolves_refs_to_canonical_ids():
    gold = mine_gold.validate_gold(
        ["42"], until="2026-07-01", sessions=set(),
        resolve=lambda ref: "T42" if ref == "42" else None,
        first_event_at=lambda tid: "2026-01-01")
    assert gold == ["T42"]


# ── build_prompt ─────────────────────────────────────────────────────────────

def _case():
    return {"query": "capture daemon restart loop", "at": "2026-07-01T12:00:00",
            "session": "S1", "event_id": 9, "sessions": ["S1"],
            "clicks": ["T7"]}


def test_build_prompt_bakes_in_bound_and_skips():
    p = mine_gold.build_prompt(_case(), "ctx here", "py mine.py tool")
    assert 'search --until "2026-07-01T12:00:00" --skip "S1"' in p
    assert 'read --until "2026-07-01T12:00:00"' in p
    assert "capture daemon restart loop" in p
    assert "ctx here" in p
    assert "T7" in p


def test_build_prompt_handles_missing_context_and_clicks():
    case = {**_case(), "clicks": [], "sessions": []}
    p = mine_gold.build_prompt(case, "", "py mine.py tool")
    assert "(context unavailable)" in p
    assert "(none recorded)" in p
    assert '--skip "-"' in p


# ── mined_queries (re-run dedupe) ────────────────────────────────────────────

def test_mined_queries_reads_existing_and_skips_junk(tmp_path):
    f = tmp_path / "cases.jsonl"
    f.write_text('{"query": "a", "gold": ["T"]}\n'
                 'not json\n'
                 '{"nogold": true}\n'
                 '{"query": "b", "gold": []}\n')
    assert mine_gold.mined_queries(f) == {"a", "b"}
    assert mine_gold.mined_queries(tmp_path / "absent.jsonl") == set()


# ── eval-side contract: until flows from case file to the search call ────────

def test_load_case_file_carries_until(tmp_path):
    f = tmp_path / "cases.jsonl"
    f.write_text(
        '{"query": "q1", "gold": ["A"], "sessions": ["S"], '
        '"until": "2026-07-01", "protocol": "agent-mined"}\n'
        '{"query": "q2", "gold": ["B"]}\n')
    cases = retrieval_eval.load_case_file(f)
    assert cases[0]["until"] == "2026-07-01"
    assert "until" not in cases[1]


def test_evaluate_passes_until_only_when_present():
    seen = []

    def fake_search(query, **kw):
        seen.append(kw.get("until"))
        return [{"thread_id": "A"}]

    cases = [{"query": "q1", "gold": ["A"], "until": "2026-07-01"},
             {"query": "q2", "gold": ["A"]}]
    report = retrieval_eval.evaluate(
        cases, limit=10, rerank=None, content_type=None,
        exclude_content_types=None, search=fake_search)
    assert seen == ["2026-07-01", None]
    assert report["mrr"] == 1.0
