"""Result grouping: one row per thread, folds annotated instead of discarded.

- ``rank.collapse_same_anchor`` folds rows sharing one (thread_id, event_id)
- ``rank.group_by_thread`` folds same-thread repeats (``_thread_more``) and
  cross-thread duplicate content (``_dup_thread_ids``), preserving ranked order
- ``search`` groups the ranked shape by default; ``group='none'``, a
  ``thread_id`` scope, and the structural shapes stay ungrouped
- ``format_results`` renders the fold annotations
"""

from __future__ import annotations

import json

import pytest

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import search
from thread_archive._retrieval.format import format_results
from thread_archive._retrieval.rank import collapse_same_anchor, group_by_thread
from thread_archive._store import init_db


def _hit(eid, tid, content, ct="user"):
    return {
        "event_id": eid, "thread_id": tid, "thread_title": f"t{tid}",
        "event_type": "message", "content_type": ct,
        "snippet": content, "full_content": content, "occurred_at": None,
    }


def _write_cc(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _cc_user(uid, text, day, hhmm="10:00"):
    return {"type": "user", "uuid": uid, "timestamp": f"2026-01-0{day}T{hhmm}:00Z",
            "sessionId": "s", "message": {"role": "user", "content": text}}


def _seed(archive_home):
    """Thread A: three matching user turns; thread B: one; threads C+D: the same
    prompt verbatim (a fork / fleet copy)."""
    init_db()
    a = archive_home / "a.jsonl"
    _write_cc(a, [_cc_user("a1", "the tachyon condenser leaks", 1),
                  _cc_user("a2", "tachyon flow reversed after the patch", 1, "11:00"),
                  _cc_user("a3", "third tachyon reading is stable", 1, "12:00")])
    import_session_incremental(a, "proj:a")

    b = archive_home / "b.jsonl"
    _write_cc(b, [_cc_user("b1", "does the tachyon budget cover q3", 2)])
    import_session_incremental(b, "proj:b")

    c = archive_home / "c.jsonl"
    _write_cc(c, [_cc_user("c1", "calibrate the flux capacitor against drift", 3)])
    import_session_incremental(c, "proj:c")

    d = archive_home / "d.jsonl"
    _write_cc(d, [_cc_user("d1", "calibrate the flux capacitor against drift", 4)])
    import_session_incremental(d, "proj:d")


def test_collapse_same_anchor_keeps_better_placed_row() -> None:
    hits = [_hit(10, 1, "Tachyon condenser notes", ct="title"),
            _hit(10, 1, "the tachyon condenser leaks"),
            _hit(11, 1, "another event")]
    out = collapse_same_anchor(hits)
    assert [(h["event_id"], h["content_type"]) for h in out] == [(10, "title"), (11, "user")]


def test_group_by_thread_folds_same_thread_repeats() -> None:
    hits = [_hit(1, 100, "best hit"), _hit(2, 200, "other thread"),
            _hit(3, 100, "second hit"), _hit(4, 100, "third hit")]
    out = group_by_thread(hits)
    assert [h["thread_id"] for h in out] == [100, 200]
    assert out[0]["_thread_more"] == 2
    assert "_thread_more" not in out[1]


def test_group_by_thread_folds_cross_thread_duplicate_content() -> None:
    hits = [_hit(1, 100, "calibrate the flux capacitor"),
            _hit(2, 200, "calibrate the  flux CAPACITOR"),  # same after normalization
            _hit(3, 300, "unrelated"),
            _hit(4, 200, "a distinct later hit")]
    out = group_by_thread(hits)
    assert [h["thread_id"] for h in out] == [100, 300, 200]
    assert out[0]["_dup_thread_ids"] == [200]
    # the folded thread surfaced later on its own distinct content
    assert out[2]["event_id"] == 4


def test_search_groups_one_row_per_thread(archive_home) -> None:
    _seed(archive_home)
    rows = search("tachyon")
    by_thread = [r["thread_id"] for r in rows]
    assert len(by_thread) == len(set(by_thread)) == 2
    folded = next(r for r in rows if r.get("_thread_more"))
    assert folded["_thread_more"] == 2

    ungrouped = search("tachyon", group="none")
    assert len(ungrouped) == 4
    assert not any(r.get("_thread_more") for r in ungrouped)


def test_search_folds_forked_duplicate_content(archive_home) -> None:
    _seed(archive_home)
    rows = search("flux capacitor")
    assert len(rows) == 1
    assert len(rows[0]["_dup_thread_ids"]) == 1


def test_thread_scope_and_structural_shapes_stay_ungrouped(archive_home) -> None:
    _seed(archive_home)
    tid = search("tachyon", group="none")[0]["thread_id"]
    scoped = search("tachyon", thread_id=tid)
    assert len(scoped) == 3 and not any(r.get("_thread_more") for r in scoped)

    oldest = search("tachyon", sort="oldest")
    assert len(oldest) == 4  # chronological first-mention scan, never grouped


def test_search_rejects_unknown_group() -> None:
    with pytest.raises(ValueError):
        search("anything", group="bogus")


def test_format_renders_fold_annotations() -> None:
    kept = _hit(1, 100, "calibrate the flux capacitor")
    kept["_thread_more"] = 2
    kept["_dup_thread_ids"] = [200, 300, 400, 500]
    rendered = format_results([kept], "flux")
    assert "+2 more in thread" in rendered
    assert "= same content in thread(s) 200, 300, 400, +1 more" in rendered
    assert "grouped: one row per thread" in rendered
