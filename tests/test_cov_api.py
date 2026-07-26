"""Dispatch coverage for the thin ``_api`` wrappers.

The public library functions are one-line adapters over the private machinery —
``import_path`` (line-stream vs DB-scanner vs unknown-provider), ``embed``, and
``watch`` (one-shot vs looping). Their real work is covered
by the machinery's own suites; these tests pin the adapter arms — the provider
routing and the return-shape wrapping — by driving each arm end to end: a real
SQLite store for the scanner shape, and a real store of transcripts for the
watcher.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

import numpy as np
import pytest

from thread_archive import _api as ta

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello api"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi from api"}]}}


def _write_cc(path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def test_import_path_unknown_provider_raises(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        ta.import_path(f, provider="nope")


def test_open_archive_discards_engine_when_initialization_fails(
    archive_home, tmp_path
) -> None:
    """A corrupt index cannot leave its failed pool installed for later calls."""
    from thread_archive._store import active_dsn

    broken = tmp_path / "broken-home"
    broken.mkdir()
    (broken / "index.db").write_bytes(b"not a sqlite database")

    with pytest.raises(Exception, match="not a database"):
        ta.open_archive(str(broken))

    assert active_dsn() is None
    assert ta.open_archive(str(archive_home)).home == archive_home


def test_import_path_routes_to_db_scanner(archive_home) -> None:
    """A ``db-scan`` provider is handed the whole store, not a session path, and
    its result comes back verbatim — the dispatch difference between the two
    importer shapes. Driven over a real OpenCode store, since a scanner handed
    the wrong path shape imports nothing."""
    db = archive_home / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT, "
                 "title TEXT, directory TEXT, time_created INTEGER, "
                 "time_updated INTEGER, agent TEXT)")
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, "
                 "time_created INTEGER, data TEXT)")
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                 ("s1", "proj", None, "Scanned Session", "/proj",
                  1700000000000, 1700000005000, "build"))
    conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                 ("m1", "s1", 1700000000000,
                  json.dumps({"role": "user", "time": {"created": 1700000000000}})))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                 ("p1", "m1", "s1", 1700000000000,
                  json.dumps({"type": "text", "text": "scanned by the db arm",
                              "time": {"start": 1700000000000}})))
    conn.commit()
    conn.close()

    result = ta.import_path(db, provider="opencode")
    assert result.imported == 1  # the scanner's own result shape, unwrapped
    assert ta.search("scanned by the db arm")


class _FixedEmbedder:
    """An embedder that answers everything with one fixed vector and records the
    documents it was handed — the front-door stand-in for the torch model."""

    def __init__(self) -> None:
        vec = np.zeros(768, dtype=np.float32)
        vec[0] = 1.0
        self.vec = vec.tolist()
        self.documents: list[str] = []

    def is_available(self) -> bool:
        return True

    def space_key(self) -> str:
        return "local:test"

    def embed_query(self, text):
        return list(self.vec)

    def embed_documents(self, texts):
        self.documents.extend(texts)
        return [list(self.vec) for _ in texts]


def test_embed_wraps_indexed_count(archive_home) -> None:
    """``embed`` forwards its bounds to the indexer and wraps the count it got
    back. Run over a stand-in model through the ``embedder`` front door, so the
    real indexing path executes without any weights."""
    from thread_archive._retrieval import vectors

    emb = _FixedEmbedder()
    lines = []
    for i in range(3):  # 3 user + 3 assistant-text = 6 embeddable docs
        lines.append({"type": "user", "uuid": f"u{i}", "timestamp": f"2026-01-01T10:0{i}:00Z",
                      "cwd": "/p", "message": {"role": "user", "content": f"question {i}"}})
        lines.append({"type": "assistant", "uuid": f"a{i}",
                      "timestamp": f"2026-01-01T10:0{i}:30Z",
                      "message": {"role": "assistant", "model": "claude-opus-4",
                                  "content": [{"type": "text", "text": f"answer {i}"}]}})
    f = archive_home / "sess.jsonl"
    _write_cc(f, lines)
    ta.import_path(f)

    # max_events bounds the pass; newest_first picks which end of the gap drains.
    assert ta.embed(max_events=2, newest_first=True, embedder=emb) == {"embedded": 2}
    assert emb.documents and all("2" in d for d in emb.documents)  # the newest pair
    # incremental: the next call takes the next slice, then the rest, then no-ops
    assert ta.embed(max_events=2, embedder=emb) == {"embedded": 2}
    assert ta.embed(embedder=emb) == {"embedded": 2}
    assert ta.embed(embedder=emb) == {"embedded": 0}
    assert vectors.get_status()["indexed"] == 6
    # rebuild re-embeds what is already there
    assert ta.embed(rebuild=True, embedder=emb) == {"embedded": 6}


def _machine_with_a_session(tmp_path, text: str):
    """A throwaway $HOME whose only AI-tool store is one claude-code session."""
    home = tmp_path / "machine"
    _write_cc(home / ".claude" / "projects" / "proj" / "s1.jsonl", [
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
         "cwd": "/proj", "message": {"role": "user", "content": text}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "ok"}]}},
    ])
    return home


def test_watch_once_polls_under_lock(archive_home, tmp_path, monkeypatch) -> None:
    """``once=True`` runs exactly one poll and hands back its result."""
    monkeypatch.setenv("HOME", str(_machine_with_a_session(tmp_path, "watched once")))

    result = ta.watch(once=True)
    assert result is not None and result.events_created > 0
    assert ta.search("watched once")
    # One poll, not a loop: nothing new is picked up without another call.
    assert ta.watch(once=True).events_created == 0


@pytest.mark.integration
def test_watch_loop_keeps_polling(archive_home, tmp_path) -> None:
    """``once=False`` runs the daemon loop — it keeps importing what appears in
    the stores until the process is stopped. Driven in a real child process,
    because the loop only ends when the watcher is told to stop."""
    machine = _machine_with_a_session(tmp_path, "watched by the loop")
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "from thread_archive import _api; _api.watch(interval=0.1)"],
        env={**os.environ, "HOME": str(machine), "THREAD_ARCHIVE_HOME": str(archive_home),
             "THREAD_ARCHIVE_EMBED": "off", "THREAD_ARCHIVE_RERANK": "off"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert proc.poll() is None, f"watcher exited: {proc.communicate()[0][-2000:]}"
            ta.close()
            if ta.search("watched by the loop"):
                break
            time.sleep(0.1)
        else:  # pragma: no cover — the first poll lands in well under a second
            raise AssertionError("the watch loop never imported the session")

        # Still looping: a session that appears later is picked up too.
        _write_cc(machine / ".claude" / "projects" / "proj" / "s2.jsonl", [
            {"type": "user", "uuid": "u2", "timestamp": "2026-01-01T11:00:00Z",
             "cwd": "/proj", "message": {"role": "user", "content": "a later session"}},
        ])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ta.close()
            if ta.search("a later session"):
                break
            time.sleep(0.1)
        else:  # pragma: no cover
            raise AssertionError("the watch loop stopped after its first pass")
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        if proc.stdout is not None:
            proc.stdout.close()


def test_status_survives_a_dead_watch_pid_and_a_vanished_backup_dest(
    archive_home, tmp_path
) -> None:
    """Health is read from records that outlive what they describe: the watcher
    that wrote the last pass may be gone, and the backup volume may be unmounted.
    Neither may take the status report down with it."""
    from thread_archive._ops.health import record_health

    record_health("watch_pass_last", {"pid": 2 ** 31 - 1, "sources": {}})  # no such process
    record_health("backup_last", {"ok": True, "dest": str(tmp_path / "unmounted" / "bak")})

    st = ta.status()
    assert st["threads"] == 0
    assert st["last_backup"]["dest"].endswith("bak")


def test_load_status_reports_the_live_state_and_the_history(archive_home) -> None:
    """The library view of a load in flight — what ``thread_archive loads`` and
    ``GET /api/loads`` both read."""
    from thread_archive._ops import load_runs

    empty = ta.load_status()
    assert empty["home"] == str(archive_home)
    assert empty["current"] == {} and empty["recent"] == []

    with load_runs.load_run("reindex", home=archive_home) as run:
        with run.phase("reindex", total=2) as ph:
            ph.advance(2)
        live = ta.load_status()
        assert live["current"]["kind"] == "reindex"
        assert live["current"]["status"] == "running"

    done = ta.load_status()
    assert done["current"]["status"] == "ok"
    assert [r["kind"] for r in done["recent"]] == ["reindex"]


def test_archives_lists_every_registered_home(archive_home, tmp_path, monkeypatch) -> None:
    """An archive is known by having been opened — including one this process
    never opened, which is the whole point of the registry."""
    from thread_archive._ops import archives as reg

    monkeypatch.setenv("THREAD_ARCHIVE_REGISTRY", str(tmp_path / "registry.json"))
    reg._last_registered.clear()
    other = tmp_path / "other-archive"
    other.mkdir()
    reg.register(other, force=True)

    rows = ta.archives()
    by_home = {r["home"]: r for r in rows}
    assert by_home[str(archive_home)]["active"] is True
    assert by_home[str(other)]["active"] is False and by_home[str(other)]["exists"] is True


def test_amend_and_amendments_round_trip_through_the_api(archive_home) -> None:
    """``amend`` writes superseding truth lines and ``amendments`` reads the audit
    trail back — the append-only edit path, driven through the library front door."""
    from sqlalchemy import select

    from thread_archive._store import Event, get_session

    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)
    ta.checkpoint()

    thread_id = ta.search("hello api")[0]["thread_id"]
    with get_session() as s:
        event_id = s.execute(
            select(Event.id).where(Event.thread_id == thread_id).order_by(Event.id)
        ).scalars().first()

    assert ta.amendments() == []
    result = ta.amend([(thread_id, event_id, {"note": "amended by test"})], reason="cov")
    assert result["events_amended"] == 1

    trail = ta.amendments()
    assert [a["reason"] for a in trail] == ["cov"]


