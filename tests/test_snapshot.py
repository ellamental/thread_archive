"""Snapshot: a frozen, self-contained archive home.

``thread_archive snapshot`` copies the JSONL truth and materializes the index
beside it, producing an ordinary ``THREAD_ARCHIVE_HOME`` that never changes.
The properties that matter: the copy is a real searchable home, it is frozen
against later growth of the source (the whole point — deterministic search for a
regression gate), the vectors ride the copy without a re-embed, and the op
leaves the process pinned where it started.
"""

from __future__ import annotations

import json

import pytest

from thread_archive import _api as api
from thread_archive._ops.snapshot import (
    SNAPSHOT_MANIFEST,
    corpus_fingerprint,
    read_snapshot_id,
)

from .helpers import import_cc_session


@pytest.fixture
def seeded(archive_home, tmp_path):
    """A real two-thread archive at ``archive_home``, checkpointed to truth."""
    import_cc_session(tmp_path, "widget")
    import_cc_session(tmp_path, "migration")
    api.checkpoint()
    return archive_home


def test_snapshot_is_a_self_contained_searchable_home(seeded, tmp_path):
    dest = tmp_path / "snap"
    res = api.snapshot(str(dest), home=str(seeded))

    # The layout is an ordinary home: copied truth + a materialized index.
    assert (dest / "truth" / "manifest.json").exists()
    assert (dest / "index.db").exists()
    assert (dest / SNAPSHOT_MANIFEST).exists()
    assert res["manifest"]["verify_ok"] is True

    # Opening the snapshot as a home and searching it works end to end.
    api.open_archive(str(dest))
    try:
        hits = api.search("hello from session widget", limit=5)
    finally:
        api.open_archive(str(seeded))
    assert hits, "the snapshot's own content must be searchable"


def test_snapshot_is_frozen_against_source_growth(seeded, tmp_path):
    """The isolation the ``until`` trick used to provide: a snapshot's corpus is
    fixed, so a thread added to the source after the snapshot never appears in
    it — search over the snapshot is deterministic regardless of live growth."""
    dest = tmp_path / "snap"
    res = api.snapshot(str(dest), home=str(seeded))
    snap_events = res["manifest"]["counts"]["events"]

    # Grow the source well past the snapshot. (snapshot() leaves the process
    # pinned to the source, so the import lands there; reading the snapshot with
    # status(home=dest) would repoint the engine, so it's read last.)
    import_cc_session(tmp_path, "added-after")
    api.checkpoint()
    assert api.status(home=str(seeded))["events"] > snap_events

    # The snapshot is unmoved.
    assert api.status(home=str(dest))["events"] == snap_events


def test_snapshot_manifest_records_provenance(seeded, tmp_path):
    dest = tmp_path / "snap"
    api.snapshot(str(dest), home=str(seeded))
    m = json.loads((dest / SNAPSHOT_MANIFEST).read_text())

    assert m["kind"] == "thread-archive-snapshot"
    assert m["source_home"] == str(seeded)
    assert m["archive_version"]
    assert m["created_at"]
    assert m["counts"]["events"] == api.status(home=str(dest))["events"]
    assert m["source_verify_ok"] is True


def test_snapshot_id_is_content_derived_and_binds_the_snapshot(seeded, tmp_path):
    """The snapshot_id is a content fingerprint: recorded in the manifest,
    readable back, and equal to the fingerprint recomputed over the snapshot."""
    dest = tmp_path / "snap"
    res = api.snapshot(str(dest), home=str(seeded))
    sid = res["manifest"]["snapshot_id"]

    assert sid and read_snapshot_id(str(dest)) == sid
    # It is the fingerprint of the snapshot's own corpus.
    api.open_archive(str(dest))
    try:
        assert corpus_fingerprint() == sid
    finally:
        api.open_archive(str(seeded))
    # A home that is not a snapshot has no id.
    assert read_snapshot_id(str(seeded)) is None


def test_snapshot_id_tracks_corpus_change_but_not_a_plain_re_snapshot(seeded, tmp_path):
    """A pure re-snapshot of an unchanged corpus reproduces the id (golds stay
    valid); a corpus that grew gets a new one (golds mined against the old one
    are detectably stale)."""
    first = api.snapshot(str(tmp_path / "a"), home=str(seeded))["manifest"]["snapshot_id"]
    # Re-snapshot the unchanged source → same id.
    again = api.snapshot(str(tmp_path / "b"), home=str(seeded))["manifest"]["snapshot_id"]
    assert again == first

    # Grow the source, then snapshot → new id.
    import_cc_session(tmp_path, "grown")
    api.checkpoint()
    grown = api.snapshot(str(tmp_path / "c"), home=str(seeded))["manifest"]["snapshot_id"]
    assert grown != first


def test_snapshot_refuses_nonempty_dest_without_force(seeded, tmp_path):
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "stranger.txt").write_text("not a snapshot")

    with pytest.raises(FileExistsError):
        api.snapshot(str(dest), home=str(seeded))

    # --force snapshots into it anyway.
    res = api.snapshot(str(dest), home=str(seeded), force=True)
    assert res["manifest"]["verify_ok"] is True


def test_re_snapshot_over_an_existing_snapshot_is_allowed(seeded, tmp_path):
    dest = tmp_path / "snap"
    api.snapshot(str(dest), home=str(seeded))
    # A second snapshot into the same home needs no force (it holds our manifest).
    res = api.snapshot(str(dest), home=str(seeded))
    assert res["manifest"]["verify_ok"] is True


def test_snapshot_can_skip_verification(seeded, tmp_path):
    """--no-verify (verify_result=False) builds the snapshot without the truth ==
    index check; the manifest records that neither verdict was taken."""
    dest = tmp_path / "snap"
    res = api.snapshot(str(dest), home=str(seeded), verify_result=False)
    assert res["manifest"]["verify_ok"] is None
    assert res["manifest"]["source_verify_ok"] is None
    assert (dest / "index.db").is_file()


def test_snapshot_leaves_the_process_pinned_to_the_source(seeded, tmp_path):
    """reindex/verify repoint the engine at the destination mid-build; the op
    must restore the source pin so a caller holding the live engine isn't
    silently reading the snapshot."""
    from thread_archive._store import active_dsn

    dest = tmp_path / "snap"
    api.snapshot(str(dest), home=str(seeded))
    # The live engine DSN — not the env-resolved home — must point back at the
    # source index, not the destination the build last touched.
    assert active_dsn() == f"sqlite:///{seeded / 'index.db'}"
