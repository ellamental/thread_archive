"""thread_read transcript contract — mode views, turn pagination, budget, summary.

Pins the ``read_thread`` transcript contract:
default user-only, ``mode`` = user/chat/full, turn pagination (limit/offset/
after_event), search-result focus (around_event/context_turns), a ~48k char budget
with a CHUNKED footer, and the ``summary`` views
(TOC, plus the stored short/indexed thread summaries behind a feature flag).
Tool *results* are never rendered (calls only).

Events are seeded directly so the rendering contract is tested independently of the
importers: one thread with thinking + tool-call + tool-result + text blocks across
three turns, the last a context-compaction boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive._retrieval import read_thread, resolve_thread_ref
from thread_archive._store import (
    Event,
    Thread,
    TopicMessage,
    init_db,
    mint_ulid,
    use_session,
)

_COMPACTION = "This session is being continued from a previous conversation. Summary: blah."


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, 0, tzinfo=timezone.utc)


# (event_type, payload, minute) in id order. Three turns:
#   turn 1: user → [thinking, Bash call, (result, dropped), text]
#   turn 2: user → [text] then [Read call, text]   (two assistant steps)
#   turn 3: user is a compaction boundary → [text]
_CORPUS = [
    ("user_message_sent", {"content": "first question about authentication"}, 1),
    ("thinking_complete", {"text": "let me think about auth"}, 2),
    ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "ls -la"}}, 3),
    ("tool_execution_completed", {"output": "TOOLOUTPUT the ls listing"}, 3),
    ("text_complete", {"text": "here is the first answer"}, 4),
    ("user_message_sent", {"content": "second question about database"}, 5),
    ("text_complete", {"text": "answer two preamble"}, 6),
    ("tool_use_complete", {"tool_name": "Read", "input": {"file_path": "/x/y"}}, 7),
    ("text_complete", {"text": "answer two conclusion"}, 8),
    ("user_message_sent", {"content": _COMPACTION}, 9),
    ("text_complete", {"text": "post compaction reply"}, 10),
]


def _seed(events=_CORPUS, *, thread_type="conversation", title="Test Thread", tid=None,
          source="claude-code", source_id=None, updated_minute=0, legacy_id=None):
    init_db()
    tid = tid or mint_ulid()
    with use_session() as s:
        s.add(Thread(id=tid, legacy_id=legacy_id, name=f"t{tid}", title=title,
                     thread_type=thread_type, source=source, source_id=source_id,
                     inserted_at=_dt(0), updated_at=_dt(updated_minute)))
        s.commit()  # parent before children (FK)
        for i, (et, payload, minute) in enumerate(events, start=1):
            s.add(Event(id=i, thread_id=tid, stream_id="s", event_type=et,
                        payload=payload, occurred_at=_dt(minute)))
        s.commit()
    return tid


def _header(out: str) -> str:
    return out.splitlines()[2]


# ── default + the three views ────────────────────────────────────────────────

def test_default_is_user_only(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid)
    assert "user turns" in _header(out)              # "3 user turns"
    assert "[USER" in out
    assert "first question about authentication" in out
    assert "[ASSISTANT" not in out                   # assistant suppressed
    assert "here is the first answer" not in out
    # the compaction turn collapses to a placeholder, never its summary text
    assert "[COMPACTION event:10]" in out
    assert _COMPACTION not in out


def test_mode_chat_user_plus_assistant_text(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, mode="chat")
    assert _header(out).startswith("Messages: 3 turns")
    assert "[ASSISTANT" in out
    assert "here is the first answer" in out
    assert "answer two preamble" in out and "answer two conclusion" in out
    # chat strips thinking AND tool calls (and so never shows results)
    assert "let me think about auth" not in out
    assert "[tool: Bash" not in out
    assert "[thinking]" not in out
    assert "TOOLOUTPUT" not in out


def test_mode_full_shows_calls_and_thinking_but_not_results_by_default(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, mode="full")
    assert "[tool: Bash command=ls -la]" in out
    assert "[tool: Read file_path=/x/y]" in out
    assert "[thinking]" in out and "let me think about auth" in out
    assert "here is the first answer" in out
    # results are off by default (must opt in with tool_results=True)
    assert "TOOLOUTPUT" not in out and "[result]" not in out


def test_tool_results_opt_in(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, mode="full", tool_results=True)
    assert "[result] TOOLOUTPUT the ls listing" in out
    assert "[tool: Bash command=ls -la]" in out  # still shows the call above it


def test_tool_results_need_tools_shown(archive_home) -> None:
    """tool_results is a no-op when calls are stripped (chat) or absent (user)."""
    tid = _seed()
    assert "TOOLOUTPUT" not in read_thread(tid, mode="chat", tool_results=True)
    assert "TOOLOUTPUT" not in read_thread(tid, mode="user", tool_results=True)


def test_tool_error_rendered_with_results(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "run a thing"}, 1),
        ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "boom"}}, 2),
        ("tool_execution_error", {"error": "ERRTEXT command failed"}, 2),
        ("text_complete", {"text": "that failed"}, 3),
    ])
    assert "ERRTEXT" not in read_thread(tid, mode="full")  # off by default
    out = read_thread(tid, mode="full", tool_results=True)
    assert "[tool error] ERRTEXT command failed" in out


def test_tool_result_truncated(archive_home) -> None:
    big = "X" * 5000
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "c"}}, 2),
        ("tool_execution_completed", {"output": big}, 2),
        ("text_complete", {"text": "done"}, 3),
    ])
    out = read_thread(tid, mode="full", tool_results=True)
    assert "… (truncated)" in out
    assert out.count("X") <= 2100  # capped near _TOOL_RESULT_CAP, not the full 5000


def test_user_only_alias(archive_home) -> None:
    tid = _seed()
    assert read_thread(tid, user_only=True) == read_thread(tid, mode="user")
    assert read_thread(tid, user_only=False) == read_thread(tid, mode="full")
    # mode wins when both are set
    assert read_thread(tid, mode="user", user_only=False) == read_thread(tid, mode="user")


def test_unrecognised_mode_falls_back_to_user(archive_home) -> None:
    tid = _seed()
    assert read_thread(tid, mode="bogus") == read_thread(tid, mode="user")


def test_mode_last_returns_only_final_assistant_text(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, mode="last")
    assert "(last assistant message)" in out
    # the closing reply — even though its turn is a compaction boundary the
    # transcript views collapse; the summary text itself must not leak
    assert "post compaction reply" in out
    assert _COMPACTION not in out
    # nothing else from the thread: no earlier answers, no user turns, no machinery
    assert "here is the first answer" not in out
    assert "second question" not in out
    assert "let me think" not in out
    assert "[tool:" not in out
    # anchored and locatable: event id, turn position, and the expand hint
    assert "event:11" in out
    assert "turn 3 of 3" in out
    assert "mode='chat', offset=2" in out


def test_mode_last_skips_trailing_unanswered_user(archive_home) -> None:
    events = _CORPUS + [("user_message_sent", {"content": "unanswered follow-up"}, 11)]
    tid = _seed(events)
    out = read_thread(tid, mode="last")
    assert "post compaction reply" in out
    assert "unanswered follow-up" not in out


def test_mode_last_truncates_at_budget(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, mode="last", max_chars=10)
    assert "post compa" in out and "post compaction reply" not in out
    assert "truncated" in out


def test_mode_last_without_assistant_text(archive_home) -> None:
    tid = _seed([("user_message_sent", {"content": "hello?"}, 1)])
    out = read_thread(tid, mode="last")
    assert "no assistant text" in out


def test_mode_ends_default_first_and_last_turn(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, mode="ends")
    assert "(ends view: first 1 + last 1 of 3 turns)" in out
    # head: the opening exchange, chat-style (thinking + tools stripped)
    assert "first question about authentication" in out
    assert "here is the first answer" in out
    assert "let me think" not in out and "[tool:" not in out
    # tail: the closing turn — a compaction boundary collapses to its placeholder,
    # exactly as it does in every transcript view
    assert "[COMPACTION event:10]" in out
    assert _COMPACTION not in out
    # middle omitted, with a resume hint pointing past the head
    assert "second question" not in out
    assert "1 intervening turns omitted" in out
    assert "mode='chat', offset=1" in out


def test_mode_ends_context_turns_widens_to_whole_thread(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, mode="ends", context_turns=2)
    # 2 per end covers all 3 turns — rendered whole, no gap marker
    assert "(ends view: all 3 of 3 turns)" in out
    assert "second question about database" in out
    assert "omitted" not in out


def test_mode_ends_budget_keeps_outermost_turns(archive_home) -> None:
    events = []
    for i, word in enumerate(("alpha", "beta", "gamma", "delta")):
        events.append(("user_message_sent", {"content": f"q{i}"}, i * 2 + 1))
        events.append(("text_complete", {"text": word + " " + "A" * 200}, i * 2 + 2))
    tid = _seed(events)
    out = read_thread(tid, mode="ends", context_turns=2, max_chars=600)
    # each end trims inward: the opening and closing turns survive, the inner
    # requested turns drop with a note
    assert "alpha" in out and "delta" in out
    assert "beta" not in out and "gamma" not in out
    assert "first 1 + last 1 of 4" in out
    assert "dropped at the" in out


# ── step grouping ────────────────────────────────────────────────────────────

def test_steps_close_at_text(archive_home) -> None:
    """turn 2 yields two assistant steps (preamble; then Read+conclusion). The
    compaction turn collapses to a placeholder, so its reply isn't a separate step
    (a compaction turn is a boundary marker, not content)."""
    tid = _seed()
    out = read_thread(tid, mode="full")
    assert out.count("[ASSISTANT") == 3  # t1:1, t2:2  (t3 collapses to a placeholder)
    assert "post compaction reply" not in out


# ── summary TOC ──────────────────────────────────────────────────────────────

def test_summary_toc(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, summary=True)
    assert "(7 messages)" in out  # 3 user + 4 assistant steps
    assert "| # | Role | Event ID | Time | Preview |" in out
    assert "| 1 | user |" in out
    # a tool-only step would preview as "[tool use]"; ours all end in text
    assert "here is the first answer" in out


def test_summary_pagination(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, summary=True, limit=2)
    assert "Showing 1-2 of 7" in out
    out2 = read_thread(tid, summary=True, limit=2, offset=2)
    assert "Showing 3-4 of 7" in out2


# ── stored summaries (summary='short' / 'indexed') ──────────────────────────

def _set_summaries(tid: str, *, short=None, indexed=None) -> None:
    with use_session() as s:
        t = s.get(Thread, tid)
        t.summary = short
        t.indexed_summary = indexed
        s.commit()


def test_summary_short_returns_stored(archive_home) -> None:
    tid = _seed()
    _set_summaries(tid, short="A short prose summary.", indexed="## Indexed\nthe long form")
    out = read_thread(tid, summary="short")
    assert "(short summary)" in out
    assert "A short prose summary." in out
    assert "the long form" not in out


def test_summary_indexed_returns_stored(archive_home) -> None:
    tid = _seed()
    _set_summaries(tid, short="A short prose summary.", indexed="## Indexed\nthe long form")
    out = read_thread(tid, summary="indexed")
    assert "(indexed summary)" in out
    assert "the long form" in out
    assert "A short prose summary." not in out


def test_summary_missing_kind_names_the_other(archive_home) -> None:
    tid = _seed()
    _set_summaries(tid, indexed="## Indexed only")
    out = read_thread(tid, summary="short")
    assert "no short summary" in out
    assert "summary='indexed'" in out


def test_summary_missing_both_points_at_toc(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, summary="indexed")
    assert "no indexed summary" in out
    assert "summary=true" in out


def test_summary_unknown_kind_is_reported(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, summary="shrot")
    assert "Unknown summary kind" in out
    assert "'short'" in out and "'indexed'" in out


def test_summary_string_bool_aliases(archive_home) -> None:
    tid = _seed()
    # MCP clients sometimes stringify booleans; 'toc' is the explicit TOC name.
    assert read_thread(tid, summary="toc") == read_thread(tid, summary=True)
    assert read_thread(tid, summary="true") == read_thread(tid, summary=True)
    assert "[USER" in read_thread(tid, summary="false")


def test_stored_summaries_feature_flag_disables(archive_home, monkeypatch) -> None:
    tid = _seed()
    _set_summaries(tid, short="A short prose summary.")
    monkeypatch.setenv("THREAD_ARCHIVE_STORED_SUMMARIES", "0")
    out = read_thread(tid, summary="short")
    assert "disabled" in out
    assert "A short prose summary." not in out
    # the TOC view is not behind the flag
    assert "| # | Role | Event ID | Time | Preview |" in read_thread(tid, summary=True)


# ── turn pagination ──────────────────────────────────────────────────────────

def test_limit_and_footer(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, limit=1)
    assert "showing 1-1" in _header(out)
    assert "⚠ CHUNKED" in out and "2 of 3 user turns not shown" in out
    assert "offset=1" in out
    # user mode → no user_only=false hint in the continuation
    assert "user_only=false" not in out


def test_offset(archive_home) -> None:
    tid = _seed()
    assert "showing 2-3" in _header(read_thread(tid, offset=1))


def test_negative_offset(archive_home) -> None:
    tid = _seed()
    assert "showing 3-3" in _header(read_thread(tid, offset=-1))


def test_offset_past_end(archive_home) -> None:
    tid = _seed()
    assert "past the end" in read_thread(tid, offset=99)


def test_after_event_resumes_next_turn(archive_home) -> None:
    tid = _seed()
    # event 4 is the last event of turn 1 → resume at turn 2
    out = read_thread(tid, after_event=4)
    assert "showing 2-3" in _header(out)
    assert "first question" not in out
    assert "second question about database" in out


def test_around_event_opens_containing_turn(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, around_event=8, context_turns=0)
    assert "focused on event 8 in turn 2, showing 2-2" in _header(out)
    assert "second question about database" in out
    assert "answer two preamble" in out and "answer two conclusion" in out
    assert "match:8" in out
    assert "first question about authentication" not in out
    assert "[COMPACTION" not in out


def test_around_event_defaults_to_one_surrounding_turn(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, around_event=6)
    assert "showing 1-3" in _header(out)
    assert "first question about authentication" in out
    assert "second question about database" in out
    assert "[COMPACTION" in out


def test_around_event_default_is_chat_but_explicit_mode_wins(archive_home) -> None:
    tid = _seed()
    focused = read_thread(tid, around_event=1, context_turns=0)
    assert "here is the first answer" in focused
    assert "[tool: Bash" not in focused
    user = read_thread(tid, around_event=1, context_turns=0, mode="user")
    assert "here is the first answer" not in user
    full = read_thread(tid, around_event=4, context_turns=0, mode="full", tool_results=True)
    assert "TOOLOUTPUT" in full and "match:4" in full


def test_around_event_hidden_event_falls_back_to_position(archive_home) -> None:
    # A search hit can be an event the transcript hides (lifecycle noise, codex
    # machinery). Opening it must land on the turn at its position, not error.
    corpus = list(_CORPUS)
    corpus.insert(9, ("progress", {"status": "working"}, 8))  # hidden: _SKIP_TYPES
    tid = _seed(corpus)
    out = read_thread(tid, around_event=10, context_turns=0)
    assert "focused on event 10 in turn 2" in _header(out)
    assert "second question about database" in out
    assert "match:" not in out  # the hit itself isn't rendered, so no marker


def test_around_event_validation(archive_home) -> None:
    tid = _seed()
    assert "was not found in thread" in read_thread(tid, around_event=999)
    assert "zero or greater" in read_thread(tid, around_event=1, context_turns=-1)


def test_max_chars_budget_and_footer_hint_in_full(archive_home) -> None:
    tid = _seed()
    out = read_thread(tid, mode="full", max_chars=50)
    assert "⚠ CHUNKED" in out
    assert "~50-char budget" in out
    # non-user view → continuation carries user_only=false
    assert "user_only=false" in out


def test_header_reports_char_count(archive_home) -> None:
    tid = _seed()
    assert "(~" in _header(read_thread(tid)) and "chars)" in _header(read_thread(tid))


# ── empty / topic / missing ──────────────────────────────────────────────────

def test_missing_thread(archive_home) -> None:
    init_db()
    assert "not found" in read_thread(424242)


def test_empty_conversation(archive_home) -> None:
    tid = _seed([])
    assert "has no messages yet" in read_thread(tid)


def test_topic_thread_message(archive_home) -> None:
    tid = _seed([], thread_type="topic", title="A Topic")
    with use_session() as s:
        s.add(TopicMessage(topic_id=tid, event_id=1, thread_id=tid, quote="q", actor="test"))
        s.commit()
    out = read_thread(tid)
    assert f"# Topic {tid}: A Topic" in out
    assert "Citations (1)" in out and "[event:1] q" in out
    # summary path gives the same topic page
    assert f"# Topic {tid}: A Topic" in read_thread(tid, summary=True)


# ── thread_id ref resolution: ULID PK, legacy int, or provider session uuid ──

_UUID = "3f2a9c1e-0b44-4d27-9a11-77c0de9912ab"


def test_read_by_bare_session_uuid(archive_home) -> None:
    """A bare provider session uuid (the thread's source_id) resolves to the thread.

    Sources differ in whether they prefix their ids; one that stores the bare
    session uuid has to resolve on the exact match, not only the suffix path the
    prefixed sources take."""
    tid = _seed(source="claude-code", source_id=_UUID)
    out = read_thread(_UUID)
    assert "first question about authentication" in out
    assert f"# Thread {tid}:" in out  # resolved to the ULID id in the header


def test_read_by_project_prefixed_source_id(archive_home) -> None:
    """The watcher stores source_id as ``{project}:{uuid}``; a bare uuid suffix-matches."""
    tid = _seed(source="claude-code", source_id=f"my-project:{_UUID}")
    out = read_thread(_UUID)
    assert f"# Thread {tid}:" in out
    assert "first question about authentication" in out


def test_legacy_integer_id_resolves_via_alias(archive_home) -> None:
    """A digit ref resolves through ``Thread.legacy_id``, the permanent alias
    for pre-ULID integer ids — never the primary key."""
    tid = _seed(legacy_id=13, source_id=_UUID)
    out = read_thread(13)
    assert f"# Thread {tid}:" in out  # header shows the ULID, not the legacy int
    assert read_thread(13) == read_thread("13") == read_thread(tid)


def test_ulid_ref_resolves_including_lowercase(archive_home) -> None:
    """The ULID PK resolves directly, case-insensitively (Crockford base32)."""
    tid = _seed()
    assert read_thread(tid) == read_thread(tid.lower())
    assert f"# Thread {tid}:" in read_thread(tid.lower())


def test_numeric_source_id_falls_through_to_resolution(archive_home) -> None:
    """A digit ref that matches no legacy_id falls back to source_id (e.g. grok
    numeric session ids)."""
    tid = _seed(source="grok", source_id="987654")
    out = read_thread("987654")  # not a legacy id, but a source_id
    assert f"# Thread {tid}:" in out


def test_unknown_uuid_reports_not_found(archive_home) -> None:
    _seed(source_id=_UUID)
    assert "not found" in read_thread("00000000-dead-beef-0000-000000000000")


def test_resolve_thread_ref_unit(archive_home) -> None:
    tid = _seed(legacy_id=16, source="claude-code", source_id=f"proj:{_UUID}")
    with use_session() as s:
        assert resolve_thread_ref(s, 16) == tid            # legacy alias, int
        assert resolve_thread_ref(s, "16") == tid          # legacy alias, digit string
        assert resolve_thread_ref(s, tid) == tid           # ULID PK
        assert resolve_thread_ref(s, tid.lower()) == tid   # ULID, case-insensitive
        assert resolve_thread_ref(s, _UUID) == tid          # suffix match
        assert resolve_thread_ref(s, f"proj:{_UUID}") == tid  # exact match
        assert resolve_thread_ref(s, "nope") is None


def test_resolve_ref_underscore_matches_literally(archive_home) -> None:
    """A ref's ``_`` is escaped in the suffix LIKE — unescaped it would act as a
    single-char wildcard and silently resolve to the WRONG thread."""
    _seed(source_id="proj:abcXdef")
    with use_session() as s:
        assert resolve_thread_ref(s, "abc_def") is None  # no wildcard match
    literal_tid = mint_ulid()
    with use_session() as s:
        s.add(Thread(id=literal_tid, name="t41", title="literal",
                     thread_type="conversation",
                     source="claude-code", source_id="proj:abc_def",
                     inserted_at=_dt(0), updated_at=_dt(2)))
        s.commit()
    with use_session() as s:
        assert resolve_thread_ref(s, "abc_def") == literal_tid


def test_resolve_scoped_to_a_source_uses_only_its_separators(archive_home) -> None:
    """Naming the source narrows the shape as well as the rows.

    ``source_id`` composition is the provider's own knowledge — claude-code
    stores ``{project}:{uuid}``, codex ``rollout-{ts}-{uuid}``, most providers the
    bare uuid. A caller that knows where a reference came from should not have a
    uuid resolve through a separator only some *other* provider composes with."""
    from thread_archive._store.resolve import resolve_session_source_id

    tid = _seed(source="cursor", source_id=f"weird-{_UUID}")
    with use_session() as s:
        # cursor declares no separators: its source_id IS the session id, so a
        # bare uuid with a prefix in front of it is not a reference to it.
        assert resolve_session_source_id(s, _UUID, source="cursor") is None
        assert resolve_session_source_id(s, f"weird-{_UUID}", source="cursor") == tid


def test_resolve_across_sources_tries_every_declared_separator(archive_home) -> None:
    """Unscoped, a reference of unknown origin is resolved as widely as any
    provider composes — which is why declaring a separator narrowly matters."""
    from thread_archive._store.resolve import resolve_session_source_id

    tid = _seed(source="codex", source_id=f"rollout-2026-01-01T10-00-00-{_UUID}")
    with use_session() as s:
        assert resolve_session_source_id(s, _UUID) == tid
        assert resolve_session_source_id(s, _UUID, source="codex") == tid


def test_resolve_newest_thread_wins(archive_home) -> None:
    """Two threads sharing a source_id → the most recently updated resolves."""
    _seed(source_id=_UUID, updated_minute=1)
    # second thread, same uuid, newer updated_at — seed without re-init_db
    newer_tid = mint_ulid()
    with use_session() as s:
        s.add(Thread(id=newer_tid, name="t21", title="newer", thread_type="conversation",
                     source="claude-code", source_id=_UUID,
                     inserted_at=_dt(0), updated_at=_dt(5)))
        s.commit()
    with use_session() as s:
        assert resolve_thread_ref(s, _UUID) == newer_tid


def test_resolve_via_import_state_watermark(archive_home) -> None:
    """A session uuid known only to ImportState resolves — the compaction-continuation
    shape: the continuation's events merge into the original thread (no Thread row
    carries its uuid) but its watermark is its own. That uuid is exactly what an agent
    inside the continued session holds and passes to thread_read."""
    from thread_archive._store import ImportState

    cont_uuid = "cccccccc-1111-2222-3333-444444444444"
    tid = _seed(source_id=f"proj:{_UUID}")
    with use_session() as s:
        s.add(ImportState(source="claude-code", source_id=f"proj:{cont_uuid}",
                          thread_id=tid, last_import_at=_dt(6)))
        s.commit()
    with use_session() as s:
        assert resolve_thread_ref(s, cont_uuid) == tid            # bare-uuid suffix
        assert resolve_thread_ref(s, f"proj:{cont_uuid}") == tid  # exact watermark


def test_resolvers_agree_across_surfaces(archive_home) -> None:
    """The MCP reader and the web viewer's archive-link answer alike for the same
    session id — whether it lives in Thread.source_id or only in ImportState."""
    from thread_archive._store import ImportState
    from thread_archive._web import resolve_archive_link

    is_only = "dddddddd-1111-2222-3333-444444444444"
    tid = _seed(source_id=f"proj:{_UUID}")
    with use_session() as s:
        s.add(ImportState(source="claude-code", source_id=f"proj:{is_only}",
                          thread_id=tid, last_import_at=_dt(6)))
        s.commit()
    for ref in (_UUID, is_only):
        with use_session() as s:
            assert resolve_thread_ref(s, ref) == tid
        assert resolve_archive_link(ref) == tid
