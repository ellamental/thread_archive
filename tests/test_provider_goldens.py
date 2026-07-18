"""Golden normalized output per provider: each committed fixture transcript is
imported into a throwaway home and the truth JSONL it produces — the archive's
canonical normalized form — is compared field-for-field against a reviewed
golden file in ``tests/goldens/providers/``.

The point-assertions in the importer tests prove specific behaviors; a golden
locks the *entire* normalized surface, so any change to what an importer emits
(a renamed payload key, a dropped block, a shifted timestamp, a different
dedup_key recipe) shows up as a reviewable diff instead of passing silently.
Fixtures carry each provider's interesting shapes: tool calls + results,
thinking/reasoning, unmodeled block types, unknown fields, and (claude-code) a
torn tail line.

When an importer change is deliberate, regenerate and review the diff:

    UPDATE_GOLDENS=1 .venv/bin/pytest tests/test_provider_goldens.py

Volatile fields are normalized before comparison: per-run uuids (stream_id /
api_call_id) become ordinals, wall-clock fields (id, recorded_at) are dropped,
and any fixture tmp-path leaking into payloads is scrubbed to a placeholder.
These goldens lock OUR normalization; drift in the providers' upstream formats
still needs real captures (tests/install/obfuscate_fixtures.py).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from thread_archive._config import resolve_paths
from thread_archive._importers import (
    import_antigravity_session_incremental,
    import_codex_session_incremental,
    import_cursor_db,
    import_grok_session_incremental,
    import_opencode_db,
    import_session_incremental,
)
from thread_archive._importers.exports import (
    import_chatgpt_export,
    import_claude_ai_export,
)
from thread_archive._store import init_db

GOLDEN_DIR = Path(__file__).parent / "goldens" / "providers"


def _write_jsonl(path: Path, lines: list, torn_tail: str | None = None) -> None:
    text = "\n".join(json.dumps(ln) for ln in lines) + "\n"
    if torn_tail is not None:
        text += torn_tail  # no trailing newline: a write cut off mid-line
    path.write_text(text, encoding="utf-8")


# ── fixtures: each provider's interesting shapes ─────────────────────────────

CLAUDE_CODE = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
     "cwd": "/proj", "unknown_future_field": {"v": 2},
     "message": {"role": "user", "content": "hello golden"}},
    {"type": "user", "uuid": "u_ide", "timestamp": "2026-01-01T10:00:01Z", "sessionId": "s1",
     "message": {"role": "user", "content": "<ide_selection>def foo(): pass</ide_selection>"}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-01-01T10:00:05Z",
     "sessionId": "s1", "message": {"role": "assistant", "model": "claude-opus-4", "content": [
         {"type": "thinking", "thinking": "let me think"},
         {"type": "text", "text": "running it now"},
         {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}},
         {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"query": "q"}}]}},
    {"type": "user", "uuid": "u2", "parentUuid": "a1", "timestamp": "2026-01-01T10:00:06Z",
     "sessionId": "s1", "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "tu1", "content": "file.txt"}]}},
    {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "timestamp": "2026-01-01T10:00:08Z",
     "sessionId": "s1", "message": {"role": "assistant", "model": "claude-opus-4",
                                    "content": [{"type": "text", "text": "one file"}]}},
]

CODEX = [
    {"type": "session_meta", "payload": {"id": "sess", "cwd": "/proj", "model": "gpt-5"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"type": "user_message", "message": "hello codex golden", "turn_id": "t1"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:05Z",
     "payload": {"type": "agent_message", "message": "hi from codex"}},
]

GROK = [
    {"type": "user", "content": [{"type": "text", "text": "<user_query>hello grok golden</user_query>"}]},
    {"type": "reasoning", "summary": "thinking about the ask"},
    {"type": "assistant", "content": "checking a file", "tool_calls": [
        {"id": "call1", "function": {"name": "read_file"}, "arguments": json.dumps({"path": "/x"})}]},
    {"type": "tool_result", "tool_call_id": "call1", "content": "contents of x"},
    {"type": "assistant", "content": "done — hi from grok", "tool_calls": []},
]

# without a summary.json created_at, the grok importer bases timestamps on wall
# clock — a golden must be deterministic, so the fixture pins it
GROK_SUMMARY = {
    "info": {"id": "grok-sess", "cwd": "/proj"},
    "current_model_id": "grok-4",
    "created_at": "2026-01-01T10:00:00Z",
}

ANTIGRAVITY = [
    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "done",
     "created_at": "2026-01-01T10:00:00Z", "content": "<USER_REQUEST>fix the golden bug</USER_REQUEST>"},
    {"step_index": 1, "source": "MODEL", "type": "TOOL_CALL", "status": "done",
     "created_at": "2026-01-01T10:00:02Z", "content": "grep -r golden"},
    {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "done",
     "created_at": "2026-01-01T10:00:05Z", "content": "On it — fixed the golden bug."},
]

# ChatGPT account-export conversation with the two shapes prior imports lost: a
# branch (a regenerated assistant reply off the active path — n1 has two children,
# `current_node` reaches only a1) and an image-only user turn carrying an
# `image_asset_pointer` (no text — the whole turn used to vanish on import). Locks:
# every node's `branch.parent_id`, `branch.active_path: false` on the off-path reply,
# and the preserved asset_pointer on the image turn's user_message_sent.
CHATGPT_EXPORT = [{
    "id": "gpt-conv-gold", "title": "Golden GPT Chat",
    "create_time": 1767261600.0, "update_time": 1767261660.0,
    "current_node": "a2",
    "mapping": {
        "root": {"id": "root", "parent": None, "children": ["n1"], "message": None},
        "n1": {"id": "n1", "parent": "root", "children": ["a1", "a1b"], "message": {
            "id": "n1", "author": {"role": "user"}, "create_time": 1767261600.0,
            "content": {"content_type": "text", "parts": ["what's 2+2?"]},
            "status": "finished_successfully", "metadata": {}}},
        "a1": {"id": "a1", "parent": "n1", "children": ["nimg"], "message": {
            "id": "a1", "author": {"role": "assistant"}, "create_time": 1767261605.0,
            "content": {"content_type": "text", "parts": ["4"]},
            "status": "finished_successfully", "metadata": {"model_slug": "gpt-4o"}}},
        "a1b": {"id": "a1b", "parent": "n1", "children": [], "message": {
            "id": "a1b", "author": {"role": "assistant"}, "create_time": 1767261607.0,
            "content": {"content_type": "text", "parts": ["It's four."]},
            "status": "finished_successfully", "metadata": {"model_slug": "gpt-4o"}}},
        "nimg": {"id": "nimg", "parent": "a1", "children": ["a2"], "message": {
            "id": "nimg", "author": {"role": "user"}, "create_time": 1767261610.0,
            "content": {"content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer",
                 "asset_pointer": "file-service://file-GOLD",
                 "size_bytes": 12345, "width": 640, "height": 480}]},
            "status": "finished_successfully", "metadata": {}}},
        "a2": {"id": "a2", "parent": "nimg", "children": [], "message": {
            "id": "a2", "author": {"role": "assistant"}, "create_time": 1767261615.0,
            "content": {"content_type": "text", "parts": ["Nice image!"]},
            "status": "finished_successfully", "metadata": {"model_slug": "gpt-4o"}}},
    },
}]

# claude.ai account-export conversation exercising the two upload shapes prior imports
# dropped: an `attachments` entry (a document whose text is in `extracted_content`) and
# a `files` entry (an image referenced by name/uuid, bytes outside the JSON). Locks that
# both survive as their own content_block events in truth.
CLAUDE_WEB = [{
    "uuid": "claude-web-gold", "name": "Golden Claude Web Chat",
    "created_at": "2026-01-01T10:00:00Z", "updated_at": "2026-01-01T10:01:00Z",
    "chat_messages": [
        {"uuid": "m1", "sender": "human", "text": "here are my notes",
         "content": [{"type": "text", "text": "here are my notes"}],
         "created_at": "2026-01-01T10:00:00Z",
         "attachments": [{"file_name": "notes.txt", "file_type": "text/plain",
                          "file_size": 30, "extracted_content": "the extracted text of my notes"}],
         "files": []},
        {"uuid": "m2", "sender": "assistant", "text": "thanks, I read your notes",
         "content": [{"type": "text", "text": "thanks, I read your notes"}],
         "created_at": "2026-01-01T10:00:10Z"},
        {"uuid": "m3", "sender": "human", "text": "and a picture",
         "content": [{"type": "text", "text": "and a picture"}],
         "created_at": "2026-01-01T10:00:20Z",
         "attachments": [],
         "files": [{"file_name": "diagram.png", "file_kind": "image", "file_uuid": "file-DIAGRAM"}]},
        {"uuid": "m4", "sender": "assistant", "text": "nice diagram",
         "content": [{"type": "text", "text": "nice diagram"}],
         "created_at": "2026-01-01T10:00:30Z"},
    ],
}]


def _make_cursor_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    cid = "compgold"
    composer = {
        "name": "Golden Cursor Chat",
        "lastUpdatedAt": 1700000000000,
        "fullConversationHeadersOnly": [
            {"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}, {"bubbleId": "b3", "type": 2}],
    }
    tool_bubble = {
        "type": 2, "text": "reading it", "createdAt": 1700000001000,
        "toolFormerData": {
            "name": "read_file", "tool": 40, "toolCallId": "toolu_g1", "status": "completed",
            "rawArgs": json.dumps({"target_file": "/x"}),
            "params": {"targetFile": "/x"},
            "result": {"contents": "golden contents"},
        },
    }
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "hello cursor golden",
                                           "createdAt": 1700000000000})),
        (f"bubbleId:{cid}:b2", json.dumps(tool_bubble)),
        (f"bubbleId:{cid}:b3", json.dumps({"type": 2, "text": "hi from cursor"})),
    ])
    conn.commit()
    conn.close()


def _make_opencode_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT, title TEXT, "
        "directory TEXT, time_created INTEGER, time_updated INTEGER, agent TEXT)")
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    sid = "sgold"
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                 (sid, "proj", None, "Golden OpenCode Session", "/proj",
                  1700000000000, 1700000005000, "build"))
    conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                 ("m1", sid, 1700000000000,
                  json.dumps({"role": "user", "time": {"created": 1700000000000}})))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                 ("p1", "m1", sid, 1700000000000,
                  json.dumps({"type": "text", "text": "hello opencode golden",
                              "time": {"start": 1700000000000}})))
    conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                 ("m2", sid, 1700000001000,
                  json.dumps({"role": "assistant", "modelID": "gpt", "providerID": "oai",
                              "time": {"created": 1700000001000, "completed": 1700000002000}})))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                 ("p2", "m2", sid, 1700000001000,
                  json.dumps({"type": "text", "text": "hi from opencode",
                              "time": {"start": 1700000001000}})))
    conn.commit()
    conn.close()


# ── normalization ────────────────────────────────────────────────────────────

THREAD_KEYS = ("name", "title", "source", "source_id", "source_metadata", "description")


def _scrub(value, tmp: str):
    if isinstance(value, str):
        return value.replace(tmp, "<fixtures>")
    if isinstance(value, list):
        return [_scrub(v, tmp) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, tmp) for k, v in value.items()}
    return value


def _normalized_truth(tmp: str) -> list[dict]:
    """Every truth record in thread-file order, volatile fields normalized."""
    threads_dir = resolve_paths(None).truth_dir / "threads"
    streams: dict[str, str] = {}
    calls: dict[str, str] = {}
    out: list[dict] = []
    for f in sorted(threads_dir.rglob("*.jsonl"), key=lambda p: int(p.stem)):
        for raw in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(raw)
            if rec["type"] == "thread":
                row = {"type": "thread", **{k: rec.get(k) for k in THREAD_KEYS}}
            elif rec["type"] == "event":
                sid, cid = rec.get("stream_id"), rec.get("api_call_id")
                row = {
                    "type": "event",
                    "event_type": rec["event_type"],
                    "occurred_at": rec.get("occurred_at"),
                    "stream": streams.setdefault(sid, f"s{len(streams)}") if sid else None,
                    "api_call": calls.setdefault(cid, f"c{len(calls)}") if cid else None,
                    "dedup_key": rec.get("dedup_key"),
                    "payload": rec.get("payload"),
                }
            else:
                row = rec
            out.append(_scrub(row, tmp))
    return out


def _check_golden(name: str, tmp: str) -> None:
    got = _normalized_truth(tmp)
    golden_file = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("UPDATE_GOLDENS"):
        golden_file.parent.mkdir(parents=True, exist_ok=True)
        golden_file.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"golden regenerated: {golden_file.name} — review the diff")
    assert golden_file.exists(), (
        f"no golden for {name}; generate with UPDATE_GOLDENS=1 and review it")
    want = json.loads(golden_file.read_text(encoding="utf-8"))
    assert got == want, (
        f"{name}: normalized output diverged from the reviewed golden. If the importer "
        f"change is deliberate: UPDATE_GOLDENS=1 pytest {__file__} and review the diff.")


# ── one test per provider ────────────────────────────────────────────────────

def test_claude_code_golden(archive_home) -> None:
    init_db()
    f = archive_home / "sess.jsonl"
    # torn tail: a crash mid-write must not derail the import of complete lines
    _write_jsonl(f, CLAUDE_CODE, torn_tail='{"type": "assistant", "uuid": "a3", "mess')
    import_session_incremental(f, "proj:s1")
    _check_golden("claude-code", str(archive_home))


def test_codex_golden(archive_home) -> None:
    init_db()
    f = archive_home / "codex.jsonl"
    _write_jsonl(f, CODEX)
    import_codex_session_incremental(f, "codex-sess")
    _check_golden("codex", str(archive_home))


def test_grok_golden(archive_home) -> None:
    init_db()
    session_dir = archive_home / "grok-sess"
    session_dir.mkdir()
    _write_jsonl(session_dir / "chat_history.jsonl", GROK)
    (session_dir / "summary.json").write_text(json.dumps(GROK_SUMMARY), encoding="utf-8")
    import_grok_session_incremental(session_dir / "chat_history.jsonl", "grok-sess")
    _check_golden("grok", str(archive_home))


def test_antigravity_golden(archive_home) -> None:
    init_db()
    f = archive_home / "transcript.jsonl"
    _write_jsonl(f, ANTIGRAVITY)
    import_antigravity_session_incremental(f, "ag-conv")
    _check_golden("antigravity", str(archive_home))


def test_cursor_golden(archive_home) -> None:
    init_db()
    db = archive_home / "state.vscdb"
    _make_cursor_db(db)
    import_cursor_db(db)
    _check_golden("cursor", str(archive_home))


def test_opencode_golden(archive_home) -> None:
    init_db()
    db = archive_home / "opencode.db"
    _make_opencode_db(db)
    import_opencode_db(db)
    _check_golden("opencode", str(archive_home))


def test_chatgpt_web_export_golden(archive_home) -> None:
    init_db()
    export_dir = archive_home / "chatgpt-export"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text(json.dumps(CHATGPT_EXPORT), encoding="utf-8")
    (export_dir / "user.json").write_text(json.dumps({"id": "u1"}), encoding="utf-8")
    import_chatgpt_export(export_dir)
    _check_golden("chatgpt-web", str(archive_home))


def test_claude_web_export_golden(archive_home) -> None:
    init_db()
    export_dir = archive_home / "claude-web-export"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text(json.dumps(CLAUDE_WEB), encoding="utf-8")
    (export_dir / "users.json").write_text(json.dumps([{"uuid": "user-1"}]), encoding="utf-8")
    import_claude_ai_export(export_dir)
    _check_golden("claude-web", str(archive_home))
