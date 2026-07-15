"""The golden-set miner — search→read behaviour into relevance judgments.

The load-bearing property: a search's result row is paired back to its call by
``tool_call_id``, not by re-matching the tool name on the result row. The dominant
archive format (Claude Code) labels tool_result rows ``tool_name='unknown'``, so a
name-based pairing would drop ~all real searches and starve the golden set. These
tests pin the cid pairing (recovers the ``unknown``-labeled result), the click
gate (a read only counts when its thread id is in *that* search's true result, so
positional misattribution can't manufacture a false case), and ``--min-terms``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from thread_archive._importers.claude_code import import_session_incremental
from thread_archive._store import init_db

_SPEC = importlib.util.spec_from_file_location(
    "golden_from_usage", Path(__file__).resolve().parent.parent / "scripts" / "golden_from_usage.py"
)
miner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(miner)


def _import(home: Path, name: str, lines: list[dict]) -> int:
    f = home / f"{name}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return import_session_incremental(f, f"proj:{name}").thread_id


def _conversation(day: int, user_text: str, asst_text: str) -> list[dict]:
    return [
        {"type": "user", "uuid": f"u{day}", "timestamp": f"2026-01-{day:02d}T10:00:00Z",
         "sessionId": f"c{day}", "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": f"a{day}", "parentUuid": f"u{day}",
         "timestamp": f"2026-01-{day:02d}T10:00:05Z",
         "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": asst_text}]}},
    ]


def _search_read_session(query: str, result_text: str, read_tid: int) -> list[dict]:
    """A session that searches, receives an ``unknown``-labeled result, then reads a
    thread. The result row is linked to the call only by ``tool_use_id`` — the case
    the miner must recover."""
    return [
        {"type": "user", "uuid": "s1", "timestamp": "2026-02-01T10:00:00Z", "sessionId": "sess",
         "message": {"role": "user", "content": "go find it"}},
        {"type": "assistant", "uuid": "s2", "parentUuid": "s1", "timestamp": "2026-02-01T10:00:05Z",
         "message": {"role": "assistant", "model": "m", "content": [
             {"type": "tool_use", "id": "toolu_S", "name": "mcp__thread-archive__thread_search",
              "input": {"query": query}}]}},
        {"type": "user", "uuid": "s3", "parentUuid": "s2", "timestamp": "2026-02-01T10:00:06Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_S", "content": result_text}]}},
        {"type": "assistant", "uuid": "s4", "parentUuid": "s3", "timestamp": "2026-02-01T10:00:08Z",
         "message": {"role": "assistant", "model": "m", "content": [
             {"type": "tool_use", "id": "toolu_R", "name": "mcp__thread-archive__thread_read",
              "input": {"thread_id": read_tid}}]}},
    ]


def test_pairs_click_through_unknown_labeled_result(archive_home) -> None:
    init_db()
    target = _import(archive_home, "tgt", _conversation(1, "a chat about zebras", "zebras are striped"))
    result = f'<search count="1"><thread id="{target}" title="zebras"><hit>zebras</hit></thread></search>'
    _import(archive_home, "sess", _search_read_session("striped equine animals plains", result, target))

    cases = miner.mine(min_query_terms=2)
    assert {"query": "striped equine animals plains", "thread_ids": [target]} in cases


def test_read_not_in_search_result_is_not_a_case(archive_home) -> None:
    """The click gate: a read whose thread id is absent from the search's true result
    is not a click on it — no case. This is exactly the positional false-positive the
    cid pairing eliminates (the old name+position attach could credit the wrong search)."""
    init_db()
    target = _import(archive_home, "tgt", _conversation(1, "a chat about zebras", "zebras are striped"))
    other = _import(archive_home, "other", _conversation(2, "a chat about comets", "comets have tails"))
    # The search's result lists `target`, but the session reads `other`.
    result = f'<search count="1"><thread id="{target}" title="zebras"><hit>zebras</hit></thread></search>'
    _import(archive_home, "sess", _search_read_session("striped equine animals plains", result, other))

    cases = miner.mine(min_query_terms=2)
    assert all(c["query"] != "striped equine animals plains" for c in cases)


def test_respects_min_terms(archive_home) -> None:
    init_db()
    target = _import(archive_home, "tgt", _conversation(1, "a chat about zebras", "zebras are striped"))
    result = f'<search count="1"><thread id="{target}" title="zebras"><hit>zebras</hit></thread></search>'
    _import(archive_home, "sess", _search_read_session("zebra", result, target))

    assert miner.mine(min_query_terms=2) == []
    assert any(c["query"] == "zebra" for c in miner.mine(min_query_terms=1))
