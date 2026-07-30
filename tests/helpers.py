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

from thread_archive import _api as ta
from thread_archive._store import get_session


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
    from thread_archive._truth import jsonl_log

    lines = tf.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, ln in enumerate(lines) if '"type": "event"' in ln)
    original = lines[idx]
    lines[idx] = marker
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()
    return original


_CC_SHAPED_PLUGIN_HEAD = '''
from pathlib import Path

from thread_archive.provider import Provider, RglobWatcher, claude_code_line_stream
from thread_archive.provider.parse import CLAUDE_CODE_CONFIG


def _rglob(name, root):
    """One transcript file per session under ``root``, named for its source id."""
    return Provider(
        name=name, label=name, parser_id="claude-code", kind="line-stream",
        parser_config=CLAUDE_CODE_CONFIG.derive(name),
        importer=claude_code_line_stream(name),
        watcher=lambda: RglobWatcher(
            Path(root), claude_code_line_stream(name), lambda p: p.stem, name=name),
    )


def _bare(name):
    """Declares the parser, ships no watcher — nothing on disk to walk."""
    return Provider(
        name=name, label=name, parser_id="claude-code", kind="line-stream",
        parser_config=CLAUDE_CODE_CONFIG.derive(name),
        importer=claude_code_line_stream(name), watcher=None,
    )

'''


def install_cc_shaped_plugin(archive_home, monkeypatch, *, stores, watcherless=()):
    """Install plugin providers that reuse the Claude Code parser, the way a plugin
    author installs one: a module declared under ``config.json``'s ``providers``
    key and discovered when the registry is built.

    ``stores`` maps provider name → the root its transcripts live under (one
    ``<source_id>.jsonl`` per session, anywhere below it); ``watcherless`` names
    providers that declare the parser but ship no watcher. ``$HOME`` is pointed at
    an empty directory so no built-in Claude-Code-shaped store can be present
    either, leaving these the only providers with anything on disk.

    The caller resets the registry when it is done — the fresh registry is cached.
    """
    from thread_archive import _providers

    empty_home = archive_home / "_no-tools-home"
    empty_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(empty_home))

    pkg = archive_home / "plugin-src"
    pkg.mkdir(exist_ok=True)
    # Distinct module name per home: a module already imported under this name is
    # reused whatever the declared path says, and each test's stores are its own.
    module = f"cc_shaped_archive_{abs(hash(str(archive_home))):x}"

    src = [_CC_SHAPED_PLUGIN_HEAD]
    declared: dict[str, dict] = {}
    for i, (name, root) in enumerate(sorted(stores.items())):
        src.append(f"P{i} = _rglob({name!r}, {str(root)!r})\n")
        declared[name] = {"module": f"{module}:P{i}", "path": str(pkg)}
    for i, name in enumerate(sorted(watcherless)):
        src.append(f"W{i} = _bare({name!r})\n")
        declared[name] = {"module": f"{module}:W{i}", "path": str(pkg)}

    (pkg / f"{module}.py").write_text("".join(src), encoding="utf-8")
    (archive_home / "config.json").write_text(
        json.dumps({"providers": declared}), encoding="utf-8")

    _providers.reset()
    found = {p.name for p in _providers.sources_using_parser("claude-code")}
    missing = set(declared) - found
    assert not missing, f"declared plugins were not discovered: {sorted(missing)}"
