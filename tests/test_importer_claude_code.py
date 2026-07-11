"""Claude Code importer: import a fixture transcript, prove repeated
import is idempotent (both by watermark and by dedup-key), and that the watermark
advances only on new content.
"""

from __future__ import annotations

import json

from sqlalchemy import delete, select

from thread_archive._importers import import_session_incremental
from thread_archive._store import Event, ImportState, Thread, _base, get_session, init_db
from thread_archive._truth import checkpoint, jsonl_log, reindex

USER = {
    "type": "user",
    "uuid": "u1",
    "timestamp": "2026-01-01T10:00:00Z",
    "sessionId": "s1",
    "cwd": "/proj",
    "message": {"role": "user", "content": "hello world"},
}
ASSISTANT = {
    "type": "assistant",
    "uuid": "a1",
    "timestamp": "2026-01-01T10:00:05Z",
    "sessionId": "s1",
    "message": {
        "role": "assistant",
        "model": "claude-opus-4",
        "content": [{"type": "text", "text": "hi there"}],
    },
}


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _event_count(thread_id=None) -> int:
    with get_session() as s:
        stmt = select(Event)
        if thread_id is not None:
            stmt = stmt.where(Event.thread_id == thread_id)
        return len(s.execute(stmt).scalars().all())


def _imported_source_ids(thread_id: int) -> set[str]:
    """The set of source message ids that survived import, recovered from each
    event's dedup_key anchor.

    The CC parser maps a line's ``uuid`` → ``provider_message_id``, and the builder
    stamps that id as the dedup_key anchor (``{id}:{event_type}:{block}:{hash}``) on
    *every* event it builds from the message. So the distinct anchors over a thread's
    events are exactly the source messages that produced at least one event. An event
    with no provider id falls back to a ``c=<hash>`` anchor, excluded here."""
    with get_session() as s:
        events = s.execute(select(Event).where(Event.thread_id == thread_id)).scalars().all()
    return {
        e.dedup_key.split(":", 1)[0]
        for e in events
        if e.dedup_key and not e.dedup_key.startswith("c=")
    }


def test_import_creates_thread_and_events(archive_home) -> None:
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])

    result = import_session_incremental(f, "proj:s1")
    assert result.is_new_thread is True
    assert result.events_created > 0
    assert result.last_message_uuid == "a1"

    with get_session() as s:
        thread = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        tid = thread.id
        assert thread.source_id == "proj:s1"
        assert thread.source_metadata["cwd"] == "/proj"
        assert thread.source_metadata["project_dir"] == "proj"
        assert thread.source_metadata["models"] == ["claude-opus-4"]  # backfilled from events
        events = s.execute(select(Event).where(Event.thread_id == thread.id)).scalars().all()

    user_events = [e for e in events if e.event_type == "user_message_sent"]
    assert any(e.payload.get("content") == "hello world" for e in user_events)

    # Truth tie-in: the imported events are in the thread's per-thread truth file.
    tf = next((archive_home / "truth" / "threads").rglob(f"{tid}.jsonl"))
    recs = [json.loads(ln) for ln in tf.read_text().splitlines() if ln.strip()]
    assert sum(1 for r in recs if r["type"] == "event") == len(events)
    assert any(r["type"] == "thread" for r in recs)  # metadata record present


def test_every_source_message_is_imported(archive_home) -> None:
    """Fidelity/no-drop: the count of unique source messages (by CC ``uuid``) equals
    the count of unique message ids that made it into the archive, and the two sets
    are identical. This is the guard against the importer silently dropping turns —
    a whole message vanishing between the source document and the event log."""
    init_db()
    lines: list[dict] = []
    for i in range(1, 4):
        lines.append({
            "type": "user", "uuid": f"u{i}", "timestamp": f"2026-01-01T10:0{i}:00Z",
            "sessionId": "s1", "cwd": "/proj",
            "message": {"role": "user", "content": f"question {i}"},
        })
        lines.append({
            "type": "assistant", "uuid": f"a{i}", "timestamp": f"2026-01-01T10:0{i}:05Z",
            "sessionId": "s1",
            "message": {"role": "assistant", "model": "claude-opus-4",
                        "content": [{"type": "text", "text": f"answer {i}"}]},
        })
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, lines)

    result = import_session_incremental(f, "proj:s1")

    source_ids = {ln["uuid"] for ln in lines}
    imported_ids = _imported_source_ids(result.thread_id)
    assert imported_ids == source_ids
    assert len(imported_ids) == len(source_ids) == 6


