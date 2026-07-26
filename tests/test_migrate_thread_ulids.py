"""The one-shot integer-id → ULID truth migration (`migrate_thread_ulids`).

Drives the real `main()` over a hand-built v1 (pre-ULID) store — integer-named
thread files, a legacy-shaped `index.db`, overlay snapshots, kg events — and
verifies the full contract: build-new → swap → v2 manifest, ULID primary ids
with `legacy_id` aliases, every reference mapped (overlays, kg entity ids and
payloads, `branched_from`), the mapping persisted to `ulid-mapping.json`, the
pre-migration truth kept in `pre-ulid-backup/`, and a post-migration reindex
that leaves search working. Edge arms: dry-run, an already-v2 store, stray and
torn truth lines, threads the index never knew, and sharded output depth.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from thread_archive._scripts.migrate_thread_ulids import (
    Migrator,
    _parse_ts_ms,
    build_mapping,
    main,
)
from thread_archive._store.ulid import normalize_ulid, ulid_timestamp_ms

T1_START = "2026-01-01T09:00:00+00:00"   # tz-aware inserted_at
T1_FIRST_EVENT = "2026-01-01T10:00:00"   # naive → treated as UTC
T1_FIRST_EVENT_MS = int(datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp() * 1000)


def _mint_index_db(home: Path) -> None:
    """A legacy-shaped index: integer thread ids, only the columns main() reads."""
    conn = sqlite3.connect(home / "index.db")
    conn.executescript(
        "CREATE TABLE threads (id INTEGER PRIMARY KEY, inserted_at TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY, thread_id INTEGER, occurred_at TEXT);"
    )
    conn.executemany(
        "INSERT INTO threads (id, inserted_at) VALUES (?, ?)",
        [(1, T1_START), (2, None), (4, "not-a-date")],
    )
    conn.executemany(
        "INSERT INTO events (id, thread_id, occurred_at) VALUES (?, ?, ?)",
        [
            (101, 1, T1_FIRST_EVENT),
            (102, 1, "2026-01-01T11:00:00"),
            (201, 2, ""),           # falsy → no start refinement
            (401, 4, "garbage"),    # unparseable → no start refinement
        ],
    )
    conn.commit()
    conn.close()


def _ev(eid: int, tid: object, text: str, when: str, **extra) -> dict:
    rec = {
        "type": "event", "id": eid, "stream_id": "s1",
        "event_type": "text_complete",
        "payload": {"block_index": 0, "text": text},
        "occurred_at": when, "recorded_at": when,
        "dedup_key": f"k{eid}",
    }
    if tid is not None:
        rec["thread_id"] = tid
    rec.update(extra)
    return rec


def _jl(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n",
        encoding="utf-8",
    )


def make_legacy_home(home: Path, *, with_unindexed: bool = True, with_kg: bool = True) -> None:
    """A v1 truth tree + legacy index under ``home``."""
    truth = home / "truth"
    threads = truth / "threads"
    _mint_index_db(home)
    _jl(truth / "manifest.json", [json.dumps(
        {"version": 1, "shard_depth": 0, "last_checkpoint_at": None,
         "hashes_baseline": {"threads/1.jsonl": "deadbeef"}})])
    # thread 1: two metadata records (first meta-less, then a branched_from), a
    # thread_id-less event, a blank line, and a torn line.
    _jl(threads / "1.jsonl", [
        {"type": "thread", "id": 1, "name": "alpha-old", "source": "claude-code",
         "thread_type": "conversation", "inserted_at": T1_START},
        _ev(101, 1, "hello ulid world", "2026-01-01T10:00:00+00:00"),
        "",
        _ev(102, None, "second message", "2026-01-01T11:00:00+00:00"),
        '{"torn',
        {"type": "thread", "id": 1, "name": "alpha", "source": "claude-code",
         "thread_type": "conversation", "inserted_at": T1_START,
         "source_metadata": {"cwd": "/p", "branched_from": 2}},
    ])
    # thread 2: events only — the migration must synthesize a metadata stub.
    _jl(threads / "2.jsonl", [
        _ev(201, 2, "orphan events here", "2026-01-02T10:00:00+00:00"),
    ])
    if with_unindexed:
        # thread 3: known only to truth, never to the index — a ULID is minted.
        _jl(threads / "3.jsonl", [
            {"type": "thread", "id": 3, "name": "ghost", "source": "claude-code",
             "thread_type": "conversation", "inserted_at": "2026-01-03T10:00:00+00:00"},
            _ev(301, 3, "unindexed thread", "2026-01-03T10:00:00+00:00"),
        ])
    _jl(threads / "stray.jsonl", [{"type": "note", "text": "not a thread file"}])
    # overlays — amendments.jsonl deliberately absent.
    _jl(truth / "thread_links.jsonl", [
        {"id": 1, "source_thread_id": 1, "target_thread_id": 2, "link_type": "related",
         "strength": 1.0, "created_by": "auto", "created_by_thread_id": None,
         "created_at": T1_START, "updated_at": T1_START},
        "",
        '{"torn',
    ])
    _jl(truth / "topic_messages.jsonl", [
        {"id": 1, "topic_id": "2", "thread_id": 1, "event_id": 101,
         "quote": "hello", "created_by_thread_id": 1, "actor": "librarian",
         "created_at": T1_START},
    ])
    _jl(truth / "import_state.jsonl", [
        {"id": 1, "source": "claude-code", "source_id": "proj:s1", "thread_id": 1,
         "last_line_count": 4, "last_file_size": 100},
    ])
    if with_kg:
        _jl(truth / "kg_events.jsonl", [
            {"id": 1, "event_type": "test_noise", "entity_type": "topic",
             "entity_id": 2, "actor_thread_id": 1, "actor": "librarian",
             "payload": {"thread_id": 1, "name": "t"}, "occurred_at": T1_START},
            {"id": 2, "event_type": "test_noise", "entity_type": "link",
             "entity_id": "1:2:related", "actor": "librarian", "actor_thread_id": None,
             "payload": {"source_thread_id": 1, "target_thread_id": 2},
             "occurred_at": T1_START},
            {"id": 3, "event_type": "test_noise", "entity_type": "topic_message",
             "entity_id": "2:101", "actor": "librarian", "actor_thread_id": None,
             "payload": {"topic_id": 2, "event_id": 101}, "occurred_at": T1_START},
            {"id": 4, "event_type": "test_noise", "entity_type": "thread",
             "entity_id": "abc", "actor": "librarian", "actor_thread_id": None,
             "payload": "not-a-dict",
             "occurred_at": T1_START},
            {"id": 5, "event_type": "test_noise", "entity_type": "topic",
             "entity_id": None, "actor": "librarian", "actor_thread_id": None,
             "payload": {"thread_id": True}, "occurred_at": T1_START},
            "",
            '{"torn',
        ])


def _run_main(home: Path, *extra: str) -> int:
    return main(["--home", str(home), *extra])


def _load_mapping(home: Path) -> dict[str, str]:
    return json.loads((home / "ulid-mapping.json").read_text())


# ── unit seams ───────────────────────────────────────────────────────────────

def test_parse_ts_ms_arms() -> None:
    assert _parse_ts_ms(None) is None
    assert _parse_ts_ms("") is None
    assert _parse_ts_ms("not-a-date") is None
    assert _parse_ts_ms(T1_FIRST_EVENT) == T1_FIRST_EVENT_MS          # naive → UTC
    assert _parse_ts_ms("2026-01-01T10:00:00+00:00") == T1_FIRST_EVENT_MS  # aware


def test_build_mapping_timestamps_and_fallbacks(archive_home) -> None:
    make_legacy_home(archive_home)
    mapping = build_mapping(archive_home / "index.db")
    assert set(mapping) == {1, 2, 4}
    for ulid in mapping.values():
        assert normalize_ulid(ulid) == ulid
    # thread 1's ULID timestamp is its first event's occurred_at, not inserted_at
    assert ulid_timestamp_ms(mapping[1]) == T1_FIRST_EVENT_MS
    # threads 2 and 4 had no parseable start — minted at "now"
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    for tid in (2, 4):
        assert abs(ulid_timestamp_ms(mapping[tid]) - now_ms) < 60_000


def test_map_id_arms(archive_home) -> None:
    m = Migrator(archive_home, {1: "01ARZ3NDEKTSV4RRFFQ69G5FAV"})
    assert m.map_id(None) is None
    assert m.map_id(True) is True
    assert m.map_id("not-a-number") == "not-a-number"
    assert m.map_id(1) == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert m.map_id("1") == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert m.minted_unindexed == 0
    minted = m.map_id(99)
    assert normalize_ulid(str(minted)) == minted
    assert m.map_id("99") == minted, "second sighting reuses the minted id"
    assert m.minted_unindexed == 1


def test_rewrite_overlay_missing_file_is_a_noop(archive_home) -> None:
    (archive_home / "truth").mkdir(parents=True)
    m = Migrator(archive_home, {})
    assert m.rewrite_overlay("amendments.jsonl", ("thread_id",)) == 0
    assert m.rewrite_kg_events() == 0


# ── dry run ──────────────────────────────────────────────────────────────────

def test_dry_run_builds_but_never_swaps(archive_home, monkeypatch, capsys) -> None:
    make_legacy_home(archive_home, with_unindexed=False, with_kg=False)
    truth = archive_home / "truth"
    before = (truth / "threads" / "1.jsonl").read_text()

    assert _run_main(archive_home, "--dry-run") == 0
    out = capsys.readouterr()
    assert "dry run" in out.out
    assert "minted" not in out.out          # every truth thread was indexed
    assert "stray" in out.err               # non-integer stem reported, skipped
    # new tree and overlay siblings built; originals untouched; no swap artifacts
    assert (truth / "threads.new").is_dir()
    assert (truth / "thread_links.jsonl.new").exists()
    assert (truth / "threads" / "1.jsonl").read_text() == before
    assert json.loads((truth / "manifest.json").read_text())["version"] == 1
    assert not (archive_home / "pre-ulid-backup").exists()
    # the mapping is still written — it's the durable record either way
    assert set(_load_mapping(archive_home)) == {"1", "2", "4"}


# ── the full migration ───────────────────────────────────────────────────────

@pytest.fixture
def migrated(archive_home, monkeypatch, capsys):
    make_legacy_home(archive_home)
    # a leftover threads.new from an aborted earlier run must be cleared
    junk = archive_home / "truth" / "threads.new" / "leftover.jsonl"
    junk.parent.mkdir(parents=True)
    junk.write_text("{}\n")
    assert _run_main(archive_home) == 0
    return archive_home, _load_mapping(archive_home), capsys.readouterr()


def test_migration_rewrites_threads(migrated) -> None:
    home, mapping, out = migrated
    truth = home / "truth"
    assert "minted 1 id(s)" in out.out       # thread 3 was truth-only
    assert set(mapping) == {"1", "2", "3", "4"}
    u1, u2, u3 = mapping["1"], mapping["2"], mapping["3"]
    assert not (truth / "threads.new").exists()

    f1 = [json.loads(ln) for ln in (truth / "threads" / f"{u1}.jsonl").read_text().splitlines()]
    metas = [r for r in f1 if r["type"] == "thread"]
    events = [r for r in f1 if r["type"] == "event"]
    assert all(r["id"] == u1 and r["legacy_id"] == 1 for r in metas)
    assert metas[-1]["source_metadata"]["branched_from"] == u2
    assert "source_metadata" not in metas[0]  # meta-less record passes through
    assert [e["id"] for e in events] == [101, 102]  # event ids stay integers; torn line dropped
    assert all(e["thread_id"] == u1 for e in events)

    # thread 2 had no metadata record — a stub carries the legacy alias
    f2 = [json.loads(ln) for ln in (truth / "threads" / f"{u2}.jsonl").read_text().splitlines()]
    assert f2[0] == {"type": "thread", "id": u2, "legacy_id": 2, "name": "thread:2"}
    assert (truth / "threads" / f"{u3}.jsonl").exists()
    # thread 4 never had a truth file; mapped but nothing written
    assert not (truth / "threads" / f"{mapping['4']}.jsonl").exists()
    assert not (truth / "threads" / "stray.jsonl").exists()


def test_migration_rewrites_overlays_and_kg(migrated) -> None:
    home, mapping, _ = migrated
    truth = home / "truth"
    u1, u2 = mapping["1"], mapping["2"]

    (link,) = [json.loads(ln) for ln in (truth / "thread_links.jsonl").read_text().splitlines()]
    assert (link["source_thread_id"], link["target_thread_id"]) == (u1, u2)
    assert link["created_by_thread_id"] is None
    (tm,) = [json.loads(ln) for ln in (truth / "topic_messages.jsonl").read_text().splitlines()]
    assert (tm["topic_id"], tm["thread_id"], tm["created_by_thread_id"]) == (u2, u1, u1)
    assert tm["event_id"] == 101
    (ist,) = [json.loads(ln) for ln in (truth / "import_state.jsonl").read_text().splitlines()]
    assert ist["thread_id"] == u1
    assert not (truth / "amendments.jsonl").exists()

    kg = {r["id"]: r for r in
          (json.loads(ln) for ln in (truth / "kg_events.jsonl").read_text().splitlines())}
    assert len(kg) == 5                      # torn + blank lines dropped
    assert kg[1]["entity_id"] == u2
    assert kg[1]["actor_thread_id"] == u1
    assert kg[1]["payload"]["thread_id"] == u1
    assert kg[2]["entity_id"] == f"{u1}:{u2}:related"
    assert kg[2]["payload"] == {"source_thread_id": u1, "target_thread_id": u2}
    assert kg[3]["entity_id"] == f"{u2}:101"
    assert kg[3]["payload"] == {"topic_id": u2, "event_id": 101}
    assert kg[4]["entity_id"] == "abc"       # unknown entity_type: untouched
    assert kg[4]["payload"] == "not-a-dict"  # non-dict payloads pass through
    assert kg[5]["entity_id"] is None
    assert kg[5]["payload"] == {"thread_id": True}  # bools are not thread ids


def test_migration_backup_and_manifest(migrated) -> None:
    home, mapping, _ = migrated
    backup = home / "pre-ulid-backup"
    assert json.loads((backup / "manifest.v1.json").read_text())["version"] == 1
    old1 = [json.loads(ln) for ln in (backup / "threads" / "1.jsonl").read_text().splitlines()
            if ln.strip() and not ln.startswith('{"torn')]
    assert old1[0]["id"] == 1                # pre-migration truth intact, integer ids
    assert (backup / "threads" / "stray.jsonl").exists()
    for name in ("thread_links.jsonl", "topic_messages.jsonl",
                 "import_state.jsonl", "kg_events.jsonl"):
        assert (backup / name).exists(), name

    manifest = json.loads((home / "truth" / "manifest.json").read_text())
    assert manifest["version"] == 2
    assert manifest["shard_depth"] == 0
    assert "hashes_baseline" not in manifest  # every line changed; baseline void


def test_migration_then_reindex_leaves_search_working(migrated) -> None:
    from sqlalchemy import select

    from thread_archive import _api
    from thread_archive._store import Thread, get_session

    home, mapping, _ = migrated
    u1 = mapping["1"]
    (home / "index.db").unlink()             # operator step: index deleted, rebuilt
    counts = _api.reindex()
    assert counts["threads"] == 3
    with get_session() as s:
        rows = {t.id: t for t in s.execute(select(Thread)).scalars()}
        assert rows[u1].legacy_id == 1
        assert rows[u1].name == "alpha"      # last metadata record won
        assert rows[mapping["2"]].name == "thread:2"
        assert {rows[mapping[k]].legacy_id for k in ("1", "2", "3")} == {1, 2, 3}
    hits = _api.search("hello ulid")
    assert any(h["thread_id"] == u1 for h in hits)


def test_second_run_refuses_already_migrated(migrated, monkeypatch, capsys) -> None:
    home, _, _ = migrated
    tree_before = sorted(p.relative_to(home) for p in home.rglob("*"))
    assert _run_main(home) == 0
    assert "already at truth format v2" in capsys.readouterr().out
    assert sorted(p.relative_to(home) for p in home.rglob("*")) == tree_before


def test_migration_refuses_newer_truth(archive_home) -> None:
    (archive_home / "truth").mkdir(parents=True)
    (archive_home / "truth" / "manifest.json").write_text(json.dumps({"version": 3}))
    with pytest.raises(RuntimeError, match="cannot migrate truth format v3"):
        _run_main(archive_home)


def test_module_entrypoint_runs_main(archive_home) -> None:
    """``python -m thread_archive._scripts.migrate_thread_ulids`` is the way an
    operator runs this — a real child process, parsing its own argv and exiting
    with main()'s return code."""
    (archive_home / "truth").mkdir(parents=True)
    (archive_home / "truth" / "manifest.json").write_text(json.dumps({"version": 2}))
    proc = subprocess.run(
        [sys.executable, "-m", "thread_archive._scripts.migrate_thread_ulids",
         "--home", str(archive_home)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "already at truth format v2" in proc.stdout


def test_migration_shards_when_over_flat_max(archive_home, monkeypatch) -> None:
    import hashlib


    make_legacy_home(archive_home, with_unindexed=False, with_kg=False)
    monkeypatch.setenv("THREAD_ARCHIVE_SHARDFLAT_MAX", "1")  # force a sharded (depth-1) layout
    assert _run_main(archive_home) == 0
    mapping = _load_mapping(archive_home)
    manifest = json.loads((archive_home / "truth" / "manifest.json").read_text())
    assert manifest["shard_depth"] == 1
    for legacy in ("1", "2"):
        ulid = mapping[legacy]
        bucket = hashlib.sha256(ulid.encode()).hexdigest()[:2]
        assert (archive_home / "truth" / "threads" / bucket / f"{ulid}.jsonl").exists()


def test_migration_repairs_mixed_v1_and_ulid_truth(archive_home) -> None:
    """The historical bug could append a v2 thread under a v1 manifest.

    Recovery must migrate the integer files without discarding the already-ULID
    truth-only file that the failed SQLite commit left behind.
    """
    make_legacy_home(archive_home, with_unindexed=False, with_kg=False)
    existing = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    _jl(archive_home / "truth" / "threads" / f"{existing}.jsonl", [
        {"type": "thread", "id": existing, "name": "mixed survivor",
         "thread_type": "conversation"},
        _ev(901, existing, "survives migration", "2026-01-04T10:00:00+00:00"),
    ])

    assert _run_main(archive_home) == 0
    assert (archive_home / "truth" / "threads" / f"{existing}.jsonl").exists()
    records = [
        json.loads(line)
        for line in (archive_home / "truth" / "threads" / f"{existing}.jsonl")
        .read_text().splitlines()
    ]
    assert records[0]["id"] == existing
    assert records[1]["thread_id"] == existing
    assert existing not in _load_mapping(archive_home).values()
    assert (archive_home / "pre-ulid-backup" / "threads" / f"{existing}.jsonl").exists()
