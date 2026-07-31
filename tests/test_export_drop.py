"""The export-drop watcher: account exports dropped into ``<home>/dumps/`` settle,
import, then get retained in ``imported/`` on a clean import or quarantined in
``failed/`` on any problem — the drop is never deleted.
"""

from __future__ import annotations

import json
import zipfile

from sqlalchemy import select

from thread_archive._store import Event, Thread, get_session, init_db
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


def _claude_batch_dir(parent, name, conv=_CONV, *, convs=None, raw=None):
    """A claude.ai batch-export directory. ``raw`` writes ``conversations.json``
    verbatim, for a bundle that is damaged rather than well-formed; ``users.json``
    is the sibling marker that classifies the bundle without parsing it."""
    d = parent / name
    d.mkdir(parents=True)
    (d / "conversations.json").write_text(
        raw if raw is not None else json.dumps(convs if convs is not None else [conv]),
        encoding="utf-8")
    (d / "users.json").write_text(json.dumps([{"uuid": "user-1"}]), encoding="utf-8")
    return d


def _claude_zip(parent, name, conv=_CONV):
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", json.dumps([conv]))
        zf.writestr("users.json", json.dumps([{"uuid": "user-1"}]))
    return path


def _chatgpt_batch_dir(parent, name):
    d = parent / name
    d.mkdir(parents=True)
    conv = {
        "id": "gpt-x", "title": "GPT", "create_time": 1767261600.0, "update_time": 1767261610.0,
        "current_node": "n1",
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["n1"], "message": None},
            "n1": {"id": "n1", "parent": "root", "children": [], "message": {
                "id": "n1", "author": {"role": "user"}, "create_time": 1767261600.0,
                "content": {"content_type": "text", "parts": ["hi gpt"]},
                "status": "finished_successfully", "metadata": {}}},
        },
    }
    (d / "conversations.json").write_text(json.dumps([conv]), encoding="utf-8")
    (d / "user.json").write_text(json.dumps({"id": "u1"}), encoding="utf-8")
    return d


def _claude_count():
    with get_session() as s:
        return len(s.execute(select(Thread).where(Thread.source == "claude")).scalars().all())


# ── settle + import + retain ─────────────────────────────────────────────────


def test_dropped_batch_dir_settles_imports_and_is_retained(archive_home) -> None:
    init_db()
    dumps = archive_home / "dumps"
    export_dir = _claude_batch_dir(dumps, "claude-export")

    w = ExportDropWatcher(dumps_dir=dumps)

    # First poll only records the settle signal — nothing imported yet.
    r1 = w.poll()
    assert r1.items_imported == 0 and r1.events_created == 0
    assert _claude_count() == 0
    assert export_dir.exists()

    # Second poll: signal unchanged → settled → import, then retain (never delete).
    r2 = w.poll()
    assert r2.items_imported == 1 and r2.events_created > 0
    assert _claude_count() == 1
    assert not export_dir.exists()
    # The original download is kept in imported/<kind>/, not destroyed — normalization
    # loss can never cost the user their export.
    assert (dumps / "imported" / "claude" / "claude-export").exists()

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude")).scalar_one()
        assert t.title == "Dropped Web Chat" and t.source_id == "conv-1"

    # The retained copy isn't rescanned; later polls are no-ops.
    assert w.poll().items_imported == 0


def test_dropped_zip_imports_and_is_retained(archive_home) -> None:
    init_db()
    dumps = archive_home / "dumps"
    z = _claude_zip(dumps, "claude-export.zip")

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()              # settle
    r = w.poll()          # import
    assert r.items_imported == 1
    assert not z.exists()
    assert (dumps / "imported" / "claude" / "claude-export.zip").exists()
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
    assert (dumps / "imported" / "claude" / "growing.zip").exists()


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


def test_drift_quarantine_is_not_scanned(archive_home) -> None:
    """``dumps/drift/`` shares the drop zone but holds preservation copies of a
    degraded source's raw store — the only copy left once the harness prunes.
    Scanning it would classify it as an unrecognized export and file it under
    ``failed/``, burying the quarantine and costing the drift snapshotter the
    prior generations it does incremental copies against."""
    init_db()
    dumps = archive_home / "dumps"
    gen = dumps / "drift" / "claude-code" / "20260101T000000Z"
    gen.mkdir(parents=True)
    (gen / "manifest.json").write_text('{"source": "claude-code"}', encoding="utf-8")

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()
    r = w.poll()

    assert r.sources_checked == 0 and not r.errors
    assert gen.exists()
    assert not (dumps / "failed").exists()


