"""Result grouping: one row per thread, folds annotated instead of discarded.

- ``rank.collapse_same_anchor`` folds rows sharing one (thread_id, event_id)
- ``rank.group_by_thread`` folds same-thread repeats (``_thread_more``) and
  cross-thread duplicate content (``_dup_thread_ids``), preserving ranked order
- ``rank.fold_duplicate_threads`` folds only the cross-thread duplicates
- ``search`` groups the ranked shape by default; ``group='none'``, a
  ``thread_id`` scope, and the structural shapes stay ungrouped, and
  ``group='dup'`` keeps per-thread hits while folding cross-thread duplicates
- ``format_results`` renders the fold annotations

and the thread-granular **list** shape, which turns a keyword search into what
an empty query already returns:

- ``group='nested'`` — every hit, clustered under its thread in event order
  (``rank.cluster_by_thread``), capped by thread and per thread. It enumerates
  every matched thread (no cross-thread duplicate fold) and outranks the
  shape-based suppressions, since asking for it is explicit
- ``group='browse'`` is the default thread shape under an older name, accepted
  so a caller carrying it keeps working
"""

from __future__ import annotations

import json

import pytest

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import search
from thread_archive._retrieval.format import format_results, top_hit
from thread_archive._retrieval.rank import (
    cluster_by_thread,
    collapse_same_anchor,
    fold_duplicate_threads,
    group_by_thread,
)
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


def test_fold_duplicate_threads_keeps_a_threads_own_repeats() -> None:
    hits = [_hit(1, 100, "best hit"), _hit(2, 200, "other thread"),
            _hit(3, 100, "second hit")]
    out = fold_duplicate_threads(hits)
    assert [h["event_id"] for h in out] == [1, 2, 3]
    assert not any("_thread_more" in h for h in out)


def test_fold_duplicate_threads_folds_cross_thread_duplicate_content() -> None:
    hits = [_hit(1, 100, "calibrate the flux capacitor"),
            _hit(2, 200, "calibrate the  flux CAPACITOR"),  # same after normalization
            _hit(3, 300, "unrelated"),
            _hit(4, 200, "a distinct later hit")]
    out = fold_duplicate_threads(hits)
    assert [h["event_id"] for h in out] == [1, 3, 4]
    assert out[0]["_dup_thread_ids"] == [200]


def test_fold_duplicate_threads_keeps_identical_text_within_one_thread() -> None:
    """Two events in one thread carrying the same line are two real occurrences —
    only the *cross*-thread twin is redundancy."""
    hits = [_hit(1, 100, "same line"), _hit(2, 100, "same line"),
            _hit(3, 200, "same line")]
    out = fold_duplicate_threads(hits)
    assert [h["event_id"] for h in out] == [1, 2]
    assert out[0]["_dup_thread_ids"] == [200]


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


def test_search_marks_forked_duplicate_content_without_hiding_it(archive_home) -> None:
    """A forked prompt produces two threads carrying identical text. They are two
    conversations, so both keep a row and the duplicate relation is a *mark* —
    hiding one would answer "which threads mention this" with 1 when it is 2."""
    _seed(archive_home)
    rows = search("flux capacitor")
    assert len(rows) == 2
    assert len(rows[0]["_dup_thread_ids"]) == 1


def test_collapse_folds_forked_duplicate_content(archive_home) -> None:
    """``collapse=True`` is the opt-in for a caller spending result slots on
    distinct content: the fork folds into one annotated row."""
    _seed(archive_home)
    rows = search("flux capacitor", collapse=True)
    assert len(rows) == 1
    assert len(rows[0]["_dup_thread_ids"]) == 1


def test_search_group_dup_folds_forks_but_keeps_a_threads_own_hits(archive_home) -> None:
    """The viewer's shape: thread A's three tachyon turns all stay, while the
    C/D fork of one prompt collapses to a single annotated row."""
    _seed(archive_home)
    rows = search("tachyon", group="dup")
    assert len(rows) == 4  # every hit survives — nothing here is a cross-thread twin
    assert not any(r.get("_thread_more") for r in rows)

    forked = search("flux capacitor", group="dup")
    assert len(forked) == 1
    assert len(forked[0]["_dup_thread_ids"]) == 1


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


# ── the thread-granular list shapes ───────────────────────────────────────────


def test_group_by_thread_without_dup_fold_marks_but_keeps_every_thread() -> None:
    """Without the fold every matched thread keeps its row, and the near-duplicate
    relation is still recorded — marking is what lets a reader see two threads
    carry the same text while both still count."""
    hits = [_hit(1, 100, "calibrate the flux capacitor"),
            _hit(2, 200, "calibrate the  flux CAPACITOR"),
            _hit(3, 100, "a second hit in 100")]
    out = group_by_thread(hits, fold_duplicates=False)
    assert [h["thread_id"] for h in out] == [100, 200]
    assert out[0]["_thread_more"] == 1  # the per-thread collapse still runs
    assert out[0]["_dup_thread_ids"] == [200]


