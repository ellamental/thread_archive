"""The export-drop watcher: account exports dropped into ``<home>/dumps/`` settle,
import, then get deleted on success or quarantined on failure.
"""

from __future__ import annotations

import json
import zipfile

from sqlalchemy import select

from thread_archive._store import Thread, get_session, init_db
from thread_archive._watcher import ExportDropWatcher
from thread_archive._watcher.sources import default_watchers

# A minimal but valid claude.ai conversation (batch/export shape).
_CONV = {
    "uuid": "conv-1",
    "name": "Dropped Web Chat",
    "created_at": "2026-01-01T10:00:00Z",
    "updated_at": "2026-01-01T10:00:10Z",
    "chat_messages": [
        {"uuid": "m1", "sender": "human", "text": "hello from a dropped export",
         "content": [{"type": "text", "text": "hello from a dropped export"}],
         "created_at": "2026-01-01T10:00:00Z"},
        {"uuid": "m2", "sender": "assistant", "text": "hi from claude",
         "content": [{"type": "text", "text": "hi from claude"}],
         "created_at": "2026-01-01T10:00:05Z"},
    ],
}


def _claude_batch_dir(parent, name, conv=_CONV):
    d = parent / name
    d.mkdir(parents=True)
    (d / "conversations.json").write_text(json.dumps([conv]), encoding="utf-8")
    (d / "users.json").write_text(json.dumps([{"uuid": "user-1"}]), encoding="utf-8")
    return d


def _claude_zip(parent, name, conv=_CONV):
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", json.dumps([conv]))
        zf.writestr("users.json", json.dumps([{"uuid": "user-1"}]))
    return path


def _claude_count():
    with get_session() as s:
        return len(s.execute(select(Thread).where(Thread.source == "claude")).scalars().all())


# ── settle + import + delete ─────────────────────────────────────────────────


def test_dropped_batch_dir_settles_imports_and_is_deleted(archive_home) -> None:
    init_db()
    dumps = archive_home / "dumps"
    export_dir = _claude_batch_dir(dumps, "claude-export")

    w = ExportDropWatcher(dumps_dir=dumps)

    # First poll only records the settle signal — nothing imported yet.
    r1 = w.poll()
    assert r1.items_imported == 0 and r1.events_created == 0
    assert _claude_count() == 0
    assert export_dir.exists()

    # Second poll: signal unchanged → settled → import, then delete the drop.
    r2 = w.poll()
    assert r2.items_imported == 1 and r2.events_created > 0
    assert _claude_count() == 1
    assert not export_dir.exists()

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude")).scalar_one()
        assert t.title == "Dropped Web Chat" and t.source_id == "conv-1"

    # Empty drop zone → later polls are no-ops.
    assert w.poll().items_imported == 0


def test_dropped_zip_imports_and_is_deleted(archive_home) -> None:
    init_db()
    dumps = archive_home / "dumps"
    z = _claude_zip(dumps, "claude-export.zip")

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()              # settle
    r = w.poll()          # import
    assert r.items_imported == 1
    assert not z.exists()
    assert _claude_count() == 1


def test_still_copying_file_is_not_imported_until_stable(archive_home) -> None:
    """A drop whose bytes are still changing between polls is deferred until it settles."""
    init_db()
    dumps = archive_home / "dumps"
    z = _claude_zip(dumps, "growing.zip")

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()  # record signal A

    # Rewrite with more content → signal changes; this poll must NOT import.
    big = dict(_CONV)
    big["chat_messages"] = _CONV["chat_messages"] * 4
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps([big]))
        zf.writestr("users.json", json.dumps([{"uuid": "user-1"}]))
    r_mid = w.poll()  # signal B != A → defer, record B
    assert r_mid.items_imported == 0
    assert _claude_count() == 0
    assert z.exists()

    # Now stable across two polls → imported.
    r_done = w.poll()  # signal B == B → settled
    assert r_done.items_imported == 1
    assert not z.exists()


# ── failure handling: quarantine, never delete ───────────────────────────────


def test_unrecognized_zip_is_quarantined_not_deleted(archive_home) -> None:
    init_db()
    dumps = archive_home / "dumps"
    junk = dumps / "not-an-export.zip"
    dumps.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(junk, "w") as zf:
        zf.writestr("README.txt", "this is not a conversation export")

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()              # settle
    r = w.poll()          # classify → None → quarantine
    assert r.items_imported == 0 and r.errors

    assert not junk.exists()
    assert (dumps / "failed" / "not-an-export.zip").exists()
    assert _claude_count() == 0

    # The quarantine subdir is skipped on subsequent scans — no re-processing.
    r2 = w.poll()
    assert r2.sources_checked == 0 and not r2.errors


def test_failed_import_is_quarantined(archive_home, monkeypatch) -> None:
    """If the bulk importer raises mid-export, the drop is quarantined, not lost."""
    init_db()
    dumps = archive_home / "dumps"
    _claude_batch_dir(dumps, "claude-export")

    from thread_archive._watcher import export_drop

    def _boom(path, **kw):
        raise RuntimeError("corrupt bundle")

    monkeypatch.setattr(export_drop, "import_claude_ai_export", _boom)

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()              # settle
    r = w.poll()          # import raises → quarantine
    assert r.items_imported == 0 and r.errors

    assert not (dumps / "claude-export").exists()
    assert (dumps / "failed" / "claude-export").exists()


# ── idempotency + wiring ─────────────────────────────────────────────────────


def test_redropping_same_content_imports_nothing_but_still_clears(archive_home) -> None:
    """Re-dropping already-imported content (any name) imports 0 conversations — the
    importer dedups by source id — and the redundant drop is still cleared."""
    init_db()
    dumps = archive_home / "dumps"
    _claude_batch_dir(dumps, "export-a")

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()
    w.poll()
    assert _claude_count() == 1

    again = _claude_batch_dir(dumps, "export-b")  # same conv uuid, different dir name
    w.poll()              # settle
    r = w.poll()          # import → all skipped (already present)
    assert r.items_imported == 0
    assert _claude_count() == 1
    assert not again.exists()


def test_default_watchers_includes_export_drop(archive_home) -> None:
    assert "export-drop" in [w.source_name for w in default_watchers()]


def test_constructing_creates_drop_zone_and_is_available(archive_home) -> None:
    """Constructed with no path it resolves <home>/dumps from the env-set home,
    creates it, and reports available."""
    w = ExportDropWatcher()
    assert w.dumps_dir == archive_home / "dumps"
    assert w.dumps_dir.exists()
    assert w.is_available()
