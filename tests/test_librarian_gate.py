"""The librarian gate hook — the enforcement that makes the one-thread-at-a-time loop
mechanically impossible to skip.

Drives the hook as a subprocess (the way Claude Code invokes it), feeding tool-call JSON
on stdin and asserting block/allow. State is isolated per test via
``$THREAD_LIBRARIAN_GATE_DIR``. These are the guarantees the backfill leans on: an
instance can't open a second thread before finishing the first, can't commit a summary
with no citations, and can't stop mid-thread.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "librarian-gate.py"

READ = "mcp__thread-archive-librarian__thread_user_messages"
CITE = "mcp__thread-archive-librarian__topic_cite"
SUMMARY = "mcp__thread-archive-librarian__thread_set_summary"
SID = "test-session"


def _run(mode: str, payload: dict, gate_dir: Path) -> tuple[str, bool]:
    payload = {"session_id": SID, **payload}
    proc = subprocess.run(
        [sys.executable, str(HOOK), mode],
        input=json.dumps(payload), capture_output=True, text=True,
        env={**os.environ, "THREAD_LIBRARIAN_GATE_DIR": str(gate_dir)},
    )
    out = proc.stdout.strip()
    blocked = False
    if out:
        try:
            blocked = json.loads(out).get("decision") == "block"
        except ValueError:
            blocked = '"decision": "block"' in out
    return out, blocked


def _arm(gate_dir: Path) -> None:
    out, _ = _run("reset", {"prompt": "/librarian 25"}, gate_dir)
    assert "ENFORCEMENT ACTIVE" in out


def _open(thread_id: int, gate_dir: Path) -> None:
    _run("post", {"tool_name": READ, "tool_input": {"thread_id": thread_id}}, gate_dir)


def test_inactive_session_is_untouched(tmp_path):
    # No /librarian → no state → nothing is ever blocked.
    _, blocked = _run("pre", {"tool_name": READ, "tool_input": {"thread_id": 9}}, tmp_path)
    assert blocked is False
    _, blocked = _run("stop", {}, tmp_path)
    assert blocked is False


def test_non_librarian_prompt_does_not_arm(tmp_path):
    out, _ = _run("reset", {"prompt": "do something else"}, tmp_path)
    assert out == ""  # not armed
    _, blocked = _run("pre", {"tool_name": READ, "tool_input": {"thread_id": 1}}, tmp_path)
    assert blocked is False


def test_blocks_switching_threads_before_commit(tmp_path):
    _arm(tmp_path)
    _open(1, tmp_path)
    # paging the SAME thread is fine
    _, blocked = _run("pre", {"tool_name": READ, "tool_input": {"thread_id": 1}}, tmp_path)
    assert blocked is False
    # opening a DIFFERENT thread before committing #1 is blocked
    _, blocked = _run("pre", {"tool_name": READ, "tool_input": {"thread_id": 2}}, tmp_path)
    assert blocked is True


def test_blocks_summary_without_citation(tmp_path):
    _arm(tmp_path)
    _open(1, tmp_path)
    _, blocked = _run("pre", {"tool_name": SUMMARY, "tool_input": {"thread_id": 1}}, tmp_path)
    assert blocked is True  # zero citations


def test_allows_summary_after_citation(tmp_path):
    _arm(tmp_path)
    _open(1, tmp_path)
    _run("post", {"tool_name": CITE, "tool_input": {"thread_id": 1, "topic_id": 5, "event_id": 9}}, tmp_path)
    _, blocked = _run("pre", {"tool_name": SUMMARY, "tool_input": {"thread_id": 1}}, tmp_path)
    assert blocked is False


def test_blocks_stop_while_thread_open(tmp_path):
    _arm(tmp_path)
    _open(1, tmp_path)
    _, blocked = _run("stop", {}, tmp_path)
    assert blocked is True


def test_full_cycle_then_next_thread_allowed(tmp_path):
    _arm(tmp_path)
    _open(1, tmp_path)
    _run("post", {"tool_name": CITE, "tool_input": {"thread_id": 1, "topic_id": 5, "event_id": 9}}, tmp_path)
    _run("post", {"tool_name": SUMMARY, "tool_input": {"thread_id": 1}}, tmp_path)
    # committed → stop is fine, and the next thread opens
    _, blocked = _run("stop", {}, tmp_path)
    assert blocked is False
    _, blocked = _run("pre", {"tool_name": READ, "tool_input": {"thread_id": 2}}, tmp_path)
    assert blocked is False


def test_stop_three_strike_release(tmp_path):
    _arm(tmp_path)
    _open(1, tmp_path)
    # blocked, blocked, blocked, then released (so a failing run can't loop forever)
    assert _run("stop", {}, tmp_path)[1] is True
    assert _run("stop", {}, tmp_path)[1] is True
    assert _run("stop", {}, tmp_path)[1] is True
    assert _run("stop", {}, tmp_path)[1] is False
