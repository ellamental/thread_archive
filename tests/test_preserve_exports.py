"""Drop-site regressions for the bulk export importer (claude.ai / xAI): no
provider record may be silently skipped/dropped/truncated on import.

Covers the drop-sites guarded in ``importers/exports.py``:
3. An xAI/Grok response with no ``message`` text must not be dropped
   (``continue``) — that loses image/attachment/generated-media and tool-only
   turns. It must be preserved with its full raw.
4. A conversation that raises mid-import must not be swallowed with a one-line
   warning (no traceback) and silently skipped. It must log the traceback and
   surface a stub thread carrying id + raw + error.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from thread_archive._importers.exports import (
    _xai_conversation_messages,
    import_xai_export,
)
from thread_archive._store import Event, Thread, get_session, init_db


def _events():
    with get_session() as s:
        return [(e.event_type, e.payload) for e in s.execute(select(Event)).scalars()]


def _threads():
    with get_session() as s:
        return {t.source_id: t for t in s.execute(select(Thread)).scalars()}


def _write_xai(archive_home, payload) -> "object":
    export_dir = archive_home / "xai" / "export_data" / "u"
    export_dir.mkdir(parents=True)
    (export_dir / "prod-grok-backend.json").write_text(json.dumps(payload), encoding="utf-8")
    return archive_home / "xai"


# ── 3. Text-less xAI responses are preserved, not dropped ─────────────────────


def test_xai_textless_assistant_response_preserved() -> None:
    """A text-less assistant turn (image-gen / tool-only) survives with its raw —
    the old `if not text_str: continue` dropped it entirely."""
    bundle = {
        "conversation": {"id": "c1", "create_time": "2026-01-01T10:00:00Z"},
        "responses": [
            {"response": {"_id": "r1", "sender": "human", "message": "make an image",
                          "create_time": "2026-01-01T10:00:00Z"}},
            {"response": {"_id": "r2", "sender": "ASSISTANT", "message": "",
                          "model": "grok-4-image", "create_time": "2026-01-01T10:00:05Z",
                          "generated_image_urls": ["https://x/img.png"]}},
        ],
    }
    msgs = _xai_conversation_messages(bundle)
    assert len(msgs) == 2, "text-less response was dropped"
    textless = msgs[1]
    assert textless["provider_message_id"] == "r2"
    blob = json.dumps(textless)
    assert "img.png" in blob, "raw content of the text-less turn was lost"
    assert textless["provider_data"].get("raw", {}).get("model") == "grok-4-image"


def test_xai_textless_human_response_preserved() -> None:
    """A text-less human turn (an uploaded image/attachment with no text) survives —
    routed through the generic role so the builder can't discard it."""
    bundle = {
        "conversation": {"id": "c2", "create_time": "2026-01-01T10:00:00Z"},
        "responses": [
            {"response": {"_id": "h1", "sender": "human", "message": "",
                          "create_time": "2026-01-01T10:00:00Z",
                          "file_attachments": [{"name": "photo.png"}]}},
        ],
    }
    msgs = _xai_conversation_messages(bundle)
    assert len(msgs) == 1
    # A generic (non user/assistant/system) role so the shared builder preserves it
    # as a `message` event rather than discarding a text-less user turn.
    assert msgs[0]["role"] not in ("user", "assistant", "system")
    assert "photo.png" in json.dumps(msgs[0])


def test_xai_textless_response_reaches_the_store(archive_home) -> None:
    """End-to-end: a text-less turn is not just kept in the normalized list but
    actually written as an event (a real `message` event carrying the raw)."""
    payload = {"conversations": [{
        "conversation": {"id": "conv-textless", "title": "Media", "create_time": "2026-01-01T10:00:00Z"},
        "responses": [
            {"response": {"_id": "r1", "sender": "human", "message": "hi",
                          "create_time": "2026-01-01T10:00:00Z"}},
            {"response": {"_id": "r2", "sender": "ASSISTANT", "message": "",
                          "model": "grok-4", "create_time": "2026-01-01T10:00:05Z",
                          "generated_image_urls": ["https://x/pic.png"]}},
        ],
    }]}
    init_db()
    root = _write_xai(archive_home, payload)
    result = import_xai_export(root)
    assert result.imported == 1
    blob = json.dumps(_events())
    assert "pic.png" in blob, "text-less turn never reached the store"
    types = [t for (t, _p) in _events()]
    assert "message" in types, "text-less turn was not preserved as a message event"


