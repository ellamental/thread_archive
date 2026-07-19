"""Both-layouts twins: a thread transiently existing at two shard depths.

A both-layouts backup mirror or a crash mid-rebalance can leave the same thread
file at two depths. Every consumer of the truth directory must treat those
twins correctly:

* ``scan_truth_counts`` collapses twins across files: a thread counts once per
  distinct stem, duplicated lines count as superseded — so verify against a
  both-layouts directory matches what a reindex of it materializes;
* reindex loads the *canonical* (manifest-depth) file last, so its records win
  last-wins over a stale twin — thread metadata can't silently regress on a
  restore;
* the synthesized-minimal thread record never clobbers a real record supplied
  by a twin of the same thread.
"""

from __future__ import annotations

import json
import shutil

from sqlalchemy import text

from thread_archive import _api as ta
from thread_archive._store import get_session
from thread_archive._truth import jsonl_log

from .helpers import import_cc_session, one_thread_file


def _make_stale_flat_twin(archive_home, *, fresh_title="fresh title", drop_canonical_meta=False):
    """Move the (single) thread's truth file to its depth-1 home, set the
    manifest's shard depth, and leave the original flat file behind as a stale
    twin. The canonical copy gains a newer thread record with ``fresh_title``
    (or loses its record entirely with ``drop_canonical_meta``). Returns
    ``(thread_id, flat_path, canonical_path)``."""
    d = archive_home / "truth"
    flat = one_thread_file(archive_home)
    tid = flat.stem
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


def test_scan_truth_counts_collapses_cross_file_twins(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
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


def test_reindex_prefers_canonical_depth_thread_record(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    tid, _flat, _canonical = _make_stale_flat_twin(archive_home, fresh_title="fresh title")

    ta.reindex()
    with get_session() as s:
        n, title = s.execute(
            text("SELECT count(*), max(title) FROM threads")
        ).one()
    assert n == 1
    assert title == "fresh title", "the stale flat twin must not shadow the canonical record"


def test_reindex_synthesized_stub_does_not_clobber_twin_record(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    tid, _flat, _canonical = _make_stale_flat_twin(archive_home, drop_canonical_meta=True)

    ta.reindex()
    with get_session() as s:
        name = s.execute(
            text("SELECT name FROM threads WHERE id = :t"), {"t": tid}
        ).scalar()
    assert name != f"thread:{tid}", "the flat twin's real record must survive"
