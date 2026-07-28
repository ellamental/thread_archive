"""Synthetic-but-realistic provider corpus for the install tests, in two layouts.

The *content* is defined once — the per-provider payload builders below produce each
importer's real on-disk shape (claude-code / codex / grok / antigravity JSONL, cursor /
opencode SQLite, chatgpt / claude.ai account-export directories, a claude-science
``operon-cli.db``, a cowork ``audit.jsonl`` + metadata sidecar). Two layouts place it:

- :func:`generate` — a **flat** tree (``out/<provider>/…``) plus an import manifest, for
  the hand-fed importer-level check (``e2e_check.py``): it imports each entry by explicit
  path. Safe to commit.
- :func:`realistic_layout` — each payload written to the **real default store location** a
  fresh machine would have it in (``~/.claude/projects``, ``~/.codex/sessions``, the
  OS-correct app-data dir for Cursor/Cowork, …) under a fake ``$HOME``, for the
  discovery-driven first-run check (``first_run.py``): nothing is hand-fed, ``thread_archive
  watch`` discovers it exactly as it would a real user's machine.

Both layouts embed the same per-provider search marker, so a check can assert the data
made it all the way through import → reindex → search. (The ``cloth`` provider is
plugin-registered on the operator's box, not part of the packaged distribution, so the
corpus deliberately has no session for it.)

To exercise the install against your *actual* conversations, run ``obfuscate_fixtures.py``
to produce an obfuscated corpus from your local stores (gitignored) and point the install
test at it instead.

Usage: ``python make_fixtures.py <out_dir>`` writes the flat layout + a manifest the e2e
check consumes.
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
from pathlib import Path

# Distinctive single tokens (survive FTS tokenization) — one per provider, so the
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


# ── content builders (payload only; the layout decides where it lands) ────────


def _cc_lines(n: int) -> list[dict]:
    """One claude-code session's JSONL turns (session index ``n``)."""
    marker = MARKERS["claude-code"]
    sid = f"sess{n}"
    # Distinct first messages: an identical replayed prefix is exactly the
    # old-style compaction shape, and continuation detection would (rightly)
    # merge the two sessions into one thread.
    return [
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


def _codex_lines() -> list[dict]:
    marker = MARKERS["codex"]
    return [
        {"type": "session_meta", "payload": {"id": "cdx1", "cwd": "/proj", "model": "gpt-5"}},
        {"type": "event_msg", "timestamp": "2026-02-02T09:00:00Z",
         "payload": {"type": "user_message", "message": f"profile the slow query {marker}", "turn_id": "t1"}},
        {"type": "event_msg", "timestamp": "2026-02-02T09:00:06Z",
         "payload": {"type": "agent_message", "message": "It's a missing index on events.thread_id."}},
    ]


def _grok_lines() -> list[dict]:
    marker = MARKERS["grok"]
    return [
        {"type": "user", "content": [{"type": "text", "text": f"<user_query>explain RRF fusion {marker}</user_query>"}]},
        {"type": "assistant", "content": "Reciprocal rank fusion blends ranked lists by 1/(k+rank).",
         "tool_calls": []},
    ]


def _antigravity_lines() -> list[dict]:
    marker = MARKERS["antigravity"]
    return [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "done",
         "created_at": "2026-02-03T11:00:00Z",
         "content": f"<USER_REQUEST>wire up the {marker} dashboard</USER_REQUEST>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "done",
         "created_at": "2026-02-03T11:00:04Z", "content": "Planning the dashboard panels now."},
    ]


