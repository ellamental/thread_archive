"""Malformed-input robustness over the line-stream providers.

The vendored parsers face the most adversarial input in the system:
undocumented provider formats that change without notice, files torn by
crashes, and encodings mangled in transit. The provider goldens lock each
importer's output for *well-formed* input; this suite locks the failure
behavior for damaged input. The contract, per provider:

- a damaged line must never abort the import — every intact line around it
  still lands;
- garbage must never fabricate events — inserted junk adds nothing;
- where the importer counts parse drops (``parse_errors``), a dropped line
  is counted, never silent;
- repairing a damaged file converges: re-importing the intact version over a
  truncated first import reaches exactly the intact yield, no duplicates
  (the content-hash cursor rewind path).

Each case runs the real importer end-to-end into a throwaway home — the
robustness being pinned is the pipeline's, not a parser function's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from thread_archive._store import Event, get_session, init_db

from .test_provider_goldens import (
    ANTIGRAVITY,
    CLAUDE_CODE,
    CODEX,
    GROK,
    GROK_SUMMARY,
)


def _event_count() -> int:
    with get_session() as s:
        return int(s.execute(select(func.count()).select_from(Event)).scalar_one())


# ── provider runners: (name, fixture lines, import-text-into-home) ──────────


def _run_claude_code(home: Path, text: str):
    from thread_archive._importers import import_session_incremental

    f = home / "sess.jsonl"
    f.write_text(text, encoding="utf-8")
    return import_session_incremental(f, "proj:s1")


def _run_codex(home: Path, text: str):
    from thread_archive._importers import import_codex_session_incremental

    f = home / "codex.jsonl"
    f.write_text(text, encoding="utf-8")
    return import_codex_session_incremental(f, "codex-sess")


def _run_grok(home: Path, text: str):
    from thread_archive._importers import import_grok_session_incremental

    session_dir = home / "grok-sess"
    session_dir.mkdir(exist_ok=True)
    (session_dir / "summary.json").write_text(json.dumps(GROK_SUMMARY), encoding="utf-8")
    f = session_dir / "chat_history.jsonl"
    f.write_text(text, encoding="utf-8")
    return import_grok_session_incremental(f, "grok-sess")


def _run_antigravity(home: Path, text: str):
    from thread_archive._importers import import_antigravity_session_incremental

    f = home / "transcript.jsonl"
    f.write_text(text, encoding="utf-8")
    return import_antigravity_session_incremental(f, "ag-conv")


PROVIDERS = [
    ("claude-code", CLAUDE_CODE, _run_claude_code),
    ("codex", CODEX, _run_codex),
    ("grok", GROK, _run_grok),
    ("antigravity", ANTIGRAVITY, _run_antigravity),
]
PROVIDER_IDS = [name for name, _, _ in PROVIDERS]


def _text(lines: list[dict]) -> str:
    return "\n".join(json.dumps(ln) for ln in lines) + "\n"


def _baseline(home: Path, runner, lines: list[dict]) -> int:
    """Import the intact fixture and return the store's event count."""
    runner(home, _text(lines))
    return _event_count()


# ── damaged lines never abort the import ─────────────────────────────────────


