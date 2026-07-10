"""Tests for the sixth-pass integrity hardening — both-layouts twins.

A thread can transiently exist at two shard depths (a both-layouts backup
mirror, a crash mid-rebalance). This pass makes every consumer of the truth
directory treat those twins correctly:

1. ``scan_truth_counts`` collapses twins across files: a thread counts once per
   distinct stem, duplicated lines count as superseded — so verify against a
   both-layouts directory matches what a reindex of it materializes.
2. Reindex loads the *canonical* (manifest-depth) file last, so its records win
   last-wins over a stale twin — thread metadata can't silently regress on a
   restore.
3. The synthesized-minimal thread record never clobbers a real record supplied
   by a twin of the same thread.
4. The backup mirror deletes provably-superseded re-homed twins regardless of
   the deletion safety bound (a rebalance moves every file at once, always
   exceeding it), while everything else stays capped.
5. ``archive backup`` exits nonzero when deletions were skipped, so the
   scheduled wrapper's failure notification fires instead of the condition
   persisting silently.
6. Manifest writers (checkpoint, truth re-emit) re-read before writing and
   mutate only their own keys, so a foreign key (verify's hashes baseline)
   written concurrently survives.
"""

from __future__ import annotations

import json
import shutil

from sqlalchemy import text

import thread_archive as ta
from thread_archive.store import get_session
from thread_archive.truth import jsonl_log

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello integrity six"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back once more"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _import_session(tmp_path, name="sess"):
    f = tmp_path / f"{name}.jsonl"
    # Distinct content per session name, or CC continuation detection merges the
    # second session into the first thread.
    user = {**USER, "uuid": f"u-{name}",
            "message": {"role": "user", "content": f"hello integrity six {name}"}}
    asst = {**ASSISTANT, "uuid": f"a-{name}"}
    _write_cc(f, [user, asst])
    ta.import_path(f)


def _one_thread_file(archive_home):
    return next((archive_home / "truth" / "threads").rglob("*.jsonl"))


def _make_stale_flat_twin(archive_home, *, fresh_title="fresh title", drop_canonical_meta=False):
    """Move the (single) thread's truth file to its depth-1 home, set the
    manifest's shard depth, and leave the original flat file behind as a stale
    twin. The canonical copy gains a newer thread record with ``fresh_title``
    (or loses its record entirely with ``drop_canonical_meta``). Returns
    ``(thread_id, flat_path, canonical_path)``."""
    d = archive_home / "truth"
    flat = _one_thread_file(archive_home)
    tid = int(flat.stem)
    jsonl_log.reset_handles()

    canonical = jsonl_log._thread_file(d, tid, 1)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(flat, canonical)

    lines = canonical.read_text(encoding="utf-8").splitlines()
    records = [json.loads(ln) for ln in lines]
    meta = next(r for r in records if r.get("type") == "thread")
    if drop_canonical_meta:
        kept = [ln for ln, r in zip(lines, records) if r.get("type") != "thread"]
        canonical.write_text("\n".join(kept) + "\n", encoding="utf-8")
    else:
        fresh = {**meta, "title": fresh_title}
        with open(canonical, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(fresh) + "\n")

    m = jsonl_log._read_manifest(d)
    m["shard_depth"] = 1
    jsonl_log._write_manifest(d, m)
    return tid, flat, canonical


