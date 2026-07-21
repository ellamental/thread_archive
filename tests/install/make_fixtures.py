"""Generate a synthetic-but-realistic provider corpus for the install test.

Writes one or more sessions for every supported provider, in each importer's real
on-disk shape: claude-code / codex / grok / antigravity JSONL, cursor / opencode
SQLite, chatgpt / claude.ai account-export directories, a claude-science
``operon-cli.db``, and a cowork ``audit.jsonl`` + metadata sidecar. Each session
embeds a distinctive search marker so the end-to-end check can assert the data made
it all the way through import → reindex → search. (The ``cloth`` provider is
plugin-registered on the operator's box, not part of the packaged distribution, so
the install corpus deliberately has no session for it.)

This corpus is **synthetic and safe to commit**. To exercise the install against your
*actual* conversations, run ``obfuscate_fixtures.py`` to produce an obfuscated corpus
from your local stores (gitignored) and point the install test at it instead.

Usage: ``python make_fixtures.py <out_dir>`` — returns/writes a manifest the e2e check
consumes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Distinctive single tokens (survive FTS tokenization) — one per provider, so the e2e
# search assertions are deterministic.
MARKERS = {
    "claude-code": "quasarclaudealpha",
    "codex": "nebulacodexbravo",
    "grok": "pulsargrokcharlie",
    "antigravity": "zenithantigravitydelta",
    "cursor": "cometcursorecho",
    "opencode": "auroraopencodefox",
    "chatgpt": "solsticechatgptgolf",
    "claude": "meridianclaudehotel",
    "claude-science": "vortexscienceindia",
    "cowork": "harborcoworkjuliet",
}


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _claude_code(out: Path) -> list[dict]:
    """Two claude-code sessions, each a few user/assistant turns."""
    imports = []
    for n in (1, 2):
        sid = f"sess{n}"
        marker = MARKERS["claude-code"]
        # Distinct first messages: an identical replayed prefix is exactly the
        # old-style compaction shape, and continuation detection would (rightly)
        # merge the two sessions into one thread.
        lines = [
            {"type": "user", "uuid": f"u{n}a", "timestamp": "2026-02-01T10:00:00Z",
             "sessionId": sid, "cwd": "/proj",
             "message": {"role": "user",
                         "content": f"refactor the auth module {marker} please, part {n}"}},
            {"type": "assistant", "uuid": f"a{n}a", "timestamp": "2026-02-01T10:00:05Z",
             "sessionId": sid, "message": {"role": "assistant", "model": "claude-opus-4-8",
             "content": [{"type": "text", "text": "Done — split the session handling out."}]}},
            {"type": "user", "uuid": f"u{n}b", "timestamp": "2026-02-01T10:01:00Z",
             "sessionId": sid, "cwd": "/proj",
             "message": {"role": "user", "content": "now add tests for the token expiry path"}},
            {"type": "assistant", "uuid": f"a{n}b", "timestamp": "2026-02-01T10:01:08Z",
             "sessionId": sid, "message": {"role": "assistant", "model": "claude-opus-4-8",
             "content": [{"type": "text", "text": "Added five cases covering refresh and expiry."}]}},
        ]
        p = out / "claude-code" / f"proj-{sid}.jsonl"
        _write_jsonl(p, lines)
        imports.append({"provider": "claude-code", "path": str(p), "source_id": f"proj:{sid}"})
    return imports


def _codex(out: Path) -> list[dict]:
    marker = MARKERS["codex"]
    lines = [
        {"type": "session_meta", "payload": {"id": "cdx1", "cwd": "/proj", "model": "gpt-5"}},
        {"type": "event_msg", "timestamp": "2026-02-02T09:00:00Z",
         "payload": {"type": "user_message", "message": f"profile the slow query {marker}", "turn_id": "t1"}},
        {"type": "event_msg", "timestamp": "2026-02-02T09:00:06Z",
         "payload": {"type": "agent_message", "message": "It's a missing index on events.thread_id."}},
    ]
    p = out / "codex" / "cdx1.jsonl"
    _write_jsonl(p, lines)
    return [{"provider": "codex", "path": str(p), "source_id": "codex-cdx1"}]


def _grok(out: Path) -> list[dict]:
    marker = MARKERS["grok"]
    lines = [
        {"type": "user", "content": [{"type": "text", "text": f"<user_query>explain RRF fusion {marker}</user_query>"}]},
        {"type": "assistant", "content": "Reciprocal rank fusion blends ranked lists by 1/(k+rank).",
         "tool_calls": []},
    ]
    p = out / "grok" / "grok-sess" / "chat_history.jsonl"
    _write_jsonl(p, lines)
    return [{"provider": "grok", "path": str(p), "source_id": "grok-sess"}]


def _antigravity(out: Path) -> list[dict]:
    marker = MARKERS["antigravity"]
    lines = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "done",
         "created_at": "2026-02-03T11:00:00Z",
         "content": f"<USER_REQUEST>wire up the {marker} dashboard</USER_REQUEST>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "done",
         "created_at": "2026-02-03T11:00:04Z", "content": "Planning the dashboard panels now."},
    ]
    p = out / "antigravity" / "conv.jsonl"
    _write_jsonl(p, lines)
    return [{"provider": "antigravity", "path": str(p), "source_id": "ag-conv"}]


def _cursor(out: Path) -> list[dict]:
    marker = MARKERS["cursor"]
    p = out / "cursor" / "state.vscdb"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    cid = "comp1"
    composer = {"name": "Cursor refactor chat", "lastUpdatedAt": 1700000000000,
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}]}
    rows = [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": f"extract the parser {marker}", "createdAt": 1700000000000})),
        (f"bubbleId:{cid}:b2", json.dumps({"type": 2, "text": "Pulled it into its own module."})),
    ]
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return [{"provider": "cursor", "path": str(p), "source_id": None}]


def _opencode(out: Path) -> list[dict]:
    marker = MARKERS["opencode"]
    p = out / "opencode" / "opencode.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT, title TEXT, "
                 "directory TEXT, time_created INTEGER, time_updated INTEGER, agent TEXT)")
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    sid = "oc1"
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                 (sid, "proj", None, "OpenCode build session", "/proj", 1700000000000, 1700000005000, "build"))
    conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                 ("m1", sid, 1700000000000, json.dumps({"role": "user", "time": {"created": 1700000000000}})))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                 ("p1", "m1", sid, 1700000000000,
                  json.dumps({"type": "text", "text": f"set up the {marker} pipeline", "time": {"start": 1700000000000}})))
    conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                 ("m2", sid, 1700000001000, json.dumps(
                     {"role": "assistant", "modelID": "gpt", "providerID": "oai",
                      "time": {"created": 1700000001000, "completed": 1700000002000}})))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                 ("p2", "m2", sid, 1700000001000,
                  json.dumps({"type": "text", "text": "Pipeline wired with three stages.", "time": {"start": 1700000001000}})))
    conn.commit()
    conn.close()
    return [{"provider": "opencode", "path": str(p), "source_id": None}]


def _chatgpt_export(out: Path) -> list[dict]:
    """A ChatGPT account-export directory: root ``conversations.json`` plus the
    ``user.json`` sibling that classifies the bundle without a parse."""
    marker = MARKERS["chatgpt"]
    d = out / "chatgpt-export"
    d.mkdir(parents=True, exist_ok=True)
    conv = [{
        "id": "gpt-conv-1", "title": "ChatGPT export chat",
        "create_time": 1767261600.0, "update_time": 1767261660.0,
        "current_node": "a1",
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["n1"], "message": None},
            "n1": {"id": "n1", "parent": "root", "children": ["a1"], "message": {
                "id": "n1", "author": {"role": "user"}, "create_time": 1767261600.0,
                "content": {"content_type": "text",
                            "parts": [f"summarize the {marker} findings"]},
                "status": "finished_successfully", "metadata": {}}},
            "a1": {"id": "a1", "parent": "n1", "children": [], "message": {
                "id": "a1", "author": {"role": "assistant"}, "create_time": 1767261605.0,
                "content": {"content_type": "text", "parts": ["Three findings, all benign."]},
                "status": "finished_successfully", "metadata": {"model_slug": "gpt-4o"}}},
        },
    }]
    (d / "conversations.json").write_text(json.dumps(conv), encoding="utf-8")
    (d / "user.json").write_text(json.dumps({"id": "user-1"}), encoding="utf-8")
    return [{"provider": "chatgpt", "kind": "export", "path": str(d)}]


def _claude_web_export(out: Path) -> list[dict]:
    """A claude.ai account-export directory: ``conversations.json`` plus the
    ``users.json`` sibling that classifies it as a claude bundle."""
    marker = MARKERS["claude"]
    d = out / "claude-export"
    d.mkdir(parents=True, exist_ok=True)
    conv = [{
        "uuid": "claude-web-1", "name": "claude.ai export chat",
        "created_at": "2026-02-04T10:00:00Z", "updated_at": "2026-02-04T10:01:00Z",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "text": f"compare the {marker} options",
             "content": [{"type": "text", "text": f"compare the {marker} options"}],
             "created_at": "2026-02-04T10:00:00Z"},
            {"uuid": "m2", "sender": "assistant", "text": "Option two is cheaper and simpler.",
             "content": [{"type": "text", "text": "Option two is cheaper and simpler."}],
             "created_at": "2026-02-04T10:00:10Z"},
        ],
    }]
    (d / "conversations.json").write_text(json.dumps(conv), encoding="utf-8")
    (d / "users.json").write_text(json.dumps([{"uuid": "user-1"}]), encoding="utf-8")
    return [{"provider": "claude", "kind": "export", "path": str(d)}]


def _claude_science(out: Path) -> list[dict]:
    """A per-org ``operon-cli.db`` with one root conversation frame."""
    marker = MARKERS["claude-science"]
    p = out / "claude-science" / "operon-cli.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(p)
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
        ("frame1", "OPERON", "completed", "agent", "Science install chat",
         "claude-opus-4-8", "proj1", 1767261600000))
    messages = [
        {"role": "user", "_uuid": "su1",
         "content": [{"type": "text", "text": f"design the {marker} assay"}]},
        {"role": "assistant", "_uuid": "sa1",
         "content": [{"type": "text", "text": "Assay designed with two controls."}]},
    ]
    conn.executemany(
        "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?,?,?)",
        [("frame1", i, json.dumps(m)) for i, m in enumerate(messages)])
    conn.commit()
    conn.close()
    return [{"provider": "claude-science", "kind": "science-db", "path": str(p),
             "org": "org-install"}]


def _cowork(out: Path) -> list[dict]:
    """A cowork session dir: CC-shaped ``audit.jsonl`` plus the metadata sidecar
    the watcher hands the importer for the title."""
    marker = MARKERS["cowork"]
    d = out / "cowork" / "sess1"
    d.mkdir(parents=True, exist_ok=True)
    audit = d / "audit.jsonl"
    _write_jsonl(audit, [
        {"type": "user", "uuid": "cwu1", "_audit_timestamp": "2026-02-05T09:00:00Z",
         "sessionId": "cw1", "cwd": "/proj",
         "message": {"role": "user", "content": f"plan the {marker} rollout"}},
        {"type": "assistant", "uuid": "cwa1", "_audit_timestamp": "2026-02-05T09:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "Rollout planned in three phases."}]}},
    ])
    meta = out / "cowork" / "sess1.json"
    meta.write_text(json.dumps({"title": "Cowork install session"}), encoding="utf-8")
    return [{"provider": "cowork", "kind": "cowork", "path": str(audit),
             "source_id": "user:org:cw-install", "metadata": str(meta)}]


def generate(out_dir) -> dict:
    """Write the full synthetic corpus under ``out_dir``; return an import manifest."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    imports: list[dict] = []
    for builder in (_claude_code, _codex, _grok, _antigravity, _cursor, _opencode,
                    _chatgpt_export, _claude_web_export, _claude_science, _cowork):
        imports.extend(builder(out))
    manifest = {"imports": imports, "markers": MARKERS}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "fixtures"
    m = generate(target)
    print(f"wrote {len(m['imports'])} session import(s) under {target}")
    for imp in m["imports"]:
        print(f"  {imp['provider']:12} {imp['path']}")