def test_failed_import_is_quarantined(archive_home) -> None:
    """A truncated download: the sibling markers still classify it as a claude.ai
    export, and the bulk importer raises on the half-written ``conversations.json``.
    The drop is quarantined, not lost."""
    init_db()
    dumps = archive_home / "dumps"
    _claude_batch_dir(dumps, "claude-export", raw='[{"uuid": "conv-1", "chat_mess')

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()              # settle
    r = w.poll()          # import raises → quarantine
    assert r.items_imported == 0 and r.errors

    assert not (dumps / "claude-export").exists()
    assert (dumps / "failed" / "claude-export").exists()


def test_zero_processed_import_is_quarantined_not_deleted(archive_home) -> None:
    """A recognized export whose import processes zero conversations — here every
    conversation carries no ``chat_messages`` at all, the shape a provider format
    change leaves — is quarantined. Deleting it would destroy the user's download
    over an import of nothing."""
    init_db()
    dumps = archive_home / "dumps"
    export_dir = _claude_batch_dir(dumps, "claude-export", convs=[
        {"uuid": "conv-1", "name": "Nothing Matched",
         "created_at": "2026-01-01T10:00:00Z", "updated_at": "2026-01-01T10:00:10Z"},
    ])

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()              # settle
    r = w.poll()          # import → processed=0 → quarantine
    assert r.items_imported == 0 and r.errors

    assert not export_dir.exists()
    assert (dumps / "failed" / "claude-export").exists()
    assert _claude_count() == 0


def test_partially_errored_import_is_quarantined_not_retained(archive_home) -> None:
    """An export holding one good conversation and one whose ``chat_messages`` is
    the wrong shape entirely: the bad one is preserved as a stub and counted as an
    error, so the bundle is quarantined for review — not retained as if clean, and
    above all not deleted. Clearing an export on "≥1 conversation processed" would
    delete the only copy of the one that errored."""
    init_db()
    dumps = archive_home / "dumps"
    export_dir = _claude_batch_dir(dumps, "claude-export", convs=[
        _CONV,
        {"uuid": "conv-broken", "name": "Wrong Shape",
         "created_at": "2026-01-01T11:00:00Z", "updated_at": "2026-01-01T11:00:10Z",
         "chat_messages": "not-a-list"},
    ])

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()              # settle
    r = w.poll()          # import → errored>0 → quarantine
    assert r.items_imported == 1 and r.errors

    assert not export_dir.exists()
    assert (dumps / "failed" / "claude-export").exists()
    assert not (dumps / "imported" / "claude-export").exists()
    # the errored conversation was preserved, not dropped on the floor
    with get_session() as s:
        kept = s.execute(select(Thread).where(
            Thread.source == "claude",
            Thread.source_id == "conv-broken:import-error")).scalars().all()
    assert len(kept) == 1


def test_dropped_chatgpt_zip_imports_as_chatgpt(archive_home) -> None:
    """The failure shape this guards: a ChatGPT export ZIP (which also carries a
    root ``conversations.json``) must import as ChatGPT — not classify as
    claude.ai, import nothing, and be deleted."""
    init_db()
    dumps = archive_home / "dumps"
    dumps.mkdir(parents=True, exist_ok=True)
    conv = {
        "id": "gconv-drop", "title": "Dropped GPT Chat",
        "create_time": 1767261600.0, "update_time": 1767261610.0,
        "current_node": "n1",
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["n1"], "message": None},
            "n1": {"id": "n1", "parent": "root", "children": [], "message": {
                "id": "n1", "author": {"role": "user"}, "create_time": 1767261600.0,
                "content": {"content_type": "text", "parts": ["dropped chatgpt message"]},
                "status": "finished_successfully", "metadata": {},
            }},
        },
    }
    path = dumps / "chatgpt-export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", json.dumps([conv]))
        zf.writestr("chat.html", "<html></html>")
        zf.writestr("user.json", json.dumps({"id": "u1"}))

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()              # settle
    r = w.poll()          # classify → chatgpt → import → retain
    assert r.items_imported == 1 and not r.errors
    assert not path.exists()
    assert (dumps / "imported" / "chatgpt" / "chatgpt-export.zip").exists()

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "chatgpt")).scalar_one()
        assert t.source_id == "gconv-drop"


