"""The public Python library surface (thread_archive.*)."""

from __future__ import annotations

import json

from thread_archive import _api as ta

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello library"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi from the assistant"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def test_public_surface_round_trip(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])

    result = ta.import_path(f)  # claude-code by default
    assert result.events_created > 0

    hits = ta.search("hello")
    assert hits and all(h["thread_title"] for h in hits)

    thread_id = hits[0]["thread_id"]
    transcript = ta.read_thread(thread_id)
    assert "[USER" in transcript and "hello library" in transcript

    focused = ta.read_thread(thread_id, around_event=hits[0]["event_id"], context_turns=0)
    assert "match:" in focused and "hi from the assistant" in focused

    st = ta.status()
    assert st["threads"] == 1
    assert st["events"] > 0
    assert st["fts_indexed"] > 0
    assert st["home"] == str(archive_home)


def test_public_api_is_exported() -> None:
    for name in ("open_archive", "search", "read_thread", "read_thread_structured",
                 "import_path", "reindex", "checkpoint", "watch", "status", "close"):
        assert hasattr(ta, name), f"thread_archive.{name} missing"


def test_structured_read(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    hits = ta.search("hello")
    doc = ta.read_thread_structured(hits[0]["thread_id"])
    assert doc["title"] and doc["messages"]
    roles = [m["role"] for m in doc["messages"]]
    assert roles == ["user", "assistant"]
    # typed render blocks, not a flattened string
    user_text = doc["messages"][0]["blocks"][0]
    assert user_text["type"] == "text" and "hello library" in user_text["text"]


def test_reindex_via_api(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)
    ta.checkpoint()
    before = {h["event_id"] for h in ta.search("hello")}

    ta.close()
    for suffix in ("", "-wal", "-shm"):
        (archive_home / f"index.db{suffix}").unlink(missing_ok=True)

    counts = ta.reindex()
    assert counts["events"] > 0 and counts["fts"] > 0
    assert {h["event_id"] for h in ta.search("hello")} == before
