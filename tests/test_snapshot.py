"""Snapshot: a frozen, self-contained archive home (``search_lab/snapshot.py``).

It copies the JSONL truth and materializes the index
beside it, producing an ordinary ``THREAD_ARCHIVE_HOME`` that never changes.
The properties that matter: the copy is a real searchable home, it is frozen
against later growth of the source (the whole point — deterministic search for a
regression gate), the vectors ride the copy without a re-embed, and the op
leaves the process pinned where it started.
"""

from __future__ import annotations

import json
import os

import pytest

from search_lab import snapshot as snap
from search_lab.snapshot import (
    SNAPSHOT_MANIFEST,
    corpus_fingerprint,
    read_snapshot_id,
)
from thread_archive import _api as api

from .helpers import corrupt_event_line, import_cc_session, one_thread_file


@pytest.fixture
def seeded(archive_home, tmp_path):
    """A real two-thread archive at ``archive_home``, checkpointed to truth."""
    import_cc_session(tmp_path, "widget")
    import_cc_session(tmp_path, "migration")
    api.checkpoint()
    return archive_home


def test_snapshot_is_a_self_contained_searchable_home(seeded, tmp_path):
    dest = tmp_path / "snap"
    res = snap.snapshot(str(dest), home=str(seeded))

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


def test_snapshot_records_its_build_as_a_load_run(seeded, tmp_path):
    """A snapshot of a real corpus runs for tens of minutes, so the build has to be
    inspectable while it happens and afterwards — as a run in the home being built,
    with the copy that opens it timed separately from the index it materializes."""
    from thread_archive._ops.load_runs import read_runs

    dest = tmp_path / "snap"
    snap.snapshot(str(dest), home=str(seeded))

    runs = read_runs(home=dest)  # newest first
    build = [r for r in runs if r.get("kind") == "snapshot"]
    assert build, f"no snapshot run recorded in {dest}: {runs}"
    rec = build[0]
    assert rec["status"] == "ok"
    phases = {p["name"]: p for p in rec["phases"]}
    assert set(phases) == {"copy", "index", "verify"}
    assert phases["copy"]["counts"]["files"] > 0
    assert phases["copy"]["counts"]["bytes"] > 0
    # reindex keeps its own, finer-grained record of the index phase.
    assert any(r.get("kind") == "reindex" for r in runs), runs


def test_snapshot_build_record_omits_the_verify_phase_when_not_verifying(seeded, tmp_path):
    from thread_archive._ops.load_runs import read_runs

    dest = tmp_path / "snap"
    snap.snapshot(str(dest), home=str(seeded), verify_result=False)

    rec = [r for r in read_runs(home=dest) if r.get("kind") == "snapshot"][0]
    assert [p["name"] for p in rec["phases"]] == ["copy", "index"]


def test_snapshot_is_frozen_against_source_growth(seeded, tmp_path):
    """A snapshot's corpus is fixed, so a thread added to the source after the
    snapshot never appears in it — search over the snapshot is deterministic
    regardless of live growth."""
    dest = tmp_path / "snap"
    res = snap.snapshot(str(dest), home=str(seeded))
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
    snap.snapshot(str(dest), home=str(seeded))
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
    res = snap.snapshot(str(dest), home=str(seeded))
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
    first = snap.snapshot(str(tmp_path / "a"), home=str(seeded))["manifest"]["snapshot_id"]
    # Re-snapshot the unchanged source → same id.
    again = snap.snapshot(str(tmp_path / "b"), home=str(seeded))["manifest"]["snapshot_id"]
    assert again == first

    # Grow the source, then snapshot → new id.
    import_cc_session(tmp_path, "grown")
    api.checkpoint()
    grown = snap.snapshot(str(tmp_path / "c"), home=str(seeded))["manifest"]["snapshot_id"]
    assert grown != first


def test_snapshot_refuses_nonempty_dest_without_force(seeded, tmp_path):
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "stranger.txt").write_text("not a snapshot")

    with pytest.raises(FileExistsError):
        snap.snapshot(str(dest), home=str(seeded))

    # --force snapshots into it anyway.
    res = snap.snapshot(str(dest), home=str(seeded), force=True)
    assert res["manifest"]["verify_ok"] is True


def test_re_snapshot_over_an_existing_snapshot_is_allowed(seeded, tmp_path):
    dest = tmp_path / "snap"
    snap.snapshot(str(dest), home=str(seeded))
    # A second snapshot into the same home needs no force (it holds our manifest).
    res = snap.snapshot(str(dest), home=str(seeded))
    assert res["manifest"]["verify_ok"] is True


def test_snapshot_can_skip_verification(seeded, tmp_path):
    """--no-verify (verify_result=False) builds the snapshot without the truth ==
    index check; the manifest records that neither verdict was taken."""
    dest = tmp_path / "snap"
    res = snap.snapshot(str(dest), home=str(seeded), verify_result=False)
    assert res["manifest"]["verify_ok"] is None
    assert res["manifest"]["source_verify_ok"] is None
    assert (dest / "index.db").is_file()