def test_ide_context_is_preserved_as_events(archive_home) -> None:
    """IDE context (opened files, selections) is preserved as ``ide_context`` events,
    not stripped away: a context-only turn survives import (its uuid is represented),
    and a turn carrying both text and a selection keeps *both*. Guards the fidelity
    hole of dropping the whole turn once its tags are stripped."""
    init_db()
    context_only = {
        "type": "user", "uuid": "u_ctx", "timestamp": "2026-01-01T10:00:03Z",
        "sessionId": "s1",
        "message": {"role": "user",
                    "content": "<ide_selection>def foo(): pass</ide_selection>"},
    }
    text_plus_ide = {
        "type": "user", "uuid": "u_mix", "timestamp": "2026-01-01T10:00:07Z",
        "sessionId": "s1",
        "message": {"role": "user",
                    "content": "fix this\n<ide_selection>def foo(): pass</ide_selection>"},
    }
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, context_only, text_plus_ide, ASSISTANT])

    result = import_session_incremental(f, "proj:s1")

    # No unique source turn is dropped, including the context-only one.
    imported_ids = _imported_source_ids(result.thread_id)
    assert imported_ids == {"u1", "u_ctx", "u_mix", "a1"}

    with get_session() as s:
        events = s.execute(
            select(Event).where(Event.thread_id == result.thread_id)
        ).scalars().all()

    # One ide_context event per selection: the context-only turn and the mixed turn.
    ide = [e for e in events if e.event_type == "ide_context"]
    assert {e.dedup_key.split(":", 1)[0] for e in ide} == {"u_ctx", "u_mix"}
    assert all("def foo(): pass" in e.payload.get("content", "") for e in ide)

    # The mixed turn keeps its user text too — the selection didn't displace it.
    mix_text = [
        e.payload.get("content", "").strip() for e in events
        if e.event_type == "user_message_sent" and e.dedup_key.split(":", 1)[0] == "u_mix"
    ]
    assert mix_text == ["fix this"]


def test_model_command_captured_as_event(archive_home) -> None:
    """A manual ``/model`` switch is preserved as a ``model_change`` event. Claude Code
    records it as two user lines — the command and a ``Set model to X`` stdout — whose
    tags are otherwise stripped to nothing; the stdout line carries the switch, and
    (crucially) it isn't deduped away against the same-timestamp command line."""
    init_db()
    cmd = {
        "type": "user", "uuid": "u_cmd", "timestamp": "2026-01-01T10:00:10Z",
        "sessionId": "s1",
        "message": {"role": "user",
                    "content": "<command-name>/model</command-name>\n"
                               "            <command-args>claude-fable-5[1m]</command-args>"},
    }
    stdout = {
        "type": "user", "uuid": "u_out", "timestamp": "2026-01-01T10:00:10Z",
        "sessionId": "s1",
        "message": {"role": "user",
                    "content": "<local-command-stdout>Set model to claude-fable-5</local-command-stdout>"},
    }
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT, cmd, stdout])

    result = import_session_incremental(f, "proj:s1")

    with get_session() as s:
        changes = s.execute(
            select(Event).where(
                Event.thread_id == result.thread_id,
                Event.event_type == "model_change",
            )
        ).scalars().all()
    assert len(changes) == 1
    payload = changes[0].payload
    assert payload["to"] == "claude-fable-5" and payload["trigger"] == "user"