def _populate_cursor_db(path: Path) -> None:
    marker = MARKERS["cursor"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
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


def _populate_opencode_db(path: Path) -> None:
    marker = MARKERS["opencode"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
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


def _populate_science_db(path: Path) -> None:
    """A per-org ``operon-cli.db`` with one root conversation frame."""
    marker = MARKERS["claude-science"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
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


def _chatgpt_export(dir_: Path) -> None:
    """A ChatGPT account-export directory: root ``conversations.json`` plus the
    ``user.json`` sibling that classifies the bundle without a parse."""
    marker = MARKERS["chatgpt"]
    dir_.mkdir(parents=True, exist_ok=True)
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
    (dir_ / "conversations.json").write_text(json.dumps(conv), encoding="utf-8")
    (dir_ / "user.json").write_text(json.dumps({"id": "user-1"}), encoding="utf-8")


def _claude_web_export(dir_: Path) -> None:
    """A claude.ai account-export directory: ``conversations.json`` plus the
    ``users.json`` sibling that classifies it as a claude bundle."""
    marker = MARKERS["claude"]
    dir_.mkdir(parents=True, exist_ok=True)
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
    (dir_ / "conversations.json").write_text(json.dumps(conv), encoding="utf-8")
    (dir_ / "users.json").write_text(json.dumps([{"uuid": "user-1"}]), encoding="utf-8")


def _cowork_audit_lines() -> list[dict]:
    """A cowork session's CC-shaped ``audit.jsonl`` turns."""
    marker = MARKERS["cowork"]
    return [
        {"type": "user", "uuid": "cwu1", "_audit_timestamp": "2026-02-05T09:00:00Z",
         "sessionId": "cw1", "cwd": "/proj",
         "message": {"role": "user", "content": f"plan the {marker} rollout"}},
        {"type": "assistant", "uuid": "cwa1", "_audit_timestamp": "2026-02-05T09:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "Rollout planned in three phases."}]}},
    ]


# ── flat layout (out/<provider>/…) + import manifest ─────────────────────────


def _claude_code(out: Path) -> list[dict]:
    imports = []
    for n in (1, 2):
        p = out / "claude-code" / f"proj-sess{n}.jsonl"
        _write_jsonl(p, _cc_lines(n))
        imports.append({"provider": "claude-code", "path": str(p), "source_id": f"proj:sess{n}"})
    return imports


def _codex(out: Path) -> list[dict]:
    p = out / "codex" / "cdx1.jsonl"
    _write_jsonl(p, _codex_lines())
    return [{"provider": "codex", "path": str(p), "source_id": "codex-cdx1"}]


def _grok(out: Path) -> list[dict]:
    p = out / "grok" / "grok-sess" / "chat_history.jsonl"
    _write_jsonl(p, _grok_lines())
    return [{"provider": "grok", "path": str(p), "source_id": "grok-sess"}]


def _antigravity(out: Path) -> list[dict]:
    p = out / "antigravity" / "conv.jsonl"
    _write_jsonl(p, _antigravity_lines())
    return [{"provider": "antigravity", "path": str(p), "source_id": "ag-conv"}]


def _cursor(out: Path) -> list[dict]:
    p = out / "cursor" / "state.vscdb"
    _populate_cursor_db(p)
    return [{"provider": "cursor", "path": str(p), "source_id": None}]


def _opencode(out: Path) -> list[dict]:
    p = out / "opencode" / "opencode.db"
    _populate_opencode_db(p)
    return [{"provider": "opencode", "path": str(p), "source_id": None}]


def _chatgpt_flat(out: Path) -> list[dict]:
    d = out / "chatgpt-export"
    _chatgpt_export(d)
    return [{"provider": "chatgpt", "kind": "export", "path": str(d)}]


def _claude_flat(out: Path) -> list[dict]:
    d = out / "claude-export"
    _claude_web_export(d)
    return [{"provider": "claude", "kind": "export", "path": str(d)}]


def _claude_science(out: Path) -> list[dict]:
    p = out / "claude-science" / "operon-cli.db"
    _populate_science_db(p)
    return [{"provider": "claude-science", "kind": "science-db", "path": str(p),
             "org": "org-install"}]


def _cowork(out: Path) -> list[dict]:
    d = out / "cowork" / "sess1"
    audit = d / "audit.jsonl"
    _write_jsonl(audit, _cowork_audit_lines())
    meta = out / "cowork" / "sess1.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"title": "Cowork install session"}), encoding="utf-8")
    return [{"provider": "cowork", "kind": "cowork", "path": str(audit),
             "source_id": "user:org:cw-install", "metadata": str(meta)}]


def generate(out_dir) -> dict:
    """Write the full synthetic corpus in the **flat** layout under ``out_dir``;
    return an import manifest the hand-fed :mod:`e2e_check` consumes."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    imports: list[dict] = []
    for builder in (_claude_code, _codex, _grok, _antigravity, _cursor, _opencode,
                    _chatgpt_flat, _claude_flat, _claude_science, _cowork):
        imports.extend(builder(out))
    manifest = {"imports": imports, "markers": MARKERS}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


# ── realistic layout (each store in its real default location) ───────────────


def _app_data_base(home: Path) -> Path:
    """The per-OS app-data root a desktop/Electron harness lives under, rooted at a
    given (fake) ``home`` — the placement mirror of
    :func:`thread_archive._watcher.paths.app_data_dir`.

    Cursor and Cowork resolve their store from ``app_data_dir()``, which is
    OS-divergent (macOS ``~/Library/Application Support``, Linux ``$XDG_CONFIG_HOME``
    or ``~/.config``). The first-run check drives the CLI with a scrubbed env (``HOME``
    only, no absolute ``XDG_CONFIG_HOME``), so the watcher's ``app_data_dir()`` resolves
    to exactly what this returns — and the fixture lands where discovery looks.
    """
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Application Support"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else home / "AppData" / "Roaming"
    return home / ".config"  # Linux / other


def realistic_layout(home) -> dict:
    """Lay the corpus out under fake ``$HOME`` ``home``, each provider's store in the
    **real default location** ``thread_archive`` discovers it in — so ``thread_archive
    watch --once`` ingests it with no hand-fed paths.

    Web exports (claude.ai / ChatGPT) have no live store — they are account exports a
    user drops in by hand — so they are written to their own directories and returned
    under ``exports`` for the caller to feed to ``thread-archive source import-account``.

    Returns ``{markers, exports, min_threads}``.
    """
    home = Path(home)
    app = _app_data_base(home)

    # claude-code — ~/.claude/projects/<project>/<uuid>.jsonl
    for n in (1, 2):
        _write_jsonl(home / ".claude" / "projects" / "proj" / f"sess{n}.jsonl", _cc_lines(n))

    # codex — ~/.codex/sessions/**/*.jsonl (rglob)
    _write_jsonl(home / ".codex" / "sessions" / "2026" / "02" / "cdx1.jsonl", _codex_lines())

    # grok — ~/.grok/sessions/<uuid>/chat_history.jsonl (id = parent dir)
    _write_jsonl(home / ".grok" / "sessions" / "grok-sess" / "chat_history.jsonl", _grok_lines())

    # antigravity — ~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl
    _write_jsonl(
        home / ".gemini" / "antigravity-cli" / "brain" / "ag-conv"
        / ".system_generated" / "logs" / "transcript.jsonl",
        _antigravity_lines(),
    )

    # cursor — <app-data>/Cursor/User/globalStorage/state.vscdb
    _populate_cursor_db(app / "Cursor" / "User" / "globalStorage" / "state.vscdb")

    # opencode — ~/.local/share/opencode/opencode.db
    _populate_opencode_db(home / ".local" / "share" / "opencode" / "opencode.db")

    # claude-science — ~/.claude-science/orgs/<org>/operon-cli.db
    _populate_science_db(home / ".claude-science" / "orgs" / "org-install" / "operon-cli.db")

    # cowork — <app-data>/Claude/local-agent-mode-sessions/<user>/<org>/local_<id>/audit.jsonl
    cowork_org = app / "Claude" / "local-agent-mode-sessions" / "user1" / "org1"
    _write_jsonl(cowork_org / "local_cw1" / "audit.jsonl", _cowork_audit_lines())
    (cowork_org / "local_cw1.json").write_text(
        json.dumps({"title": "Cowork install session"}), encoding="utf-8")

    # web exports — dropped in by hand; fed to `thread-archive source import-account`
    exports_root = home / "downloads"
    chatgpt_dir = exports_root / "chatgpt-export"
    claude_dir = exports_root / "claude-export"
    _chatgpt_export(chatgpt_dir)
    _claude_web_export(claude_dir)

    # 2 claude-code + codex + grok + antigravity + cursor + opencode + science + cowork
    # + 2 exports = 11; a floor leaves importer evolution room.
    return {
        "markers": MARKERS,
        "exports": [str(chatgpt_dir), str(claude_dir)],
        "min_threads": 10,
    }


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "fixtures"
    m = generate(target)
    print(f"wrote {len(m['imports'])} session import(s) under {target}")
    for imp in m["imports"]:
        print(f"  {imp['provider']:12} {imp['path']}")
