"""Source-import watermarks: never lose an unimported tail.

* Watermarks survive reindex (carried from the previous index, or seeded from
  the ``import_state.jsonl`` checkpoint snapshot when the index is gone), so a
  rebuild never makes the importer adopt an active source at EOF and skip its
  unimported tail.
* The maintenance checkpoint (``snapshots=False``) keeps ``import_state.jsonl``
  fresh, and the watcher's import-state stamp moves on watermark-only changes,
  so the snapshot can't go stale when no events were created.
"""

from __future__ import annotations

import json

from sqlalchemy import select

import thread_archive as ta
from thread_archive.store import ImportState, get_session

from .helpers import append_jsonl, cc_assistant, cc_user, import_cc_session, write_jsonl

LATER_USER = {"type": "user", "uuid": "u2", "timestamp": "2026-01-01T10:01:00Z",
              "cwd": "/proj", "message": {"role": "user", "content": "the appended tail line"}}


def test_reindex_preserves_import_state_so_source_tails_still_import(archive_home, tmp_path) -> None:
    """The critical loss path: source grows → reindex (wipes nothing now) → the tail
    must still import instead of being adopted-away at EOF."""
    f = tmp_path / "sess.jsonl"
    write_jsonl(f, [cc_user(), cc_assistant()])
    ta.import_path(f)

    # The source grows before anyone polls it again.
    append_jsonl(f, [LATER_USER])

    ta.reindex()

    # The watermark survived the rebuild...
    with get_session() as s:
        state = s.execute(select(ImportState)).scalars().one()
        assert state.last_line_count == 2

    # ...so the next import picks up the appended tail rather than skipping it.
    res = ta.import_path(f)
    assert res.events_created > 0
    assert ta.search("appended tail")


def test_reindex_restores_import_state_from_checkpoint_snapshot(archive_home, tmp_path) -> None:
    """With the previous index gone entirely (``rm index.db``), the checkpoint's
    ``import_state.jsonl`` snapshot seeds the cursors."""
    f = tmp_path / "sess.jsonl"
    write_jsonl(f, [cc_user(), cc_assistant()])
    ta.import_path(f)
    ta.checkpoint()  # writes truth/import_state.jsonl
    assert (archive_home / "truth" / "import_state.jsonl").exists()

    append_jsonl(f, [LATER_USER])

    ta.close()
    (archive_home / "index.db").unlink()
    ta.reindex()

    with get_session() as s:
        state = s.execute(select(ImportState)).scalars().one()
        assert state.last_line_count == 2

    res = ta.import_path(f)
    assert res.events_created > 0
    assert ta.search("appended tail")


def test_maintenance_checkpoint_snapshots_import_state(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)  # import_path runs checkpoint(snapshots=False)
    snap = archive_home / "truth" / "import_state.jsonl"
    assert snap.exists()
    rows = [json.loads(ln) for ln in snap.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["source"] == "claude-code"


def test_import_state_stamp_moves_on_watermark_only_change(archive_home, tmp_path):
    from thread_archive.importers._state import upsert_import_state
    from thread_archive.watcher.daemon import Watcher

    import_cc_session(tmp_path)
    stamp1 = Watcher._import_state_stamp()
    assert stamp1 is not None

    # A watermark-only change: no events, no thread — just a cursor advance,
    # exactly what adopt_if_unwatermarked / an empty-content poll performs.
    with get_session() as s:
        upsert_import_state(
            s, source="codex", source_id="wm-only", thread_id=None,
            last_line_count=10, last_file_size=1000, last_message_uuid=None,
        )
        s.commit()
    stamp2 = Watcher._import_state_stamp()
    assert stamp2 != stamp1