def test_subagent_filed_as_hidden_system_thread(archive_home) -> None:
    """A ``agent-*`` subagent transcript files as a hidden ``thread_type='system'``
    thread with a 🤖 title and parent lineage stamped — kept out of the sidebar."""
    init_db()
    f = archive_home / "agent.jsonl"
    sub_user = {"type": "user", "uuid": "su1", "timestamp": "2026-01-01T10:00:00Z",
                "sessionId": "parent-sess", "agentId": "agent-abc", "cwd": "/proj",
                "message": {"role": "user", "content": "do the subtask"}}
    sub_asst = {"type": "assistant", "uuid": "sa1", "timestamp": "2026-01-01T10:00:05Z",
                "sessionId": "parent-sess", "agentId": "agent-abc",
                "message": {"role": "assistant", "model": "claude-opus-4",
                            "content": [{"type": "text", "text": "done"}]}}
    _write_jsonl(f, [sub_user, sub_asst])

    result = import_session_incremental(f, "proj:agent-abc")
    assert result.is_new_thread is True
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        assert t.thread_type == "system"
        assert t.title.startswith("🤖")
        assert t.source_metadata.get("is_subagent") is True
        assert t.source_metadata.get("agent_id") == "agent-abc"
        assert t.source_metadata.get("parent_session_id") == "parent-sess"


def test_custom_title_wins_over_ai_title(archive_home) -> None:
    """A user rename (``custom-title``) beats the auto-titler's ``ai-title``."""
    init_db()
    f = archive_home / "titled.jsonl"
    _write_jsonl(f, [
        {"type": "ai-title", "aiTitle": "Auto Generated Title"},
        USER, ASSISTANT,
        {"type": "custom-title", "customTitle": "My Renamed Session"},
    ])
    import_session_incremental(f, "proj:t1")
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        assert t.title == "My Renamed Session"


def test_ai_title_used_when_no_rename(archive_home) -> None:
    """With no user rename, the auto-titler's ``ai-title`` is the title."""
    init_db()
    f = archive_home / "ai.jsonl"
    _write_jsonl(f, [{"type": "ai-title", "aiTitle": "Memory requirements for MoE"}, USER, ASSISTANT])
    import_session_incremental(f, "proj:t2")
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        assert t.title == "Memory requirements for MoE"


def test_command_invocation_titles_with_command_name(archive_home) -> None:
    """A bare slash-command session is titled with the command, not the XML noise
    or the injected skill doc that follows."""
    init_db()
    f = archive_home / "cmd.jsonl"
    cmd_user = {"type": "user", "uuid": "uc", "timestamp": "2026-01-01T10:00:00Z", "cwd": "/proj",
                "message": {"role": "user",
                            "content": "<command-message>cleanup</command-message>\n<command-name>/cleanup</command-name>"}}
    _write_jsonl(f, [cmd_user, ASSISTANT])
    import_session_incremental(f, "proj:cmd")
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        assert t.title == "/cleanup"


def test_description_and_models_backfilled(archive_home) -> None:
    """A new thread carries a one-line description (first user message) and a
    denormalized models list from its api_request_completed events."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])
    import_session_incremental(f, "proj:s1")
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        assert t.description == "hello world"
        assert t.source_metadata.get("models") == ["claude-opus-4"]


def test_title_resyncs_on_rename(archive_home) -> None:
    """An existing thread re-syncs its title when a later ``custom-title`` appears."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])
    import_session_incremental(f, "proj:s1")
    with get_session() as s:
        assert s.execute(select(Thread)).scalar_one().title == "hello world"

    u2 = {**USER, "uuid": "u2", "timestamp": "2026-01-01T10:01:00Z",
          "message": {"role": "user", "content": "more"}}
    a2 = {**ASSISTANT, "uuid": "a2", "timestamp": "2026-01-01T10:01:05Z",
          "message": {"role": "assistant", "model": "claude-opus-4",
                      "content": [{"type": "text", "text": "ok"}]}}
    _write_jsonl(f, [USER, ASSISTANT, {"type": "custom-title", "customTitle": "Renamed Later"}, u2, a2])
    import_session_incremental(f, "proj:s1")
    with get_session() as s:
        assert s.execute(select(Thread)).scalar_one().title == "Renamed Later"