# ── 1. scan collapses cross-file twins ───────────────────────────────────────
def test_scan_truth_counts_collapses_cross_file_twins(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    with get_session() as s:
        idx_events = s.execute(text("SELECT count(*) FROM events")).scalar()

    baseline = jsonl_log.scan_truth_counts()
    assert baseline["threads"] == 1
    assert baseline["events_effective"] == idx_events
    assert baseline["duplicate_id_lines"] == 0

    _make_stale_flat_twin(archive_home)

    scan = jsonl_log.scan_truth_counts()
    assert scan["threads"] == 1, "a thread at two depths is still one thread"
    assert scan["events_effective"] == idx_events, "twin lines are superseded, not drift"
    assert scan["duplicate_id_lines"] == idx_events
    assert scan["events"] == 2 * idx_events

    # End to end: shallow verify stays clean over the both-layouts directory.
    assert ta.verify()["ok"] is True


# ── 2. reindex: the canonical-depth record wins over a stale twin ────────────
def test_reindex_prefers_canonical_depth_thread_record(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    tid, _flat, _canonical = _make_stale_flat_twin(archive_home, fresh_title="fresh title")

    ta.reindex()
    with get_session() as s:
        n, title = s.execute(
            text("SELECT count(*), max(title) FROM threads")
        ).one()
    assert n == 1
    assert title == "fresh title", "the stale flat twin must not shadow the canonical record"


# ── 3. the synthesized stub never clobbers a twin's real record ──────────────
def test_reindex_synthesized_stub_does_not_clobber_twin_record(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    tid, _flat, _canonical = _make_stale_flat_twin(archive_home, drop_canonical_meta=True)

    ta.reindex()
    with get_session() as s:
        name = s.execute(
            text("SELECT name FROM threads WHERE id = :t"), {"t": tid}
        ).scalar()
    assert name != f"thread:{tid}", "the flat twin's real record must survive"


# ── 4. mirror: re-homed twins are exempt from the deletion cap ───────────────
def test_mirror_deletes_rehomed_twins_beyond_cap(archive_home, tmp_path, monkeypatch) -> None:
    from thread_archive import api

    # Cap at zero: every non-twin deletion is skipped, so anything that DOES get
    # deleted went through the twin exemption.
    monkeypatch.setattr(api, "_MIRROR_DELETE_FLOOR", 0)
    monkeypatch.setattr(api, "_MIRROR_DELETE_MAX_FRACTION", 0.0)

    for name in ("one", "two"):
        _import_session(tmp_path, name=name)
    dest = tmp_path / "dest"
    assert ta.backup(str(dest))["mirror_complete"] is True
    flat_rels = sorted(
        p.relative_to(archive_home / "truth")
        for p in (archive_home / "truth" / "threads").rglob("*.jsonl")
    )
    assert all(len(rel.parts) == 2 for rel in flat_rels), "starts flat"

    # Drive the real rebalance: shrink the flat threshold and checkpoint.
    monkeypatch.setattr(jsonl_log, "_FLAT_MAX", 1)
    jsonl_log.checkpoint()
    assert jsonl_log._shard_depth(archive_home / "truth") == 1
    assert not any((archive_home / "truth" / rel).exists() for rel in flat_rels)

    # A stale non-twin at the destination stays protected by the zero cap.
    stale = dest / "threads" / "999999.jsonl"
    stale.write_text('{"type": "thread", "id": 999999, "name": "stale"}\n', encoding="utf-8")

    res = ta.backup(str(dest))
    assert res["rehomed_twins_deleted"] == len(flat_rels)
    for rel in flat_rels:
        assert not (dest / rel).exists(), f"stale twin {rel} must be gone from the backup"
    assert stale.exists(), "non-twin deletions stay capped"
    assert res["deletions_skipped"] == 1
    assert res["mirror_complete"] is True


# ── 5. skipped deletions fail the backup exit code ───────────────────────────
def test_backup_cli_fails_on_skipped_deletions(archive_home, tmp_path, monkeypatch) -> None:
    from thread_archive import api
    from thread_archive.cli import main

    monkeypatch.setattr(api, "_MIRROR_DELETE_FLOOR", 0)
    monkeypatch.setattr(api, "_MIRROR_DELETE_MAX_FRACTION", 0.0)

    _import_session(tmp_path)
    dest = tmp_path / "dest"
    assert main(["backup", str(dest)]) == 0

    stale = dest / "threads" / "999999.jsonl"
    stale.write_text('{"type": "thread", "id": 999999, "name": "stale"}\n', encoding="utf-8")
    assert main(["backup", str(dest)]) == 1, "a skipped deletion must fail the run"
    assert stale.exists()


# ── 6. manifest writers preserve foreign keys ────────────────────────────────
def test_checkpoint_preserves_foreign_manifest_keys(archive_home, tmp_path, monkeypatch) -> None:
    _import_session(tmp_path)
    d = archive_home / "truth"
    jsonl_log.checkpoint()

    baseline = {"at": "2026-01-01T00:00:00+00:00", "truth_mismatched": 0, "index_mismatched": 0}
    orig = jsonl_log._checkpoint_changed_threads

    def sneaky(dd, depth, last_iso):
        # A verify --hashes run landing mid-checkpoint, after the manifest read.
        m = jsonl_log._read_manifest(dd)
        m["hashes_baseline"] = baseline
        jsonl_log._write_manifest(dd, m)
        return orig(dd, depth, last_iso)

    monkeypatch.setattr(jsonl_log, "_checkpoint_changed_threads", sneaky)
    jsonl_log.checkpoint()
    assert jsonl_log._read_manifest(d).get("hashes_baseline") == baseline


def test_truth_reemit_preserves_foreign_manifest_keys(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    d = archive_home / "truth"
    jsonl_log.checkpoint()

    baseline = {"at": "2026-01-01T00:00:00+00:00", "truth_mismatched": 0, "index_mismatched": 0}
    m = jsonl_log._read_manifest(d)
    m["hashes_baseline"] = baseline
    jsonl_log._write_manifest(d, m)

    jsonl_log.rebuild_truth_from_store()
    m = jsonl_log._read_manifest(d)
    assert m.get("hashes_baseline") == baseline
    assert "shard_depth" in m and "last_checkpoint_at" in m
