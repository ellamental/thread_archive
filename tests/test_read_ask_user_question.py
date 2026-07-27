"""An AskUserQuestion result carries the user's decision, not just the prose recap.

The harness records the answer twice: as English in ``output`` and machine-readable
under ``annotations.structured_result``. The structured view passes the second copy
through — ``answers`` (question text → the option label taken, or the reader's own
words) plus the questions as recorded — so the viewer can render the question with
its outcome marked instead of a JSON blob. A rejected call carries its denial kind
for the same reason: asked and deliberately not answered is its own outcome.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

from thread_archive._retrieval.read import read_thread_structured
from thread_archive._store import Event, Thread, init_db, mint_ulid, use_session

_QUESTIONS = [
    {
        "question": "Which way should folding default?",
        "header": "Fold",
        "multiSelect": False,
        "options": [
            {"label": "fold=True default", "description": "Nothing changes for today's callers."},
            {"label": "Stop folding", "description": "One less concept."},
        ],
    }
]


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, 0, tzinfo=timezone.utc)


_next_event_id = itertools.count(1)


def _seed(events) -> str:
    init_db()
    tid = mint_ulid()
    with use_session() as s:
        s.add(Thread(id=tid, name=f"t{tid}", title="T", thread_type="conversation",
                     source="claude-code", inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
        for et, payload, minute in events:
            s.add(Event(id=next(_next_event_id), thread_id=tid, stream_id="s", event_type=et,
                        payload=payload, occurred_at=_dt(minute)))
        s.commit()
    return tid


def _blocks(tid: str) -> list[dict]:
    return [b for m in read_thread_structured(tid)["messages"] for b in m["blocks"]]


def _call_event(minute: int = 2) -> tuple:
    return ("tool_use_complete",
            {"tool_name": "AskUserQuestion", "input": {"questions": _QUESTIONS}}, minute)


def _answer_event(answer: str, minute: int = 3) -> tuple:
    return ("tool_execution_completed", {
        "tool_name": "unknown",
        "output": f'The user answered: "Which way should folding default?"="{answer}".',
        "annotations": {"structured_result": {
            "questions": _QUESTIONS,
            "answers": {"Which way should folding default?": answer},
        }},
    }, minute)


def test_answer_rides_the_result_block():
    tid = _seed([("user_message_sent", {"content": "decide"}, 1),
                 _call_event(), _answer_event("Stop folding")])
    call, result = [b for b in _blocks(tid) if b["type"] in ("tool_use", "tool_result")]
    assert call["input"]["questions"][0]["header"] == "Fold"
    assert result["answers"] == {"Which way should folding default?": "Stop folding"}
    assert result["questions"][0]["options"][1]["label"] == "Stop folding"
    # the prose recap stays too — the block is still a tool result
    assert "The user answered" in result["output"]


def test_free_text_answer_passes_through_verbatim():
    """A reader who writes their own answer instead of taking an option: the text is
    the answer, and nothing tries to map it back onto a label."""
    tid = _seed([_call_event(), _answer_event("neither — leave browse alone")])
    result = [b for b in _blocks(tid) if b["type"] == "tool_result"][0]
    assert result["answers"]["Which way should folding default?"] == "neither — leave browse alone"


def test_ordinary_tool_result_carries_no_answers():
    tid = _seed([("tool_use_complete", {"tool_name": "Bash", "input": {"command": "ls"}}, 2),
                 ("tool_execution_completed", {"output": "a.py"}, 3)])
    result = [b for b in _blocks(tid) if b["type"] == "tool_result"][0]
    assert "answers" not in result and "questions" not in result


def test_malformed_structured_result_is_ignored():
    """A structured_result that isn't the {questions, answers} shape (a plain string,
    an empty map) leaves the block as an ordinary tool result rather than half-built."""
    for sr in ("User rejected tool use", {"answers": {}}, {"answers": "picked"}, None):
        tid = _seed([_call_event(),
                     ("tool_execution_completed",
                      {"output": "out", "annotations": {"structured_result": sr}}, 3)])
        result = [b for b in _blocks(tid) if b["type"] == "tool_result"][0]
        assert "answers" not in result, sr


def test_denied_call_records_why_it_never_ran():
    tid = _seed([_call_event(), ("tool_execution_error", {
        "error": "The user doesn't want to proceed with this tool use.",
        "annotations": {"structured_result": "User rejected tool use",
                        "tool_denial_kind": "user-rejected"},
    }, 3)])
    error = [b for b in _blocks(tid) if b["type"] == "tool_error"][0]
    assert error["denial_kind"] == "user-rejected"


def test_ordinary_tool_error_carries_no_denial_kind():
    tid = _seed([("tool_execution_error", {"error": "command not found"}, 2)])
    error = [b for b in _blocks(tid) if b["type"] == "tool_error"][0]
    assert "denial_kind" not in error
