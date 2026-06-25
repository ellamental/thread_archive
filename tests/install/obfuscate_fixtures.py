"""Build an *obfuscated* fixture corpus from your real local AI-tool stores.

The committed install corpus is synthetic. This opt-in tool lets you exercise the install
against the real shape and scale of *your* conversations without committing private
content: it reads your local provider stores, **scrubs every free-text field**
(deterministic per-word hashing, structural tags + discriminators preserved so the
importers still parse), and writes the result to a **gitignored** dir
(``tests/install/fixtures-real/``) with a manifest the e2e check consumes.

Privacy: obfuscation is one-way and lossy, but it is *not* a guarantee — never commit the
output, and review a sample before sharing. This writes only under the output dir; it
never modifies your real stores.

    python tests/install/obfuscate_fixtures.py            # discover defaults, 5 sessions each
    python tests/install/obfuscate_fixtures.py --limit 20 --claude-dir ~/.claude/projects
    python tests/install/e2e_check.py --fixtures tests/install/fixtures-real   # then test it

Run the result through the Docker install test by mounting it (see run_install_test.sh).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import string
import sys
from pathlib import Path

OUT_DEFAULT = Path(__file__).resolve().parent / "fixtures-real"
SALT = "thread-archive-obfuscation-v1"

# Keys whose string values are structural (discriminators, ids, timestamps, models) and
# must survive verbatim or the importers stop parsing.
PRESERVE_KEYS = {
    "type", "role", "model", "modelID", "providerID", "uuid", "parentUuid", "sessionId",
    "id", "session_id", "message_id", "composerId", "bubbleId", "turn_id", "status",
    "source", "timestamp", "created_at", "createdAt", "time", "time_created",
    "time_updated", "lastUpdatedAt", "start", "completed", "created", "step_index",
    "agent", "project_id", "parent_id",
}

_ALNUM = re.compile(r"[A-Za-z0-9]+")
# Protect angle-bracket markup (<user_query>, <USER_REQUEST>, …) — importers extract
# titles from these tags, so scramble only the text *between* tags.
_SEGMENT = re.compile(r"(<[^>]+>)")


def _pseudo(word: str) -> str:
    h = hashlib.sha1((SALT + word.lower()).encode()).hexdigest()
    alpha = string.ascii_lowercase
    out = "".join(alpha[int(h[i:i + 2], 16) % 26] for i in range(0, min(len(word) * 2, len(h)), 2))
    out = (out or "x")[: max(1, len(word))]
    if word.isupper():
        return out.upper()
    if word[:1].isupper():
        return out.capitalize()
    return out


def _scrub_text(s: str) -> str:
    parts = _SEGMENT.split(s)
    return "".join(p if p.startswith("<") and p.endswith(">") else _ALNUM.sub(lambda m: _pseudo(m.group(0)), p)
                   for p in parts)


def scrub(value, key: str | None = None):
    """Recursively obfuscate free-text string leaves, preserving structural keys/tags."""
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, str) and key not in PRESERVE_KEYS:
        return _scrub_text(value)
    return value


# ── per-provider readers (best-effort discovery; override the path flags) ──────
def _jsonl_sessions(src_glob: Path, pattern: str, limit: int):
    files = sorted(src_glob.glob(pattern))[:limit] if src_glob.exists() else []
    for f in files:
        try:
            lines = [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except (OSError, ValueError):
            continue
        if lines:
            yield f, lines


def _obfuscate_jsonl(provider, src, pattern, out, limit, marker) -> list[dict]:
    imports = []
    for i, (f, lines) in enumerate(_jsonl_sessions(src, pattern, limit)):
        scrubbed = [scrub(ln) for ln in lines]
        scrubbed = _inject_marker_jsonl(provider, scrubbed, marker if i == 0 else None)
        dest = out / provider / (f.name if provider != "grok" else f"sess{i}/chat_history.jsonl")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(json.dumps(x) for x in scrubbed) + "\n", encoding="utf-8")
        imports.append({"provider": provider, "path": str(dest),
                        "source_id": f"{provider}-{i}"})
    return imports


def _inject_marker_jsonl(provider, lines, marker):
    """Append a known marker token to the first user-text field so e2e can assert a hit."""
    if not marker:
        return lines
    for ln in lines:
        if provider == "claude-code" and ln.get("type") == "user":
            msg = ln.get("message", {})
            if isinstance(msg.get("content"), str):
                msg["content"] = f"{msg['content']} {marker}"
                return lines
        if provider == "codex" and ln.get("payload", {}).get("type") == "user_message":
            ln["payload"]["message"] = f"{ln['payload'].get('message','')} {marker}"
            return lines
        if provider == "grok" and ln.get("type") == "user":
            c = ln.get("content")
            if isinstance(c, list) and c and isinstance(c[0], dict):
                c[0]["text"] = f"{c[0].get('text','')} {marker}"
                return lines
    # fall back: tag the first line's stringifiable content
    return lines


def _obfuscate_opencode(db_path: Path, out: Path, limit: int, marker: str) -> list[dict]:
    if not db_path.exists():
        return []
    dest = out / "opencode" / "opencode.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(db_path)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(dest)
    dst.execute("CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT, title TEXT, "
                "directory TEXT, time_created INTEGER, time_updated INTEGER, agent TEXT)")
    dst.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    dst.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    sids = [r["id"] for r in src.execute("SELECT id FROM session ORDER BY time_updated DESC LIMIT ?", (limit,))]
    injected = False
    for sid in sids:
        s = src.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
        dst.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                    (s["id"], s["project_id"], s["parent_id"], _scrub_text(s["title"] or ""),
                     "/proj", s["time_created"], s["time_updated"], s["agent"]))
        for m in src.execute("SELECT * FROM message WHERE session_id=?", (sid,)):
            dst.execute("INSERT INTO message VALUES (?,?,?,?)",
                        (m["id"], m["session_id"], m["time_created"],
                         json.dumps(scrub(json.loads(m["data"]))) if m["data"] else m["data"]))
        for p in src.execute("SELECT * FROM part WHERE session_id=?", (sid,)):
            data = scrub(json.loads(p["data"])) if p["data"] else None
            if data and not injected and data.get("type") == "text":
                data["text"] = f"{data.get('text','')} {marker}"
                injected = True
            dst.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                        (p["id"], p["message_id"], p["session_id"], p["time_created"],
                         json.dumps(data) if data is not None else p["data"]))
    dst.commit()
    src.close()
    dst.close()
    return [{"provider": "opencode", "path": str(dest), "source_id": None}]


def main(argv: list[str] | None = None) -> int:
    home = Path.home()
    p = argparse.ArgumentParser(description="Obfuscate real local stores into a test corpus.")
    p.add_argument("--out", default=str(OUT_DEFAULT))
    p.add_argument("--limit", type=int, default=5, help="max sessions per provider")
    p.add_argument("--claude-dir", default=str(home / ".claude" / "projects"))
    p.add_argument("--codex-dir", default=str(home / ".codex"))
    p.add_argument("--grok-dir", default=str(home / ".grok"))
    p.add_argument("--opencode-db", default=str(home / ".local" / "share" / "opencode" / "opencode.db"))
    args = p.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    markers = {"claude-code": "obfclaudemarker", "codex": "obfcodexmarker",
               "grok": "obfgrokmarker", "opencode": "obfopencodemarker"}

    imports: list[dict] = []
    imports += _obfuscate_jsonl("claude-code", Path(args.claude_dir), "**/*.jsonl", out, args.limit, markers["claude-code"])
    imports += _obfuscate_jsonl("codex", Path(args.codex_dir), "**/*.jsonl", out, args.limit, markers["codex"])
    imports += _obfuscate_jsonl("grok", Path(args.grok_dir), "**/chat_history.jsonl", out, args.limit, markers["grok"])
    imports += _obfuscate_opencode(Path(args.opencode_db), out, args.limit, markers["opencode"])

    # only keep markers for providers we actually produced sessions for
    have = {imp["provider"] for imp in imports}
    manifest = {"imports": imports, "markers": {k: v for k, v in markers.items() if k in have}}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if not imports:
        print("no real stores found — point --claude-dir / --codex-dir / etc. at your data", file=sys.stderr)
        return 1
    print(f"wrote {len(imports)} obfuscated session(s) under {out}")
    for imp in imports:
        print(f"  {imp['provider']:12} {imp['path']}")
    print("\nNext: python tests/install/e2e_check.py --fixtures", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
