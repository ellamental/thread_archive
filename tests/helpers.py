"""Shared factories for tests that drive the archive through a claude-code import.

The archive-wide integrity tests all start the same way: write a minimal one-turn
claude-code session to disk, import it, then damage/inspect the truth dir or the
index. These helpers are that shared setup. Session content is distinct per
``name``, or claude-code continuation detection merges two sessions imported into
the same home into one thread.
"""

from __future__ import annotations

import json

from sqlalchemy import text

import thread_archive as ta
from thread_archive.store import get_session


def cc_user(name: str = "sess", content: str | None = None) -> dict:
    return {
        "type": "user", "uuid": f"u-{name}", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj",
        "message": {"role": "user", "content": content or f"hello from session {name}"},
    }


def cc_assistant(name: str = "sess", text_content: str | None = None) -> dict:
    return {
        "type": "assistant", "uuid": f"a-{name}", "timestamp": "2026-01-01T10:00:05Z",
        "message": {"role": "assistant", "model": "claude-opus-4",
                    "content": [{"type": "text", "text": text_content or f"hi back to {name}"}]},
    }


def write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def append_jsonl(path, lines) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")


def import_cc_session(tmp_path, name: str = "sess"):
    """Write a one-turn claude-code session named ``name`` and import it."""
    f = tmp_path / f"{name}.jsonl"
    write_jsonl(f, [cc_user(name), cc_assistant(name)])
    return ta.import_path(f)


def one_thread_file(archive_home):
    """The (single) thread truth file of a one-thread archive."""
    return next((archive_home / "truth" / "threads").rglob("*.jsonl"))


def event_count() -> int:
    with get_session() as s:
        return s.execute(text("SELECT count(*) FROM events")).scalar()


def corrupt_event_line(tf, marker: str = '{"type": "event", "id": corrupted beyond') -> str:
    """Replace the file's first event line with an unparseable one; returns the
    original line so the test can assert on what was lost."""
    from thread_archive.truth import jsonl_log

    lines = tf.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, ln in enumerate(lines) if '"type": "event"' in ln)
    original = lines[idx]
    lines[idx] = marker
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()
    return original