def test_cluster_by_thread_groups_hits_and_restores_event_order() -> None:
    hits = [_hit(9, 100, "best hit"), _hit(2, 200, "other thread"),
            _hit(4, 100, "earlier in 100")]
    out = cluster_by_thread(hits, max_threads=10)
    # thread 100 ranked first (its best hit led), and its own hits read in event order
    assert [(h["thread_id"], h["event_id"]) for h in out] == [(100, 4), (100, 9), (200, 2)]
    # ranked position survives the reorder, so the quality verdict can find the head
    assert [h["_rank_pos"] for h in out] == [2, 0, 1]


def test_cluster_by_thread_caps_threads_and_hits_per_thread() -> None:
    hits = [_hit(i, 100, f"hit {i}") for i in range(1, 8)] + [_hit(99, 200, "other")]
    out = cluster_by_thread(hits, max_threads=1, max_per_thread=3)
    assert [h["thread_id"] for h in out] == [100, 100, 100]  # thread 200 past the cap
    assert out[0]["_thread_more"] == 4  # 7 hits, 3 shown, remainder folded onto the lead row


def test_browse_is_an_alias_for_the_default_thread_shape(archive_home) -> None:
    """``group='browse'`` once named a separate enumerating path. The default
    shape enumerates now, so the old name resolves to it rather than erroring —
    a caller carrying it keeps working and gets the same rows."""
    _seed(archive_home)
    for q in ("tachyon", "flux capacitor"):
        assert ([r["thread_id"] for r in search(q, group="browse")]
                == [r["thread_id"] for r in search(q)])


def test_the_thread_shape_carries_the_thread_row_columns(archive_home) -> None:
    _seed(archive_home)
    row = next(r for r in search("tachyon") if r.get("_thread_more"))
    assert row["_group"] == "thread"
    assert row["thread_source"] == "claude-code"
    assert row["n_events"] == 3
    assert row["_thread_more"] == 2  # 3 hits in the thread, one row


def test_search_group_nested_clusters_every_hit_under_its_thread(archive_home) -> None:
    _seed(archive_home)
    rows = search("tachyon", group="nested")
    assert len(rows) == 4  # every hit survives, unlike the one-row-per-thread default
    by_thread = [r["thread_id"] for r in rows]
    assert len(set(by_thread)) == 2
    # each thread's hits are contiguous
    assert by_thread == sorted(by_thread, key=by_thread.index)
    fat = [r for r in rows if by_thread.count(r["thread_id"]) == 3]
    assert [r["event_id"] for r in fat] == sorted(r["event_id"] for r in fat)


def test_search_group_nested_limit_counts_threads(archive_home) -> None:
    """limit bounds threads, not hits — so a cut can't slice a cluster in half."""
    _seed(archive_home)
    rows = search("tachyon", group="nested", limit=1)
    assert len({r["thread_id"] for r in rows}) == 1
    assert len(rows) == 3  # the whole cluster, past a limit of 1


def test_list_shapes_outrank_the_structural_suppressions(archive_home) -> None:
    """A thread_id scope and the structural shapes stand grouping down — but
    asking for a list shape is explicit, so it wins."""
    _seed(archive_home)
    tid = search("tachyon", group="none")[0]["thread_id"]
    assert len(search("tachyon", thread_id=tid, group="thread")) == 1
    assert len(search("tachyon", sort="oldest", group="thread")) == 2

    # count still wins: it tallies the whole unranked pool per thread already.
    assert len(search("tachyon", output="count")) == 4


def test_format_counts_a_thread_granular_result_in_threads(archive_home) -> None:
    """The default shape's rows are threads, so its header counts threads. A
    header reading "4 result(s)" over two threads counts the right number in the
    wrong unit, which is what sends a caller looking for two more conversations."""
    _seed(archive_home)
    rendered = format_results(search("tachyon"), "tachyon")
    assert "2 thread(s) for \"tachyon\"" in rendered


def test_format_nested_renders_thread_headers_over_their_hits(archive_home) -> None:
    _seed(archive_home)
    rendered = format_results(search("tachyon", group="nested"), "tachyon")
    assert "4 result(s) in 2 thread(s) for \"tachyon\"" in rendered
    assert "clustered under their thread" in rendered
    assert "· claude-code · 3 hits" in rendered
    assert "· claude-code · 1 hit\n" in rendered  # singular, not "1 hits"
    assert "the tachyon condenser leaks" in rendered


def test_nested_quality_verdict_judges_the_top_ranked_hit() -> None:
    """Clustering reorders rows, so the verdict must follow ``_rank_pos`` rather
    than whatever hit happens to land in row 0 — otherwise a nested search reports
    the match quality of an arbitrary row."""
    head, tail = _hit(9, 100, "tachyon condenser"), _hit(4, 100, "unrelated prose")
    clustered = cluster_by_thread([head, tail], max_threads=10)
    assert clustered[0] is tail and top_hit(clustered) is head
    assert "quality=strong" in format_results(clustered, "tachyon condenser")
    # ...and row 0's own text would have produced the opposite verdict
    assert "quality=weak" in format_results([tail], "tachyon condenser")


def test_format_renders_fold_annotations() -> None:
    kept = _hit(1, 100, "calibrate the flux capacitor")
    kept["_thread_more"] = 2
    kept["_dup_thread_ids"] = [200, 300, 400, 500]
    rendered = format_results([kept], "flux")
    assert "+2 more in thread" in rendered
    assert "= same content in thread(s) 200, 300, 400, +1 more" in rendered
    assert "grouped: one row per thread" in rendered