def test_xai_normal_text_turns_unchanged(archive_home) -> None:
    """Guard: text turns still import as normal user/assistant events + models."""
    payload = {"conversations": [{
        "conversation": {"id": "conv-ok", "title": "Chat", "create_time": "2026-01-01T10:00:00Z"},
        "responses": [
            {"response": {"_id": "r1", "sender": "human", "message": "q",
                          "create_time": "2026-01-01T10:00:00Z"}},
            {"response": {"_id": "r2", "sender": "ASSISTANT", "message": "a",
                          "model": "grok-4", "create_time": "2026-01-01T10:00:05Z"}},
        ],
    }]}
    init_db()
    root = _write_xai(archive_home, payload)
    result = import_xai_export(root)
    assert result.imported == 1
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "grok")).scalar_one()
        assert (t.source_metadata or {}).get("models") == ["grok-4"]
    types = {t for (t, _p) in _events()}
    assert "user_message_sent" in types


# ── 4. A conversation that blows up mid-import surfaces a stub ─────────────────


def test_failed_conversation_preserved_as_stub(archive_home, monkeypatch) -> None:
    """A conversation that raises mid-import must not be silently skipped: it must
    surface a stub thread carrying id + raw + error, and count as `errored`."""
    payload = {"conversations": [{
        "conversation": {"id": "conv-boom", "title": "Boom", "create_time": "2026-01-01T10:00:00Z"},
        "responses": [
            {"response": {"_id": "r1", "sender": "human", "message": "q",
                          "create_time": "2026-01-01T10:00:00Z"}},
        ],
    }]}
    init_db()
    root = _write_xai(archive_home, payload)

    import thread_archive._importers.exports as exports_mod

    real_assemble = exports_mod.assemble_events
    calls = {"n": 0}

    def _boom(session, thread_id, messages, builder, **kw):
        # Fail the real conversation's assemble, but let the stub's assemble through.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated assemble failure")
        return real_assemble(session, thread_id, messages, builder, **kw)

    monkeypatch.setattr(exports_mod, "assemble_events", _boom)

    result = import_xai_export(root)  # must not raise
    assert result.errored == 1, "failed conversation was not counted as errored"
    threads = _threads()
    assert "conv-boom:import-error" in threads, "failed conversation vanished with no stub"
    blob = json.dumps(_events())
    assert "simulated assemble failure" in blob, "the error was not preserved on the stub"
    # The real source_id stays unimported (retryable), not shadowed by the stub.
    assert "conv-boom" not in threads


def test_failed_conversation_stub_idempotent(archive_home, monkeypatch) -> None:
    """Re-running an export whose conversation keeps failing must not restack the
    stub events."""
    payload = {"conversations": [{
        "conversation": {"id": "conv-dupe", "title": "Dupe", "create_time": "2026-01-01T10:00:00Z"},
        "responses": [
            {"response": {"_id": "r1", "sender": "human", "message": "q",
                          "create_time": "2026-01-01T10:00:00Z"}},
        ],
    }]}
    init_db()
    root = _write_xai(archive_home, payload)

    import thread_archive._importers.exports as exports_mod

    real_assemble = exports_mod.assemble_events

    def _fail_real(session, thread_id, messages, builder, **kw):
        # Only the stub message (role ending in _import_error) is allowed through.
        if messages and str(messages[0].get("role", "")).endswith("_import_error"):
            return real_assemble(session, thread_id, messages, builder, **kw)
        raise RuntimeError("still failing")

    monkeypatch.setattr(exports_mod, "assemble_events", _fail_real)

    import_xai_export(root)
    n = len(_events())
    import_xai_export(root, force=True)  # retry; still failing
    assert len(_events()) == n, "failed-conversation stub restacked on retry"
