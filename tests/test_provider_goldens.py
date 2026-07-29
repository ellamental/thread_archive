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
still needs real captures — collected by ``thread-archive source fix`` into the
scaffold's ``samples/`` under the archive home, never into this repo.

The comparison machinery is the PUBLIC plugin harness
(:mod:`thread_archive.provider.testing`) — the same ``assert_golden`` /
``write_jsonl`` an external provider plugin runs against its own importer. These
tests are its dogfood: every internal golden exercises the shipped harness, so
a harness regression fails here before any plugin author hits it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from thread_archive._importers import (
    import_antigravity_session_incremental,
    import_claude_science_db,
    import_codex_session_incremental,
    import_cowork_session_incremental,
    import_cursor_db,
    import_grok_session_incremental,
    import_opencode_db,
    import_session_incremental,
)
from thread_archive._importers.exports import (
    import_chatgpt_export,
    import_claude_ai_export,
)
from thread_archive.provider.testing import assert_golden, init_archive, write_jsonl

GOLDEN_DIR = Path(__file__).parent / "goldens" / "providers"


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


# Cowork audit.jsonl: CC-shaped lines under `_audit_timestamp`, with the two
# cowork-specific shapes — the `isReplay` SDK echo (must not become a second
# user turn) and the sidecar metadata title — plus a tool round-trip so the
# whole CC-path normalization is locked for this source too.
COWORK = [
    {"type": "user", "uuid": "cw-u1", "_audit_timestamp": "2026-01-01T10:00:00Z",
     "sessionId": "cw-s1", "cwd": "/proj",
     "message": {"role": "user", "content": "hello cowork golden"}},
    {"type": "user", "uuid": "cw-u1", "isReplay": True,
     "timestamp": "2026-01-01T10:00:01Z",
     "message": {"role": "user", "content": "hello cowork golden"}},
    {"type": "assistant", "uuid": "cw-a1", "parentUuid": "cw-u1",
     "_audit_timestamp": "2026-01-01T10:00:05Z",
     "message": {"role": "assistant", "model": "claude-opus-4", "content": [
         {"type": "text", "text": "checking the file"},
         {"type": "tool_use", "id": "cw-tu1", "name": "Read",
          "input": {"file_path": "/x"}}]}},
    {"type": "user", "uuid": "cw-u2", "parentUuid": "cw-a1",
     "_audit_timestamp": "2026-01-01T10:00:06Z",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "cw-tu1", "content": "contents of x"}]}},
    {"type": "assistant", "uuid": "cw-a2", "parentUuid": "cw-u2",
     "_audit_timestamp": "2026-01-01T10:00:08Z",
     "message": {"role": "assistant", "model": "claude-opus-4",
                 "content": [{"type": "text", "text": "done — hi from cowork"}]}},
]

COWORK_META = {"title": "Golden Cowork Task"}