@pytest.mark.parametrize("name,lines,runner", PROVIDERS, ids=PROVIDER_IDS)
def test_truncated_interior_line_keeps_the_rest(archive_home, name, lines, runner):
    """A line cut mid-JSON (torn write, partial copy) is dropped, not fatal:
    every other line's content still imports."""
    init_db()
    serialized = [json.dumps(ln) for ln in lines]
    serialized[0] = serialized[0][: max(4, len(serialized[0]) * 6 // 10)]
    result = runner(archive_home, "\n".join(serialized) + "\n")
    assert result is not None
    assert _event_count() >= 1  # the intact lines landed


@pytest.mark.parametrize("name,lines,runner", PROVIDERS, ids=PROVIDER_IDS)
def test_junk_and_blank_lines_add_nothing_and_abort_nothing(
    archive_home, name, lines, runner
):
    """Non-JSON junk and blank lines interleaved through the file neither
    crash the import nor fabricate events."""
    init_db()
    intact = _baseline(archive_home, runner, lines)

    # Same source id: the junk-riddled file re-imports over the intact one via
    # the content-hash rewind; dedup collapses the overlap, so any count above
    # `intact` is an event fabricated from garbage.
    serialized = [json.dumps(ln) for ln in lines]
    mangled = []
    for s in serialized:
        mangled.append(s)
        mangled.append("")  # blank
        mangled.append("   ")  # whitespace-only
    mangled.insert(0, "this is not json {{{")
    mangled.append("\x00\x00 binary-ish garbage \xff")
    runner(archive_home, "\n".join(mangled) + "\n")
    assert _event_count() == intact


@pytest.mark.parametrize("name,lines,runner", PROVIDERS, ids=PROVIDER_IDS)
def test_wrong_typed_fields_do_not_abort(archive_home, name, lines, runner):
    """A line whose fields carry the wrong types (null ids, numeric messages,
    object timestamps) — the classic soft-format-drift shape — must not
    abort the import of the intact lines around it."""
    init_db()
    hostile = dict(lines[0])
    for key in ("message", "content", "payload", "timestamp", "uuid", "created_at"):
        if key in hostile:
            hostile[key] = {"unexpected": ["shape", 42]} if key != "uuid" else None
    serialized = [json.dumps(ln) for ln in lines] + [json.dumps(hostile)]
    result = runner(archive_home, "\n".join(serialized) + "\n")
    assert result is not None
    assert _event_count() >= 1


@pytest.mark.parametrize("name,lines,runner", PROVIDERS, ids=PROVIDER_IDS)
def test_unknown_line_type_does_not_abort(archive_home, name, lines, runner):
    """A line type the parser has never seen (a provider update) must not
    abort the import; the intact lines still land."""
    init_db()
    unknown = {
        "type": "totally_new_line_type_v99",
        "uuid": "x-unknown-1",
        "timestamp": "2026-01-01T10:00:09Z",
        "payload": {"future": True},
    }
    serialized = [json.dumps(ln) for ln in lines] + [json.dumps(unknown)]
    result = runner(archive_home, "\n".join(serialized) + "\n")
    assert result is not None
    assert _event_count() >= 1


# ── drops are counted where the pipeline accounts for them ──────────────────


def test_claude_code_counts_parse_drops(archive_home):
    """The claude-code path reports dropped-unparseable lines on the result —
    the watcher's health accounting depends on this number being real."""
    init_db()
    serialized = [json.dumps(ln) for ln in CLAUDE_CODE]
    serialized.insert(1, "not json at all")
    serialized.insert(3, '{"torn": "mid')
    result = _run_claude_code(archive_home, "\n".join(serialized) + "\n")
    assert result.parse_errors == 2


def test_claude_code_preserves_unknown_line_types(archive_home):
    """Unknown line types are preserved as events, not consumed — the
    format-drift insurance the walkthroughs rely on."""
    init_db()
    intact = _baseline(archive_home, _run_claude_code, CLAUDE_CODE)

    init_db()
    unknown = {
        "type": "totally_new_line_type_v99",
        "uuid": "x-unknown-2",
        "timestamp": "2026-01-01T10:00:09Z",
        "sessionId": "s2",
        "payload": {"future": True},
    }
    home2 = archive_home / "second"
    home2.mkdir()
    _run_claude_code(home2, _text(CLAUDE_CODE + [unknown]))
    assert _event_count() == intact + 1


# ── repair converges: truncated import + intact re-import = intact yield ────


@pytest.mark.parametrize("name,lines,runner", PROVIDERS, ids=PROVIDER_IDS)
def test_repaired_file_reimport_converges(archive_home, name, lines, runner):
    """Import a file with a truncated first line, then repair it (write the
    intact version) and re-import under the same source id: the repaired
    line's content is recovered, and a further re-import adds nothing. This
    is the interior-line-repair shape the content-hash cursor rewind exists
    for — the damaged import must not pin the watermark past the repair."""
    init_db()
    serialized = [json.dumps(ln) for ln in lines]
    serialized[0] = serialized[0][: max(4, len(serialized[0]) * 6 // 10)]
    runner(archive_home, "\n".join(serialized) + "\n")
    damaged = _event_count()

    runner(archive_home, _text(lines))  # the repaired file, same source id
    repaired = _event_count()
    assert repaired > damaged  # the truncated line's content was recovered

    runner(archive_home, _text(lines))  # idempotent: nothing re-lands
    assert _event_count() == repaired
