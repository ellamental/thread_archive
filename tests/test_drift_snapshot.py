"""Drift quarantine: bounded, incremental preservation snapshots of a degraded
source's raw store under ``<home>/dumps/drift/<source>/``."""

from __future__ import annotations

import json
import sqlite3

import pytest

from thread_archive._watcher import drift_snapshot
from thread_archive._watcher.drift_snapshot import snapshot_degraded, snapshot_source
from thread_archive._watcher.sources import RglobWatcher


def _no_import(path, source_id):
    raise AssertionError("snapshots must never import")


@pytest.fixture()
def store(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    (root / "a.jsonl").write_text('{"line": 1}\n')
    sub = root / "deep"
    sub.mkdir()
    (sub / "b.jsonl").write_text('{"line": 2}\n')
    return root


def _watcher(root) -> RglobWatcher:
    return RglobWatcher(root, _no_import, lambda f: f.stem, name="demo-source")


def test_snapshot_copies_store_with_manifest_and_source_ids(archive_home, store):
    from pathlib import Path

    gen = snapshot_source(_watcher(store), reason="stale_ingest")
    assert gen is not None
    gen_dir = Path(gen)
    assert gen_dir.parent == archive_home / "dumps" / "drift" / "demo-source"
    manifest = json.loads((gen_dir / "manifest.json").read_text())
    assert manifest["source"] == "demo-source"
    assert manifest["reason"] == "stale_ingest"
    assert manifest["dropped"] == 0
    by_name = {f["path"].rsplit("/", 1)[-1]: f for f in manifest["files"]}
    assert set(by_name) == {"a.jsonl", "b.jsonl"}
    assert by_name["a.jsonl"]["source_id"] == "a"  # replayable after pruning
    for f in manifest["files"]:
        stored = gen_dir / f["stored"]
        assert stored.is_file()
        assert stored.read_bytes() == open(f["path"], "rb").read()


def test_snapshot_respects_refresh_window_then_deduplicates(archive_home, store, monkeypatch):
    w = _watcher(store)
    first = snapshot_source(w, reason="stale_ingest")
    assert first is not None
    # within the refresh window: no second generation, however degraded
    assert snapshot_source(w, reason="stale_ingest") is None

    # window elapsed: only *new or changed* files are copied again
    monkeypatch.setattr(drift_snapshot, "REFRESH_HOURS", 0.0)
    (store / "c.jsonl").write_text('{"line": 3}\n')
    second = snapshot_source(w, reason="stale_ingest")
    assert second is not None and second != first
    manifest = json.loads((archive_home / "dumps" / "drift" / "demo-source" /
                           second.rsplit("/", 1)[-1] / "manifest.json").read_text())
    assert [f["path"].rsplit("/", 1)[-1] for f in manifest["files"]] == ["c.jsonl"]
    # nothing new → no empty generation litter
    assert snapshot_source(w, reason="stale_ingest") is None
    # generations are never auto-deleted: both remain
    gens = [p for p in (archive_home / "dumps" / "drift" / "demo-source").iterdir()
            if p.is_dir()]
    assert len(gens) == 2


def test_snapshot_bounds_drop_oldest_and_record_it(archive_home, store, monkeypatch):
    monkeypatch.setattr(drift_snapshot, "MAX_FILES", 1)
    import os
    import time

    now = time.time()
    os.utime(store / "a.jsonl", (now, now))  # newest → kept
    os.utime(store / "deep" / "b.jsonl", (now - 1000, now - 1000))
    gen = snapshot_source(_watcher(store), reason="capture_skips")
    manifest = json.loads((archive_home / "dumps" / "drift" / "demo-source" /
                           gen.rsplit("/", 1)[-1] / "manifest.json").read_text())
    assert len(manifest["files"]) == 1
    assert manifest["files"][0]["path"].endswith("a.jsonl")  # newest-first priority
    assert manifest["dropped"] == 1  # the cap is loud, not silent


def test_sqlite_stores_copy_through_the_backup_api(archive_home, tmp_path):
    db = tmp_path / "store" / "live.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()

    gen = snapshot_source(_watcher(db.parent), reason="went_dark")
    # RglobWatcher globs *.jsonl by default — use a db-shaped watcher instead
    assert gen is None

    class DbWatcher:
        source_name = "demo-db"

        def store_paths(self):
            return iter([db])

    gen = snapshot_source(DbWatcher(), reason="went_dark")
    stored = next(
        p for p in (archive_home / "dumps" / "drift" / "demo-db").rglob("live.db")
    )
    out = sqlite3.connect(stored)
    assert out.execute("SELECT x FROM t").fetchone() == (42,)
    assert out.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    out.close()


def test_snapshot_degraded_covers_only_degraded_sources_with_watchers(archive_home, store):
    degraded = {
        "demo-source": {"reason": "stale_ingest", "since": None},
        "absent-source": {"reason": "went_dark", "since": None},
    }
    written = snapshot_degraded([_watcher(store)], degraded)
    assert set(written) == {"demo-source"}


def test_snapshot_skips_watchers_that_cannot_enumerate(archive_home):
    class Blind:
        source_name = "blind"

        def store_paths(self):
            return iter(())

    assert snapshot_source(Blind(), reason="went_dark") is None
    assert not (archive_home / "dumps" / "drift" / "blind").exists()