def _make_claude_science_db(path: Path) -> None:
    """An operon-cli.db with the store's interesting shapes: a root conversation
    whose transcript carries the full Anthropic block set (text / thinking /
    tool_use / tool_result — messages are already Claude-shaped), and a child
    frame (a spawned subagent) that must import as a hidden 🤖 system thread."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE frames ("
        "id TEXT PRIMARY KEY, parent_frame_id TEXT, root_frame_id TEXT, agent_name TEXT,"
        "status TEXT, conversation_type TEXT, name TEXT, task_summary TEXT, model TEXT,"
        "project_id TEXT, created_at INTEGER, updated_at INTEGER)")
    conn.execute(
        "CREATE TABLE frame_messages ("
        "frame_id TEXT, idx INTEGER, msg_json TEXT, PRIMARY KEY(frame_id, idx))")
    conn.execute(
        "INSERT INTO frames (id, agent_name, status, conversation_type, name, model, "
        "project_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("root-gold", "OPERON", "completed", "agent", "Golden Science Chat",
         "claude-opus-4-8", "proj_real", 1767261600000))
    root_messages = [
        {"role": "user", "_uuid": "sci-u1",
         "content": [{"type": "text", "text": "hello science golden"}]},
        {"role": "assistant", "_uuid": "sci-a1", "content": [
            {"type": "thinking", "thinking": "considering the assay"},
            {"type": "text", "text": "running the query"},
            {"type": "tool_use", "id": "sci-tu1", "name": "search_literature",
             "input": {"query": "golden assay"}}]},
        {"role": "user", "_uuid": "sci-u2", "content": [
            {"type": "tool_result", "tool_use_id": "sci-tu1", "content": "one paper found"}]},
        {"role": "assistant", "_uuid": "sci-a2",
         "content": [{"type": "text", "text": "done — hi from science"}]},
    ]
    conn.executemany(
        "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?,?,?)",
        [("root-gold", i, json.dumps(m)) for i, m in enumerate(root_messages)])
    conn.execute(
        "INSERT INTO frames (id, parent_frame_id, root_frame_id, agent_name, status, "
        "conversation_type, task_summary, model, project_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("child-gold", "root-gold", "root-gold", "REVIEWER", "completed", "agent",
         "Review the assay", "claude-opus-4-8", "proj_real", 1767261700000))
    child_messages = [
        {"role": "user", "_uuid": "sci-cu1",
         "content": [{"type": "text", "text": "review this assay"}]},
        {"role": "assistant", "_uuid": "sci-ca1",
         "content": [{"type": "text", "text": "assay looks sound"}]},
    ]
    conn.executemany(
        "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?,?,?)",
        [("child-gold", i, json.dumps(m)) for i, m in enumerate(child_messages)])
    conn.commit()
    conn.close()


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


# ── one test per provider ────────────────────────────────────────────────────

def test_claude_code_golden(archive_home) -> None:
    init_archive()
    f = archive_home / "sess.jsonl"
    # torn tail: a crash mid-write must not derail the import of complete lines
    write_jsonl(f, CLAUDE_CODE, torn_tail='{"type": "assistant", "uuid": "a3", "mess')
    import_session_incremental(f, "proj:s1")
    assert_golden("claude-code", archive_home, GOLDEN_DIR)


def test_codex_golden(archive_home) -> None:
    init_archive()
    f = archive_home / "codex.jsonl"
    write_jsonl(f, CODEX)
    import_codex_session_incremental(f, "codex-sess")
    assert_golden("codex", archive_home, GOLDEN_DIR)


def test_grok_golden(archive_home) -> None:
    init_archive()
    session_dir = archive_home / "grok-sess"
    session_dir.mkdir()
    write_jsonl(session_dir / "chat_history.jsonl", GROK)
    (session_dir / "summary.json").write_text(json.dumps(GROK_SUMMARY), encoding="utf-8")
    import_grok_session_incremental(session_dir / "chat_history.jsonl", "grok-sess")
    assert_golden("grok", archive_home, GOLDEN_DIR)


def test_antigravity_golden(archive_home) -> None:
    init_archive()
    f = archive_home / "transcript.jsonl"
    write_jsonl(f, ANTIGRAVITY)
    import_antigravity_session_incremental(f, "ag-conv")
    assert_golden("antigravity", archive_home, GOLDEN_DIR)


def test_cursor_golden(archive_home) -> None:
    init_archive()
    db = archive_home / "state.vscdb"
    _make_cursor_db(db)
    import_cursor_db(db)
    assert_golden("cursor", archive_home, GOLDEN_DIR)


def test_opencode_golden(archive_home) -> None:
    init_archive()
    db = archive_home / "opencode.db"
    _make_opencode_db(db)
    import_opencode_db(db)
    assert_golden("opencode", archive_home, GOLDEN_DIR)


def test_claude_science_golden(archive_home) -> None:
    init_archive()
    db = archive_home / "operon-cli.db"
    _make_claude_science_db(db)
    import_claude_science_db(db, "org-gold")
    assert_golden("claude-science", archive_home, GOLDEN_DIR)


def test_cowork_golden(archive_home) -> None:
    init_archive()
    session_dir = archive_home / "cw_sess"
    session_dir.mkdir()
    audit = session_dir / "audit.jsonl"
    write_jsonl(audit, COWORK)
    meta = archive_home / "cw_sess.json"
    meta.write_text(json.dumps(COWORK_META), encoding="utf-8")
    import_cowork_session_incremental(audit, "user:org:cw-sess", meta)
    assert_golden("cowork", archive_home, GOLDEN_DIR)


def test_chatgpt_web_export_golden(archive_home) -> None:
    init_archive()
    export_dir = archive_home / "chatgpt-export"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text(json.dumps(CHATGPT_EXPORT), encoding="utf-8")
    (export_dir / "user.json").write_text(json.dumps({"id": "u1"}), encoding="utf-8")
    import_chatgpt_export(export_dir)
    assert_golden("chatgpt-web", archive_home, GOLDEN_DIR)


def test_claude_web_export_golden(archive_home) -> None:
    init_archive()
    export_dir = archive_home / "claude-web-export"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text(json.dumps(CLAUDE_WEB), encoding="utf-8")
    (export_dir / "users.json").write_text(json.dumps([{"uuid": "user-1"}]), encoding="utf-8")
    import_claude_ai_export(export_dir)
    assert_golden("claude-web", archive_home, GOLDEN_DIR)
