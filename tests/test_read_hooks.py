"""Hooks that fired are visible, with what they injected — never a bare placeholder.

  * a ``hook_additional_context`` attachment renders as a first-class hook block
    (hook name + the injected content), on both the structured and string paths.
  * other preserved attachments carry their real content in the structured view
    (machinery: behind the tools gate) and a label on the string path.
  * ``hook_context`` sidecar events and ``hook_progress`` firings surface in the
    structured (web) view; the token-budgeted string path still skips them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive._retrieval.read import read_thread, read_thread_structured
from thread_archive._store import Event, Thread, init_db, use_session


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, 0, tzinfo=timezone.utc)


def _attachment_payload(att: dict) -> dict:
    """A context_summary payload the importer writes for a preserved attachment."""
    atype = att.get("type", "attachment")
    return {
        "content": f"[attachment: {atype}]",
        "system_type": "attachment",
        "provider_data": {"line": {"attachment": att, "type": "attachment"}},
    }


_HOOK_ATT = {
    "type": "hook_additional_context",
    "content": ["[system-map] `archive` (system)\n  path: archive"],
    "hookName": "UserPromptSubmit",
    "hookEvent": "UserPromptSubmit",
}

_CORPUS = [
    ("user_message_sent", {"content": "the question"}, 1),
    ("context_summary", _attachment_payload(_HOOK_ATT), 2),
    ("context_summary", _attachment_payload(
        {"type": "skill_listing", "content": "- backend-api: reach the backend"}), 3),
    ("context_summary", _attachment_payload(
        {"type": "deferred_tools_delta", "addedNames": ["CronCreate", "Monitor"]}), 4),
    ("hook_context", {"hook_name": "kg-context", "context": "<kg_context>topics</kg_context>"}, 5),
    ("progress", {"data": {"type": "hook_progress", "command": "callback",
                           "hookName": "PreToolUse:Read", "hookEvent": "PreToolUse"}}, 6),
    ("progress", {"data": {"type": "bash_progress", "output": "chugging"}}, 7),
    ("text_complete", {"text": "the answer"}, 8),
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


def _blocks(data: dict) -> list[dict]:
    return [b for m in data["messages"] for b in m["blocks"]]


def test_structured_hook_attachment_is_first_class() -> None:
    data = read_thread_structured(_seed())
    hooks = [b for b in _blocks(data) if b["type"] == "hook"]
    by_name = {h["hook_name"]: h for h in hooks}
    # The attachment-preserved hook: name + exactly what it injected.
    assert "[system-map] `archive` (system)" in by_name["UserPromptSubmit"]["text"]
    # The sidecar-preserved hook surfaces the same way.
    assert "<kg_context>" in by_name["kg-context"]["text"]


def test_structured_hooks_survive_tools_off() -> None:
    # Hook injections are conversation context, not machinery — visible even
    # with the tools toggle off (the point of surfacing them).
    data = read_thread_structured(_seed(), include_tools=False)
    types = {b["type"] for b in _blocks(data)}
    assert "hook" in types
    # Machinery stays gated: attachments and bare firings disappear.
    assert "attachment" not in types and "hook_fired" not in types


def test_structured_attachments_carry_real_content() -> None:
    data = read_thread_structured(_seed())
    atts = {b["attachment_type"]: b for b in _blocks(data) if b["type"] == "attachment"}
    # `content` attachments show the content itself…
    assert "backend-api" in atts["skill_listing"]["text"]
    # …and content-less ones fall back to their own fields, never a bare label.
    assert "CronCreate" in atts["deferred_tools_delta"]["text"]


def test_structured_hook_progress_marker_and_other_progress_skipped() -> None:
    blocks = _blocks(read_thread_structured(_seed()))
    fired = [b for b in blocks if b["type"] == "hook_fired"]
    assert [b["hook_name"] for b in fired] == ["PreToolUse:Read"]
    # Non-hook progress stays bookkeeping noise.
    assert not any("chugging" in str(b) for b in blocks)


def test_string_full_view_shows_hook_with_content() -> None:
    out = read_thread(_seed(), mode="full")
    assert "[hook: UserPromptSubmit]" in out
    assert "[system-map] `archive` (system)" in out
    # Other attachments stay a label on the token-budgeted path.
    assert "[attachment: skill_listing]" in out
    assert "backend-api" not in out
    # Sidecar/progress hook records stay off the string path entirely.
    assert "kg_context" not in out and "PreToolUse" not in out


def test_string_chat_view_hides_hook_machinery() -> None:
    out = read_thread(_seed(), mode="chat")
    assert "the answer" in out
    assert "hook" not in out.lower()
    assert "[attachment:" not in out
