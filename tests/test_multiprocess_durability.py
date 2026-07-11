"""Real-process durability: the flock/fsync/intent protocol exercised by actual
OS processes, not in-process simulation.

The crash-safety unit tests (test_truth.py, test_integrity_hardening.py,
test_drain_intent.py) simulate failure by monkeypatching seams in-process. This
suite complements them with the real thing: separate interpreters appending
concurrently to one archive home, and writers SIGKILLed mid-drain at each
protocol window (see tests/mp_child.py), followed by recovery in a fresh
process — the lock-release-on-death, torn-buffer, and intent-resolution
behavior only a real kill can produce.

Every scenario ends at the same bar: ``verify()`` ok, event content identical
to a clean control import, one copy of everything.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from thread_archive import _api as ta
from thread_archive._importers import import_session_incremental
from thread_archive._retrieval.fts import ensure_fts
from thread_archive._store import Event, Thread, get_session, init_db

pytestmark = pytest.mark.integration

CHILD = Path(__file__).parent / "mp_child.py"


def _session_lines(token: str, turns: int = 6) -> list[dict]:
    """A CC-shaped transcript: ``turns`` user/assistant pairs, every message
    carrying ``token`` so cross-thread contamination is greppable."""
    lines = []
    prev = None
    for i in range(turns):
        u, a = f"u{i}", f"a{i}"
        lines.append({"type": "user", "uuid": u, "parentUuid": prev,
                      "timestamp": f"2026-01-01T10:{i:02d}:00Z", "sessionId": "s1", "cwd": "/p",
                      "message": {"role": "user", "content": f"{token} question {i}"}})
        lines.append({"type": "assistant", "uuid": a, "parentUuid": u,
                      "timestamp": f"2026-01-01T10:{i:02d}:05Z", "sessionId": "s1",
                      "message": {"role": "assistant", "model": "m",
                                  "content": [{"type": "text", "text": f"{token} answer {i}"}]}})
        prev = a
    return lines


def _write_session(dirpath: Path, name: str, token: str) -> Path:
    f = dirpath / f"{name}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in _session_lines(token)) + "\n", encoding="utf-8")
    return f


def _spawn(mode: str, session_file: Path, source_id: str, home: Path,
           seam: str | None = None, go_file: Path | None = None) -> subprocess.Popen:
    argv = [sys.executable, str(CHILD), mode, str(session_file), source_id]
    if seam:
        argv.append(seam)
    env = {**os.environ, "THREAD_ARCHIVE_HOME": str(home)}
    if go_file:
        env["MP_GO_FILE"] = str(go_file)
    return subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _store_state() -> tuple[int, int, list[str]]:
    """(thread_count, event_count, sorted dedup_keys) of the ambient store.

    Uniqueness is asserted per thread — the schema's UNIQUE index is
    ``(thread_id, dedup_key)``; structurally identical events in *different*
    threads legitimately share a key."""
    with get_session() as s:
        threads = s.execute(select(Thread)).scalars().all()
        events = s.execute(select(Event)).scalars().all()
        pairs = [(e.thread_id, e.dedup_key) for e in events if e.dedup_key]
        assert len(pairs) == len(set(pairs)), "duplicate dedup_key within a thread"
        return len(threads), len(events), sorted(k for _t, k in pairs)


def _control_state(tmp_path: Path, token: str) -> tuple[int, int, set[str]]:
    """What a clean single-process import of the same session produces."""
    control = tmp_path / "control-home"
    ta.open_archive(str(control))
    init_db()
    f = _write_session(tmp_path, "control", token)
    import_session_incremental(f, "proj:s1")
    return _store_state()


# ── killed writers: one child per drain-protocol window ─────────────────────

@pytest.mark.parametrize("seam", ["after_intent", "mid_append", "after_drain"])
def test_writer_killed_mid_drain_recovers_to_clean_import(archive_home, tmp_path, seam) -> None:
    expected = _control_state(tmp_path, "tok-crash")

    ta.open_archive(str(archive_home))
    init_db()
    ensure_fts()

    f = _write_session(tmp_path, "victim", "tok-crash")
    proc = _spawn("import-kill", f, "proj:s1", archive_home, seam=seam)
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == -signal.SIGKILL, f"child survived its seam: {err}"

    if seam in ("after_intent", "mid_append"):
        # the crash left its intent frame behind; the next writer must resolve it
        intent = archive_home / ".drain.intent"
        assert intent.exists() and intent.read_text().strip(), "kill missed the intent window"
    else:
        # truth committed, SQLite didn't — rebuild the projection from truth
        ta.reindex()

    # recovery is a fresh process's ordinary next step: re-import the same session
    # (taking the truth write lock resolves any leftover intent on the way in)
    import_session_incremental(f, "proj:s1")

    assert _store_state() == expected
    result = ta.verify()
    assert result["ok"], f"verify failed: {result.get('failed_components')}"

    # and the store has converged: another import creates nothing
    r = import_session_incremental(f, "proj:s1")
    assert r.events_created == 0


# ── concurrent writers ───────────────────────────────────────────────────────

def test_concurrent_imports_no_cross_thread_bleed(archive_home, tmp_path) -> None:
    ta.open_archive(str(archive_home))
    init_db()
    ensure_fts()

    tokens = [f"tok-conc-{i}" for i in range(4)]
    files = [_write_session(tmp_path, f"sess{i}", tok) for i, tok in enumerate(tokens)]
    go = tmp_path / "go"
    procs = [_spawn("import", f, f"proj:s{i}", archive_home, go_file=go)
             for i, f in enumerate(files)]
    go.touch()  # release the barrier: all four append at once
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f"concurrent import failed: {err}"
        assert json.loads(out)["events_created"] > 0

    threads, events, _keys = _store_state()
    assert threads == 4
    with get_session() as s:
        rows = s.execute(select(Event)).scalars().all()
        for i, tok in enumerate(tokens):
            mine = [e for e in rows if tok in json.dumps(e.payload)]
            assert mine, f"session {i}'s content missing"
            assert len({e.thread_id for e in mine}) == 1, f"{tok} bled across threads"

    result = ta.verify()
    assert result["ok"], f"verify failed: {result.get('failed_components')}"

    # the truth alone reproduces the exact same store
    ta.reindex()
    assert _store_state() == (threads, events, _keys)


def test_same_session_import_race_lands_single_copy(archive_home, tmp_path) -> None:
    expected = _control_state(tmp_path, "tok-race")

    ta.open_archive(str(archive_home))
    init_db()
    ensure_fts()

    f = _write_session(tmp_path, "shared", "tok-race")
    go = tmp_path / "go"
    procs = [_spawn("import", f, "proj:s1", archive_home, go_file=go) for _ in range(2)]
    go.touch()
    results = [(p, *p.communicate(timeout=120)) for p in procs]

    # At least one racer lands the session. The loser either sees the winner's
    # rows and imports nothing, or trips the UNIQUE (threads.name) backstop and
    # fails loudly — the constraint exists exactly so a create/create race can't
    # double the thread. Either way the store must not hold a second copy.
    assert any(p.returncode == 0 for p, _o, _e in results)
    for p, _out, err in results:
        assert p.returncode == 0 or "UNIQUE constraint failed" in err, \
            f"racing import failed some other way: {err}"

    assert _store_state() == expected, "the same session must land exactly once"
    result = ta.verify()
    assert result["ok"], f"verify failed: {result.get('failed_components')}"
