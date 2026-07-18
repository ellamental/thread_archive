"""The raw source mirror: verbatim, compressed, and never deleted.

Exercises the real sweep against stub watchers (the conftest keeps the real
enabled set away from tests): file transcripts round-trip byte-for-byte
through gzip, unchanged files are stat-skipped, a shrunken transcript rotates
a generation instead of overwriting the only copy, JSON sidecars ride along
under the size cap, SQLite stores snapshot consistently via the backup API
with one ``.prev`` generation, and unsupported watcher shapes are reported
rather than silently skipped.
"""

from __future__ import annotations

import gzip
import json
import sqlite3

import pytest

from thread_archive._ops.source_mirror import MIRROR_SUBDIR, mirror_sources
from thread_archive._watcher.base import SourceWatcher
from thread_archive._watcher.sources import DbScanWatcher, RglobWatcher


@pytest.fixture
def stub_watchers(monkeypatch):
    """Install the given watchers as the enabled set for mirror_sources."""
    from thread_archive._watcher import sources as watcher_sources

    def install(watchers):
        monkeypatch.setattr(
            watcher_sources, "enabled_watchers", lambda home=None: watchers
        )

    return install


def _file_watcher(root) -> RglobWatcher:
    return RglobWatcher(
        root, importer=lambda *a, **kw: None, source_id_of=lambda p: p.stem,
        name="stubprov",
    )


def _mirrored(archive_home, provider: str, src) -> bytes:
    dest = archive_home / MIRROR_SUBDIR / provider
    dest = dest.joinpath(*src.resolve().parts[1:])
    return gzip.decompress(dest.with_name(dest.name + ".gz").read_bytes())


def test_transcripts_and_sidecars_round_trip(archive_home, tmp_path, stub_watchers):
    store = tmp_path / "store"
    (store / "sess").mkdir(parents=True)
    transcript = store / "sess" / "one.jsonl"
    transcript.write_text('{"type": "user"}\n', encoding="utf-8")
    sidecar = store / "sess" / "summary.json"
    sidecar.write_text('{"title": "t"}', encoding="utf-8")
    big = store / "sess" / "big.json"
    big.write_text("x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
    stub_watchers([_file_watcher(store)])

    r = mirror_sources(home=str(archive_home))

    assert r["ok"] is True
    p = r["providers"]["stubprov"]
    assert p["copied"] == 2 and p["sidecars"] == 1 and p["sidecars_capped"] == 1
    assert _mirrored(archive_home, "stubprov", transcript) == transcript.read_bytes()
    assert _mirrored(archive_home, "stubprov", sidecar) == sidecar.read_bytes()
    # Health record lands for `archive status`.
    health = json.loads((archive_home / "health.json").read_text())
    assert health["source_mirror_last"]["ok"] is True

    # Second sweep: nothing changed, nothing copied — stat-skip via manifest.
    r2 = mirror_sources(home=str(archive_home))
    p2 = r2["providers"]["stubprov"]
    assert p2["copied"] == 0 and p2["unchanged"] == p["copied"]

    # Growth recopies; the mirror follows the source.
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write('{"type": "assistant"}\n')
    r3 = mirror_sources(home=str(archive_home))
    assert r3["providers"]["stubprov"]["copied"] == 1
    assert _mirrored(archive_home, "stubprov", transcript) == transcript.read_bytes()


def test_shrunken_transcript_rotates_a_generation(archive_home, tmp_path, stub_watchers):
    store = tmp_path / "store"
    store.mkdir()
    transcript = store / "one.jsonl"
    transcript.write_text("line-1\nline-2\n", encoding="utf-8")
    stub_watchers([_file_watcher(store)])
    mirror_sources(home=str(archive_home))

    transcript.write_text("rewritten\n", encoding="utf-8")
    r = mirror_sources(home=str(archive_home))

    assert r["providers"]["stubprov"]["generations"] == 1
    assert _mirrored(archive_home, "stubprov", transcript) == b"rewritten\n"
    dest_dir = (archive_home / MIRROR_SUBDIR / "stubprov").joinpath(
        *transcript.resolve().parts[1:-1]
    )
    gen = dest_dir / "one.jsonl.gz.1"
    assert gzip.decompress(gen.read_bytes()) == b"line-1\nline-2\n"


def test_sqlite_store_snapshots_with_one_prev_generation(
    archive_home, tmp_path, stub_watchers
):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('first')")
    conn.commit()
    conn.close()
    stub_watchers([DbScanWatcher(db, "stubdb", scanner=lambda *a: None)])

    r = mirror_sources(home=str(archive_home))
    assert r["providers"]["stubdb"]["copied"] == 1

    dest_dir = (archive_home / MIRROR_SUBDIR / "stubdb").joinpath(
        *db.resolve().parts[1:-1]
    )
    snap = dest_dir / "state.db.gz"
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(snap.read_bytes()))
    rows = sqlite3.connect(restored).execute("SELECT v FROM t").fetchall()
    assert rows == [("first",)]

    # Unchanged → skipped; changed → resnapshot with the old copy kept as .prev.
    assert mirror_sources(home=str(archive_home))["providers"]["stubdb"]["unchanged"] == 1
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO t VALUES ('second')")
    conn.commit()
    conn.close()
    r3 = mirror_sources(home=str(archive_home))
    assert r3["providers"]["stubdb"]["copied"] == 1
    prev = dest_dir / "state.db.prev.gz"
    assert prev.exists()
    restored.write_bytes(gzip.decompress(prev.read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT count(*) FROM t").fetchone() == (1,)


def test_unsupported_watcher_shape_is_reported(archive_home, stub_watchers):
    class OddWatcher(SourceWatcher):
        @property
        def source_name(self) -> str:
            return "odd"

        def is_available(self) -> bool:
            return True

        def discover(self):  # pragma: no cover — never called by the mirror
            raise NotImplementedError

        def poll(self):  # pragma: no cover — never called by the mirror
            raise NotImplementedError

    stub_watchers([OddWatcher()])
    r = mirror_sources(home=str(archive_home))
    assert r["unsupported"] == ["odd"]
    assert r["providers"] == {}


def test_unreadable_file_costs_only_itself(archive_home, tmp_path, stub_watchers):
    store = tmp_path / "store"
    store.mkdir()
    good = store / "good.jsonl"
    good.write_text("ok\n", encoding="utf-8")
    bad = store / "bad.jsonl"
    bad.write_text("secret\n", encoding="utf-8")
    bad.chmod(0o000)
    stub_watchers([_file_watcher(store)])
    try:
        r = mirror_sources(home=str(archive_home))
    finally:
        bad.chmod(0o644)
    p = r["providers"]["stubprov"]
    assert p["copied"] == 1
    assert p["error_count"] == 1 and "bad.jsonl" in p["errors"][0]
    assert _mirrored(archive_home, "stubprov", good) == b"ok\n"