def test_stamp_makes_a_born_frozen_home_bindable_without_copying_it(seeded, tmp_path):
    """A corpus home built from a fixed dataset needs the identity, not a copy of
    itself: stamping writes the same manifest in place, and the id is the
    fingerprint of the home's own corpus."""
    before = sorted(p.name for p in seeded.iterdir())

    manifest = snap.stamp_snapshot(str(seeded))

    assert manifest["snapshot_id"] == corpus_fingerprint()
    assert read_snapshot_id(str(seeded)) == manifest["snapshot_id"]
    assert manifest["in_place"] is True
    assert manifest["counts"]["events"] == api.status(home=str(seeded))["events"]
    # Nothing was copied: the home gained the manifest and nothing else.
    assert sorted(p.name for p in seeded.iterdir()) == sorted([*before, SNAPSHOT_MANIFEST])


def test_stamp_leaves_the_process_pinned_where_it_started(seeded, tmp_path):
    """Stamping another home reads it — and must not leave the caller's engine
    pointed at it."""
    from thread_archive._store import active_dsn

    other = tmp_path / "corpus"
    snap.snapshot(str(other), home=str(seeded))  # a second, self-contained home
    api.open_archive(str(seeded))

    snap.stamp_snapshot(str(other))

    assert active_dsn() == f"sqlite:///{seeded / 'index.db'}"


def test_a_home_reads_back_the_id_its_cases_will_carry(seeded):
    """The point of stamping: an unstamped home has no id to bind a case file to,
    and a stamped one reads back the id its cases carry."""
    assert snap.read_snapshot_id() is None

    sid = snap.stamp_snapshot(str(seeded))["snapshot_id"]
    assert snap.read_snapshot_id() == sid


def test_re_stamping_tracks_a_rebuilt_corpus(seeded, tmp_path):
    """Re-stamping an unchanged corpus reproduces the id; a corpus that changed
    takes a new one, so golds mined against the old id read as stale."""
    first = snap.stamp_snapshot(str(seeded))["snapshot_id"]
    assert snap.stamp_snapshot(str(seeded))["snapshot_id"] == first

    import_cc_session(tmp_path, "grown")
    api.checkpoint()
    assert snap.stamp_snapshot(str(seeded))["snapshot_id"] != first


def test_stamp_refuses_the_live_archive(tmp_path, monkeypatch):
    """The live archive grows, so an id stamped over it would go on blessing golds
    the corpus has moved past. Only an explicit force gets through."""
    from thread_archive import _config as config

    # A real archive at the default location, reached the way the operator's is.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    live = config.default_home()
    live.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(config.ENV_HOME, str(live))
    import_cc_session(tmp_path, "widget")
    api.checkpoint()

    with pytest.raises(ValueError):
        snap.stamp_snapshot(str(live))
    assert read_snapshot_id(str(live)) is None

    assert snap.stamp_snapshot(str(live), force=True)["snapshot_id"]


def test_snapshot_leaves_the_process_pinned_to_the_source(seeded, tmp_path):
    """reindex/verify repoint the engine at the destination mid-build; the op
    must restore the source pin so a caller holding the live engine isn't
    silently reading the snapshot."""
    from thread_archive._store import active_dsn

    dest = tmp_path / "snap"
    snap.snapshot(str(dest), home=str(seeded))
    # The live engine DSN — not the env-resolved home — must point back at the
    # source index, not the destination the build last touched.
    assert active_dsn() == f"sqlite:///{seeded / 'index.db'}"


# ── the command line (`python search_lab/snapshot.py <dest>`) ─────────────────


def test_snapshot_cli_reports_a_failed_build(seeded, tmp_path, capsys):
    # A destination the process cannot create: the OS error is the operator's
    # answer, not a traceback.
    locked = tmp_path / "read-only-parent"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        assert snap.main([str(locked / "frozen"), "--home", str(seeded)]) == 1
    finally:
        os.chmod(locked, 0o700)
    assert "snapshot failed: [Errno 13] Permission denied" in capsys.readouterr().err


def test_snapshot_cli_that_fails_verification_warns_and_exits_1(seeded, tmp_path, capsys):
    """A snapshot is built to be trusted by later runs, so one built from damaged
    truth must not read as done — it says so and exits nonzero, over a real torn
    truth line rather than a claimed one."""
    corrupt_event_line(one_thread_file(seeded))

    assert snap.main([str(tmp_path / "frozen"), "--home", str(seeded)]) == 1
    cap = capsys.readouterr()
    assert "WARNING: the built snapshot failed verification" in cap.err
    assert "done:" in cap.out  # the dest is still named — it exists, it is just suspect


def test_snapshot_cli_builds_a_frozen_home(seeded, tmp_path, capsys):
    """The entry point copies truth, materializes the index, and reports — the dest
    is a real, verified home a harness can be pointed at."""
    dest = tmp_path / "frozen"
    assert snap.main([str(dest), "--home", str(seeded)]) == 0
    out = capsys.readouterr().out
    assert "done:" in out and str(dest) in out
    assert (dest / "truth" / "manifest.json").is_file()
    assert (dest / "index.db").is_file()
    assert (dest / "snapshot.json").is_file()

    # A non-empty stranger dir is refused without --force, taken with it.
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "x").write_text("nope")
    assert snap.main([str(stranger), "--home", str(seeded)]) == 1
    assert "snapshot refused" in capsys.readouterr().err
    assert snap.main([str(stranger), "--force", "--home", str(seeded)]) == 0
