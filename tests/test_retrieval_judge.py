"""Unit coverage for the LLM-judge harness's pure logic.

The judging call itself (a headless ``claude`` subprocess) is operator-run and
costs tokens; what needs test coverage is everything around it — evidence
assembly and grade parsing in ``judge_query`` (with the call stubbed) and the
``summarize`` aggregation, especially the click-calibration and beyond-click
accounting that make this harness's numbers mean what they claim.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_judge",
    Path(__file__).resolve().parent.parent / "scripts" / "retrieval_judge.py",
)
retrieval_judge = importlib.util.module_from_spec(_SPEC)
sys.modules["retrieval_judge"] = retrieval_judge
_SPEC.loader.exec_module(retrieval_judge)


def _hits(*tids):
    return [{"thread_id": t, "thread_title": f"title {t}", "snippet": f"snip {t}"}
            for t in tids]


# ── judge_query ──────────────────────────────────────────────────────────────

def test_judge_query_dedups_threads_and_maps_grades():
    prompts = []

    def fake_call(prompt, model):
        prompts.append(prompt)
        return [{"id": 0, "grade": 2}, {"id": 1, "grade": 0}]

    graded = retrieval_judge.judge_query(
        "q", _hits("A", "A", "B"), "opus", call=fake_call)  # A twice: one candidate
    assert graded == {"A": 2, "B": 0}
    assert prompts[0].count("title A") == 1


def test_judge_query_drops_malformed_grade_rows():
    fake = lambda p, m: [{"id": 0, "grade": 1}, {"id": 99, "grade": 2},  # noqa: E731
                         {"id": 1, "grade": 7}, {"grade": 2}, "junk"]
    graded = retrieval_judge.judge_query("q", _hits("A", "B"), "opus", call=fake)
    assert graded == {"A": 1}


def test_judge_query_propagates_call_failure():
    assert retrieval_judge.judge_query(
        "q", _hits("A"), "opus", call=lambda p, m: None) is None


def test_judge_query_empty_hits_is_empty_not_failure():
    assert retrieval_judge.judge_query("q", [], "opus") == {}


# ── summarize ────────────────────────────────────────────────────────────────

def test_summarize_click_calibration_and_beyond_click():
    rows = [
        # click ranked and judged answering; one extra answer beyond the click
        {"rel_at5": 1.0, "ans_at5": 0.4, "n_answers": 2, "click_grade": 2,
         "beyond_click": 1},
        # click ranked but judged irrelevant — label noise made visible
        {"rel_at5": 0.2, "ans_at5": 0.0, "n_answers": 0, "click_grade": 0,
         "beyond_click": 0},
        # click never ranked: excluded from calibration, still scored
        {"rel_at5": 0.0, "ans_at5": 0.0, "n_answers": 1, "click_grade": None,
         "beyond_click": 1},
    ]
    report = retrieval_judge.summarize(rows)
    assert report["n"] == 3
    assert report["clicks_judged"] == 2
    assert report["click_judged_relevant"] == 0.5
    assert report["beyond_click_answers"] == 2
    assert report["any_answer_at5"] == 1 / 3
    assert report["any_relevant_at5"] == 2 / 3


def test_summarize_empty():
    assert retrieval_judge.summarize([]) == {"n": 0}
