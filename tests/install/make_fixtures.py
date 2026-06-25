"""Generate a synthetic-but-realistic provider corpus for the install test.

Writes one or more sessions for every supported provider, in each importer's real
on-disk shape (claude-code / codex / grok / antigravity JSONL, cursor / opencode
SQLite). Each session embeds a distinctive search marker so the end-to-end check can
assert the data made it all the way through import → reindex → search.

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
        lines = [
            {"type": "user", "uuid": f"u{n}a", "timestamp": "2026-02-01T10:00:00Z",
             "sessionId": sid, "cwd": "/proj",
             "message": {"role": "user", "content": f"refactor the auth module {marker} please"}},
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


def generate(out_dir) -> dict:
    """Write the full synthetic corpus under ``out_dir``; return an import manifest."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    imports: list[dict] = []
    for builder in (_claude_code, _codex, _grok, _antigravity, _cursor, _opencode):
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
