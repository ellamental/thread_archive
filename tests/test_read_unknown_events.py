"""The reader must surface the content the builder now preserves — and never
silently swallow an event type it doesn't model.

  * ``message`` (a preserved non-standard-role turn) shows under its own role.
  * ``ide_context`` / ``content_block`` show as machinery (full view / include_tools).
  * a genuinely-unknown event type is rendered as a labeled block, not dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive._retrieval.read import read_thread, read_thread_structured
from thread_archive._store import Event, Thread, init_db, use_session


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, 0, tzinfo=timezone.utc)


# user → assistant text, then the three preserved/unknown kinds, then a tool-role turn.
_CORPUS = [
    ("user_message_sent", {"content": "the question"}, 1),
    ("text_complete", {"text": "the answer"}, 2),
    ("content_block", {"block_type": "server_tool_use",
                       "data": {"type": "server_tool_use", "name": "web_search",
                                "input": {"query": "eds pain"}}}, 3),
    ("ide_context", {"context_type": "selection", "file_path": "/proj/foo.py",
                     "content": "def foo(): pass"}, 4),
    ("mystery_event", {"content": "some future payload"}, 5),
    ("message", {"role": "tool", "content": "plugin output here"}, 6),
]


def _seed(events=_CORPUS, tid=1) -> int:
    init_db()
    with use_session() as s:
        s.add(Thread(id=tid, name=f"t{tid}", title="T", thread_type="conversation",
                     source="claude-code", inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
        for i, (et, payload, minute) in enumerate(events, start=1):
            s.add(Event(id=i, thread_id=tid, stream_id="s", event_type=et,
                        payload=payload, occurred_at=_dt(minute)))
        s.commit()
    return tid


def test_full_view_surfaces_preserved_and_unknown() -> None:
    out = read_thread(_seed(), mode="full")
    assert "plugin output here" in out and "[TOOL" in out       # non-standard role turn
    assert "block: server_tool_use" in out and "eds pain" in out  # unmodeled block
    assert "ide selection" in out and "/proj/foo.py" in out       # ide context
    assert "[mystery_event]" in out and "some future payload" in out  # unknown, not dropped


def test_chat_view_hides_machinery_but_keeps_role_turn() -> None:
    out = read_thread(_seed(), mode="chat")
    # Genuine content stays; machinery (tools/context/unknown) is stripped.
    assert "the answer" in out
    assert "plugin output here" in out
    assert "server_tool_use" not in out
    assert "ide selection" not in out
    assert "mystery_event" not in out


def test_structured_view_types_and_default_gate() -> None:
    tid = _seed()
    msgs = read_thread_structured(tid, include_tools=True)["messages"]
    types = {b["type"] for m in msgs for b in m["blocks"]}
    assert {"content_block", "ide_context", "unknown"} <= types
    assert any(m["role"] == "tool" for m in msgs)  # non-standard role preserved

    # Without machinery, the unmodeled/unknown blocks drop out but the role turn stays.
    lean = read_thread_structured(tid, include_tools=False)["messages"]
    lean_types = {b["type"] for m in lean for b in m["blocks"]}
    assert not ({"content_block", "ide_context", "unknown"} & lean_types)
    assert any(m["role"] == "tool" for m in lean)