def test_metadata_survives_reindex(archive_home) -> None:
    """Title, description, and the backfilled models list are written to truth, so a
    ``rm index.db && reindex`` reproduces them."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [{"type": "custom-title", "customTitle": "Kept Title"}, USER, ASSISTANT])
    import_session_incremental(f, "proj:s1")
    checkpoint()

    from thread_archive._store import get_engine
    get_engine().dispose()
    jsonl_log.reset_handles()
    _base.close_engine()
    for suffix in ("", "-wal", "-shm"):
        (archive_home / f"index.db{suffix}").unlink(missing_ok=True)
    reindex()

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        assert t.title == "Kept Title"
        assert t.description == "hello world"
        assert t.source_metadata.get("models") == ["claude-opus-4"]


def test_hook_context_sidecar_imported(archive_home) -> None:
    """A sibling ``<session>.context.jsonl`` imports as hook_context events on the
    thread; empty-context lines are skipped and re-import adds nothing."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])
    _write_jsonl(archive_home / "sess.context.jsonl", [
        {"hook": "UserPromptSubmit", "context": "injected context here", "ts": "2026-01-01T10:00:01"},
        {"hook": "PreToolUse", "context": "", "ts": "2026-01-01T10:00:02"},  # empty → skipped
    ])
    import_session_incremental(f, "proj:s1")

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        hooks = s.execute(
            select(Event).where(Event.thread_id == t.id, Event.event_type == "hook_context")
        ).scalars().all()
    assert len(hooks) == 1
    assert hooks[0].payload["context"] == "injected context here"
    assert hooks[0].payload["hook_name"] == "UserPromptSubmit"

    # Re-import: the sidecar cursor short-circuits, nothing doubles.
    import_session_incremental(f, "proj:s1")
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        n = len(s.execute(
            select(Event).where(Event.thread_id == t.id, Event.event_type == "hook_context")
        ).scalars().all())
    assert n == 1


def test_source_override_labels_thread_and_watermark(archive_home) -> None:
    """``source=`` overrides the default ``"claude-code"`` label end to end — the
    one knob a deployment watcher (e.g. cloth) needs to import Claude-Code-shaped
    JSONL under its own identity. Thread, name, and import-state all carry it."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])

    result = import_session_incremental(f, "cloth-cli-7", source="cloth")
    assert result.is_new_thread is True
    assert result.events_created > 0

    with get_session() as s:
        thread = s.execute(select(Thread).where(Thread.source == "cloth")).scalar_one()
        assert thread.source_id == "cloth-cli-7"
        assert thread.name == "cloth:cloth-cli-7"
        # No claude-code thread leaked in under the default.
        assert s.execute(select(Thread).where(Thread.source == "claude-code")).first() is None
        # Watermark is keyed on the overridden source, so the resume path matches.
        state = s.execute(select(ImportState).where(ImportState.source == "cloth")).scalar_one()
        assert state.source_id == "cloth-cli-7"

    # Resume resolves the same thread under the same source — no fork, no re-import.
    again = import_session_incremental(f, "cloth-cli-7", source="cloth")
    assert again.events_created == 0
    assert again.is_new_thread is False


def test_reimport_unchanged_file_is_noop(archive_home) -> None:
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])

    import_session_incremental(f, "proj:s1")
    n1 = _event_count()
    result = import_session_incremental(f, "proj:s1")
    assert result.events_created == 0  # file-size watermark short-circuits
    assert _event_count() == n1


def test_dedup_key_idempotent_even_without_watermark(archive_home) -> None:
    """The real idempotence guarantee: wipe the watermark, re-import the same file,
    and the dedup-key membership check still adds nothing."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])

    import_session_incremental(f, "proj:s1")
    n1 = _event_count()

    with get_session() as s:
        s.execute(delete(ImportState))
        s.commit()

    result = import_session_incremental(f, "proj:s1")
    assert result.events_created == 0
    assert result.is_new_thread is False  # resolved the existing thread, didn't recreate
    assert _event_count() == n1