# ── idempotency + wiring ─────────────────────────────────────────────────────


def test_redropping_same_content_imports_nothing_but_still_clears(archive_home) -> None:
    """Re-dropping already-imported content (any name) imports 0 conversations — the
    event-level dedup collapses everything already held — and the redundant drop is
    still cleared."""
    init_db()
    dumps = archive_home / "dumps"
    _claude_batch_dir(dumps, "export-a")

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()
    w.poll()
    assert _claude_count() == 1

    again = _claude_batch_dir(dumps, "export-b")  # same conv uuid, different dir name
    w.poll()              # settle
    r = w.poll()          # import → all unchanged, nothing new lands
    assert r.items_imported == 0
    assert _claude_count() == 1
    assert not again.exists()
    # The newer drop is retained; the older retained one of the same kind is pruned —
    # a full re-export supersedes it, so imported/claude/ holds exactly the latest.
    assert (dumps / "imported" / "claude" / "export-b").exists()
    assert not (dumps / "imported" / "claude" / "export-a").exists()


def test_redropped_export_merges_a_grown_conversation(archive_home) -> None:
    """A conversation that gained messages since the last export must gain exactly its
    new tail in the SAME thread on redrop — the preservation case a recurring account
    re-export exists for. Skipping it because the conversation id already exists would
    silently lose every message added to an old conversation."""
    init_db()
    dumps = archive_home / "dumps"
    _claude_batch_dir(dumps, "export-a")

    w = ExportDropWatcher(dumps_dir=dumps)
    w.poll()
    w.poll()
    assert _claude_count() == 1

    grown = {
        **_CONV,
        "updated_at": "2026-01-02T09:00:00Z",
        "chat_messages": _CONV["chat_messages"] + [
            {"uuid": "m3", "sender": "human", "text": "a follow-up after the first export",
             "content": [{"type": "text", "text": "a follow-up after the first export"}],
             "created_at": "2026-01-02T09:00:00Z"},
        ],
    }
    _claude_batch_dir(dumps, "export-b", conv=grown)
    w.poll()              # settle
    r = w.poll()          # import → the grown conversation merges its tail
    assert r.items_imported == 1
    assert r.events_created >= 1
    assert _claude_count() == 1, "the grown conversation forked a second thread"

    with get_session() as s:
        thread = s.execute(select(Thread).where(Thread.source == "claude")).scalars().one()
        contents = [e.payload.get("content") for e in s.execute(
            select(Event).where(Event.thread_id == thread.id)
        ).scalars()]
    assert "a follow-up after the first export" in contents
    assert contents.count("hello from a dropped export") == 1, "redrop duplicated old events"


def test_retention_is_bounded_to_latest_per_kind(archive_home) -> None:
    """imported/ keeps only the most-recent export per kind: a re-export prunes the
    prior one of the same kind (a full account export supersedes it), while a different
    provider's retained copy is left untouched — so retention stays bounded, not a pile."""
    init_db()
    dumps = archive_home / "dumps"
    w = ExportDropWatcher(dumps_dir=dumps)

    def settle_and_import():
        w.poll()  # record settle signal
        w.poll()  # signal unchanged → import

    _claude_batch_dir(dumps, "claude-1", conv={**_CONV, "uuid": "conv-A", "name": "A"})
    settle_and_import()
    _chatgpt_batch_dir(dumps, "chatgpt-1")
    settle_and_import()
    assert (dumps / "imported" / "claude" / "claude-1").exists()
    assert (dumps / "imported" / "chatgpt" / "chatgpt-1").exists()

    # A second claude export supersedes the first claude one — but not the chatgpt one.
    _claude_batch_dir(dumps, "claude-2", conv={**_CONV, "uuid": "conv-B", "name": "B"})
    settle_and_import()
    assert (dumps / "imported" / "claude" / "claude-2").exists()
    assert not (dumps / "imported" / "claude" / "claude-1").exists()
    assert (dumps / "imported" / "chatgpt" / "chatgpt-1").exists()


def test_default_watchers_includes_export_drop(archive_home) -> None:
    assert "export-drop" in [w.source_name for w in default_watchers()]


def test_constructing_creates_drop_zone_and_is_available(archive_home) -> None:
    """Constructed with no path it resolves <home>/dumps from the env-set home,
    creates it, and reports available."""
    w = ExportDropWatcher()
    assert w.dumps_dir == archive_home / "dumps"
    assert w.dumps_dir.exists()
    assert w.is_available()
