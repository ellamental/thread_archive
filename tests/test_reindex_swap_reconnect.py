"""The ingest lock reconnects to a swapped index.db on acquire.

``reindex`` publishes by renaming a fresh build over ``index.db``; a writer's
pooled connections keep the *old inode* open. The writer-side guard cannot live
in ``open_archive``: a one-shot writer opens the archive, then *blocks* on
``shared_ingest_lock()`` for the whole duration of a running reindex — the swap
happens during that wait, after any pre-wait identity check. So the check runs
on lock *acquire* (``reconnect_if_swapped``), where the shared hold guarantees
the observed identity stays valid until release.

These tests simulate the post-wait world deterministically: bind the engine,
swap ``index.db`` (same path, new inode — exactly what ``reindex`` publishes),
then take the lock and write. Without the acquire-side reconnect, the import
reports success, the truth is durable, and a fresh read of ``index.db`` shows
none of it — the commit landed in the unlinked pre-swap inode.
"""

from __future__ import annotations

import os
import shutil
import sqlite3

from sqlalchemy import text

from thread_archive import _api as ta
from thread_archive._importers import import_session_incremental
from thread_archive._store import get_session
from thread_archive._truth import checkpoint, shared_ingest_lock
from thread_archive._truth.jsonl_log import try_shared_ingest_lock

from .helpers import cc_assistant, cc_user, import_cc_session, write_jsonl


def _swap_index(home, *, marker: str | None = None) -> None:
    """Simulate a reindex publish: copy index.db to a new inode and rename it
    over the live file (dropping sidecars, as reindex does). ``marker`` adds a
    sentinel table only the post-swap file has."""
    idx = home / "index.db"
    # Fold the WAL in so the copy is complete on its own.
    con = sqlite3.connect(idx)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    tmp = home / "index.db.rebuild"
    shutil.copy(idx, tmp)
    if marker:
        con = sqlite3.connect(tmp)
        con.execute(f"CREATE TABLE {marker} (x)")
        con.commit()
        con.close()
    os.replace(tmp, idx)
    for suffix in ("-wal", "-shm"):
        sidecar = home / f"index.db{suffix}"
        sidecar.unlink(missing_ok=True)


def _bind_engine_pre_swap() -> None:
    """Open the archive and force a pooled connection onto the current inode."""
    ta.open_archive()
    with get_session() as s:
        s.execute(text("SELECT count(*) FROM events")).scalar()


def test_import_under_lock_after_swap_lands_in_live_index(archive_home, tmp_path):
    import_cc_session(tmp_path, "seed")
    _bind_engine_pre_swap()
    _swap_index(archive_home)

    f = tmp_path / "late.jsonl"
    write_jsonl(f, [cc_user("late", "RACETOKEN payload"), cc_assistant("late")])
    with shared_ingest_lock():
        result = import_session_incremental(f, "late")
        checkpoint(snapshots=False)
    assert result.events_created > 0

    # The bar: a FRESH OS-level read of index.db — not the writer's own engine,
    # which sees its own inode regardless of where that inode is linked.
    con = sqlite3.connect(archive_home / "index.db")
    n = con.execute(
        "SELECT count(*) FROM events WHERE payload LIKE '%RACETOKEN%'"
    ).fetchone()[0]
    con.close()
    assert n > 0, "import committed into the orphaned pre-swap inode"


def test_try_lock_acquire_reconnects_after_swap(archive_home, tmp_path):
    import_cc_session(tmp_path, "seed")
    _bind_engine_pre_swap()
    _swap_index(archive_home, marker="swap_marker")

    # The daemon path: try_shared_ingest_lock acquired must observe the new file.
    with try_shared_ingest_lock() as acquired:
        assert acquired
        with get_session() as s:
            found = s.execute(
                text("SELECT name FROM sqlite_master WHERE name='swap_marker'")
            ).scalar()
        assert found == "swap_marker", "engine still reads the pre-swap inode"


def test_checkpoint_is_self_locking_after_swap(archive_home, tmp_path):
    """A bare checkpoint() — the pre-backup call — gets the reconnect too."""
    import_cc_session(tmp_path, "seed")
    _bind_engine_pre_swap()
    _swap_index(archive_home, marker="swap_marker")

    checkpoint()  # must not read thread metadata off the orphaned inode

    with get_session() as s:
        found = s.execute(
            text("SELECT name FROM sqlite_master WHERE name='swap_marker'")
        ).scalar()
    assert found == "swap_marker"