def test_reimport_after_watermark_loss_does_not_double(archive_home) -> None:
    """The reindex-safety guard: after ``rm index.db && reindex`` the watermark is
    gone but the thread+events are rebuilt from truth — and a bulk-seeded archive's
    stored dedup_keys need not match a fresh import's. Re-importing the file must NOT
    re-insert those events; the thread is adopted (watermark re-stamped at EOF)."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])

    import_session_incremental(f, "proj:s1")
    n1 = _event_count()

    # Simulate the post-reindex state: events present but with keys a fresh import
    # won't reproduce, and no watermark.
    with get_session() as s:
        for e in s.execute(select(Event)).scalars().all():
            if e.dedup_key:
                e.dedup_key = "STALE-" + e.dedup_key
        s.execute(delete(ImportState))
        s.commit()

    result = import_session_incremental(f, "proj:s1")
    assert result.events_created == 0          # adopted, not re-imported
    assert _event_count() == n1                # nothing doubled


def test_watermark_advances_on_appended_turns(archive_home) -> None:
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])
    import_session_incremental(f, "proj:s1")
    n1 = _event_count()

    user2 = {**USER, "uuid": "u2", "timestamp": "2026-01-01T10:01:00Z",
             "message": {"role": "user", "content": "second question"}}
    assistant2 = {**ASSISTANT, "uuid": "a2", "timestamp": "2026-01-01T10:01:05Z",
                  "message": {"role": "assistant", "model": "claude-opus-4",
                              "content": [{"type": "text", "text": "second answer"}]}}
    _write_jsonl(f, [USER, ASSISTANT, user2, assistant2])

    result = import_session_incremental(f, "proj:s1")
    assert result.events_created > 0
    assert _event_count() > n1

    with get_session() as s:
        events = s.execute(select(Event)).scalars().all()
        state = s.execute(select(ImportState)).scalar_one()
    assert any(
        e.payload.get("content") == "second question"
        for e in events
        if e.event_type == "user_message_sent"
    )
    assert state.last_line_count == 4  # watermark advanced to the full file
    assert state.last_message_uuid == "a2"


def test_import_then_checkpoint_reindex_is_lossless(archive_home) -> None:
    """Import → checkpoint → delete index.db → reindex must
    reproduce the thread *and* its events (the importer streams events to truth;
    checkpoint snapshots the thread)."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])
    import_session_incremental(f, "proj:s1")
    checkpoint()

    with get_session() as s:
        before_events = [
            (e.event_type, e.payload.get("content")) for e in
            s.execute(select(Event).order_by(Event.id)).scalars()
        ]
        before_thread = s.execute(select(Thread)).scalar_one()
        before_title = before_thread.title

    # Nuke the index; truth is the only surviving copy.
    from thread_archive._store import get_engine

    get_engine().dispose()
    jsonl_log.reset_handles()
    _base.close_engine()
    for suffix in ("", "-wal", "-shm"):
        (archive_home / f"index.db{suffix}").unlink(missing_ok=True)

    counts = reindex()
    assert counts["threads"] == 1
    assert counts["events"] == len(before_events)

    with get_session() as s:
        after_events = [
            (e.event_type, e.payload.get("content")) for e in
            s.execute(select(Event).order_by(Event.id)).scalars()
        ]
        after_thread = s.execute(select(Thread)).scalar_one()
    assert after_events == before_events
    assert after_thread.source_id == "proj:s1"
    assert after_thread.title == before_title


def test_empty_import_leaves_no_ghost_thread_or_truth_file(archive_home) -> None:
    """A session whose lines yield zero events (thread created, then discarded)
    must leave no thread row AND no truth file. A ghost ``threads/<id>.jsonl``
    would drift ``verify`` and be resurrected as an empty thread on reindex."""
    init_db()
    f = archive_home / "empty.jsonl"
    # A user line with empty content parses but builds no events.
    _write_jsonl(f, [{**USER, "message": {"role": "user", "content": ""}}])

    result = import_session_incremental(f, "proj:empty")
    assert result.events_created == 0
    assert result.thread_id == 0
    assert result.is_new_thread is False

    with get_session() as s:
        row = s.execute(select(Thread).where(Thread.source_id == "proj:empty")).first()
        assert row is None
    ghosts = list((archive_home / "truth" / "threads").rglob("*.jsonl"))
    assert ghosts == [], f"ghost truth file(s) written for a discarded thread: {ghosts}"
