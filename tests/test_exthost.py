"""The exthost steering-recovery watcher: capture mid-turn steering messages that
Claude Code drops from the session JSONL, without duplicating the ones it kept.

A webview message is "lost" iff its uuid is absent from the session JSONL — that
uuid gate is what keeps a persisted message (whose JSONL copy may carry injected
IDE context, so its content-hash differs) from being re-imported as a near-dup.
"""

from __future__ import annotations

import json

from sqlalchemy import select

import thread_archive.watcher.exthost as ex
from thread_archive.importers._state import create_thread
from thread_archive.store import Event, get_session, init_db
from thread_archive.watcher.exthost import ExthostWatcher

CWD = "/proj"
SESSION = "sess-123-abc"
SOURCE_ID = "-proj:sess-123-abc"  # _munge_cwd("/proj") + ":" + SESSION


def _recv(obj: dict) -> str:
    return f'2026-06-25 10:00:00.000 [info] Received message from webview: {json.dumps(obj)}'


def _user_msg(uuid: str, text: str, channel: str = "chan1") -> dict:
    return {
        "type": "io_message",
        "channelId": channel,
        "message": {
            "type": "user",
            "uuid": uuid,
            "session_id": "",  # always empty for webview messages — why we need the channel
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        },
    }


def _control(channel: str = "chan1") -> dict:
    # A non-user io_message that carries the channel's cwd + resumed session id.
    return {"type": "io_message", "channelId": channel, "cwd": CWD, "resume": SESSION,
            "message": {"type": "request"}}


def _write_log(path, lines) -> None:
    path.write_text("\n".join(_recv(o) for o in lines) + "\n", encoding="utf-8")


def test_exthost_recovers_only_lost_steering(archive_home, tmp_path, monkeypatch) -> None:
    init_db()
    with get_session() as s:
        tid = create_thread(s, source="claude-code", source_id=SOURCE_ID, title="t")
        s.commit()

    # The session JSONL holds the persisted uuid but NOT the lost one.
    jsonl = tmp_path / f"{SESSION}.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "uuid": "uuid-persisted",
                    "message": {"role": "user", "content": "kept"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ex, "_session_jsonl", lambda cwd, sid: jsonl if sid == SESSION else None)
    monkeypatch.setattr(ex, "_too_fresh", lambda iso: False)  # grace under test separately

    log = tmp_path / "Claude VSCode.log"
    _write_log(log, [
        _control(),
        _user_msg("uuid-persisted", "kept"),          # in the JSONL → must be skipped
        _user_msg("uuid-lost", "the lost steering text"),  # absent → must be captured
    ])

    w = ExthostWatcher(log_globs=[str(log)])
    assert w.is_available()
    r = w.poll()
    assert r.events_created == 1, "only the lost steering message should be imported"

    with get_session() as s:
        texts = [e.payload.get("content") for e in
                 s.execute(select(Event).where(Event.thread_id == tid)).scalars()]
    assert "the lost steering text" in texts
    assert "kept" not in texts  # the persisted one was NOT re-imported via exthost

    # Idempotent: the unchanged log fingerprint-skips on the next poll.
    assert w.poll().events_created == 0


def test_exthost_skips_when_thread_absent(archive_home, tmp_path, monkeypatch) -> None:
    """A lost message for a session with no thread yet is left for a later poll
    (the JSONL importer will create the thread first)."""
    init_db()
    jsonl = tmp_path / f"{SESSION}.jsonl"
    jsonl.write_text("", encoding="utf-8")  # exists but empty → uuid not present
    monkeypatch.setattr(ex, "_session_jsonl", lambda cwd, sid: jsonl if sid == SESSION else None)
    monkeypatch.setattr(ex, "_too_fresh", lambda iso: False)

    log = tmp_path / "Claude VSCode.log"
    _write_log(log, [_control(), _user_msg("uuid-lost", "orphan steering")])

    r = ExthostWatcher(log_globs=[str(log)]).poll()
    assert r.events_created == 0  # no thread to attach to → nothing imported


def test_exthost_grace_defers_fresh_messages(archive_home, tmp_path, monkeypatch) -> None:
    """A message younger than the grace window is deferred (it may still be mid-write
    to the JSONL), and the log is left un-fingerprinted so it's re-checked next poll."""
    init_db()
    with get_session() as s:
        create_thread(s, source="claude-code", source_id=SOURCE_ID, title="t")
        s.commit()
    jsonl = tmp_path / f"{SESSION}.jsonl"
    jsonl.write_text("", encoding="utf-8")
    monkeypatch.setattr(ex, "_session_jsonl", lambda cwd, sid: jsonl if sid == SESSION else None)
    monkeypatch.setattr(ex, "_too_fresh", lambda iso: True)  # everything is "too fresh"

    log = tmp_path / "Claude VSCode.log"
    _write_log(log, [_control(), _user_msg("uuid-lost", "just typed")])

    w = ExthostWatcher(log_globs=[str(log)])
    assert w.poll().events_created == 0          # deferred, not imported
    assert str(log) not in w._seen_fp            # not fingerprinted → re-checked next poll
    # Once it ages out, the same unchanged log is reprocessed and the message lands.
    monkeypatch.setattr(ex, "_too_fresh", lambda iso: False)
    assert w.poll().events_created == 1


def test_exthost_skips_bare_commands(archive_home, tmp_path, monkeypatch) -> None:
    """Bare slash-commands aren't losses (CC drops them by design)."""
    init_db()
    with get_session() as s:
        create_thread(s, source="claude-code", source_id=SOURCE_ID, title="t")
        s.commit()
    jsonl = tmp_path / f"{SESSION}.jsonl"
    jsonl.write_text("", encoding="utf-8")
    monkeypatch.setattr(ex, "_session_jsonl", lambda cwd, sid: jsonl if sid == SESSION else None)

    log = tmp_path / "Claude VSCode.log"
    _write_log(log, [_control(), _user_msg("uuid-cmd", "/debrief")])

    assert ExthostWatcher(log_globs=[str(log)]).poll().events_created == 0
