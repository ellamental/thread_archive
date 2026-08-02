"""Enumeration: paging a result set, and knowing when you have seen all of it.

A ranked search returns a cut, and a cut alone reads identically whether ten rows
are all of them or ten of nine hundred. These pin the three mechanisms that close
that gap:

- ``page=N`` walks the set, and pages are slices of ONE ordering, so a walk
  neither repeats nor skips a row
- the result carries the size of the set it is a page of, and says whether that
  number is a total (``exhaustive``) or the reach of a cut pool
- the thread shape resolves its thread list from the whole match set rather than
  from the candidate pool, so paging it to the end reaches every matched thread —
  including the ones ranked past the pool boundary, which are not ranked low but
  absent

plus ``match='substring'``, the opt-in that lifts the recent-window cap on the
one scan that can see within-token matches (``p4`` inside ``mp4``).
"""

from __future__ import annotations

import json

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import search
from thread_archive._retrieval.format import format_results
from thread_archive._retrieval.fts import count_matches
from thread_archive._store import init_db


def _write_cc(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _cc_user(uid, text, day, hhmm="10:00"):
    return {"type": "user", "uuid": uid, "timestamp": f"2026-01-{day:02d}T{hhmm}:00Z",
            "sessionId": "s", "message": {"role": "user", "content": text}}


def _seed_many(archive_home, n: int, term: str = "widget") -> None:
    """``n`` threads, each with one user turn naming ``term`` — a match set big
    enough to outrun a deliberately shallow candidate pool."""
    init_db()
    for i in range(n):
        f = archive_home / f"t{i}.jsonl"
        _write_cc(f, [_cc_user(f"u{i}", f"the {term} report number {i}", (i % 28) + 1)])
        import_session_incremental(f, f"proj:t{i}")


def test_page_walks_the_set_without_repeating_or_skipping(archive_home) -> None:
    _seed_many(archive_home, 12)
    walked: list[str] = []
    for page in range(1, 5):
        rows = search("widget", limit=3, page=page)
        walked += [r["thread_id"] for r in rows]
    assert len(walked) == 12
    assert len(set(walked)) == 12  # disjoint pages, and every thread reached


def test_page_one_is_unchanged_by_the_existence_of_later_pages(archive_home) -> None:
    """The pool is sized from page*limit, so asking for a deep page must not
    reshuffle a shallow one — a caller can't tell a moved row from a missing one."""
    _seed_many(archive_home, 12)
    first = [r["thread_id"] for r in search("widget", limit=3, page=1)]
    again = [r["thread_id"] for r in search("widget", limit=3, page=4)]
    assert first == [r["thread_id"] for r in search("widget", limit=3, page=1)]
    assert not set(first) & set(again)


def test_results_carry_the_size_of_the_set_they_page(archive_home) -> None:
    _seed_many(archive_home, 12)
    rows = search("widget", limit=5)
    assert len(rows) == 5
    assert rows.total_threads == 12
    assert rows.pages == 3
    assert rows.page == 1
    assert rows.exhaustive is True


def test_a_starved_pool_reports_the_real_total_not_its_own_reach(archive_home) -> None:
    """The walk is bounded by the candidate pool; the *total* is not. A pool far
    shallower than the match set can only hand back pool-deep rows — but it asks
    the index how big the set really is rather than reporting how far it got, and
    flags ``exhaustive=False`` so a caller can tell a bounded walk from a finished
    one. Reporting the reach as the total is the silent-truncation lie this whole
    file exists to prevent."""
    from dataclasses import replace

    from thread_archive._retrieval.params import DEFAULT

    _seed_many(archive_home, 30)
    shallow = replace(DEFAULT, pool_floor=4)

    # limit drives the pool depth, so a small one starves it well under the 30
    # messages that actually match.
    cut = search("widget", limit=2, params=shallow)
    assert cut.total == 30          # the real size of the answer, not the pool's
    assert cut.exhaustive is False  # ...and the walk cannot reach all of it
    assert cut.pages * 2 < 30       # pages divide what a walk reaches

    # Sized past the set, the same query is exhaustive and pages to the end.
    whole = search("widget", limit=30, params=shallow)
    assert whole.total == 30
    assert whole.exhaustive is True
    assert whole.pages == 1


def test_a_starved_pool_still_pages_disjointly(archive_home) -> None:
    """The ordering may not depend on which page was asked for. Size the pool from
    page*limit and each page ranks a different prefix, landing the recency tail at
    a shifting offset — a 721-thread walk serves 122 rows twice and skips as many.
    Pinning the pool to (query, limit) is what holds, and this is the shape that
    catches its loss: a match set several times the pool, walked end to end."""
    from dataclasses import replace

    from thread_archive._retrieval.params import DEFAULT

    _seed_many(archive_home, 40)
    shallow = replace(DEFAULT, pool_floor=3)
    totals, walked = set(), []
    page = 1
    while True:
        rows = search("widget", limit=4, page=page, params=shallow)
        totals.add(rows.total)
        walked += [r["thread_id"] for r in rows]
        if page >= rows.pages:
            break
        page += 1
    assert totals == {40}                     # the total never moved as we paged
    assert len(walked) == len(set(walked))    # no row served twice, none skipped


def test_the_ranked_head_leads_the_enumeration(archive_home) -> None:
    """Membership comes from the set, but order still comes from the ranker where
    the ranker reached — an enumeration shouldn't cost you relevance on page 1."""
    init_db()
    for i in range(8):
        f = archive_home / f"r{i}.jsonl"
        _write_cc(f, [_cc_user(f"r{i}", f"widget mention {i}", (i % 28) + 1)])
        import_session_incremental(f, f"proj:r{i}")
    exact = archive_home / "exact.jsonl"
    _write_cc(exact, [_cc_user("x1", "widget widget widget", 1)])
    import_session_incremental(exact, "proj:exact")

    ranked_first = search("widget", limit=1)[0]
    assert "widget widget widget" in (ranked_first.get("thread_title") or "")


def test_a_page_past_the_end_says_so_instead_of_looking_empty(archive_home) -> None:
    """An enumerator that walks off the end must not read its own success as
    'this query matches nothing'."""
    _seed_many(archive_home, 4)
    rows = search("widget", limit=3, page=9)
    assert list(rows) == []
    rendered = format_results(rows, "widget")
    assert "past the end" in rendered
    assert "Every match has been listed" in rendered
    assert "No results" not in rendered


def test_header_names_the_page_and_the_total(archive_home) -> None:
    _seed_many(archive_home, 12)
    rendered = format_results(search("widget", limit=5), "widget")
    assert "5 of 12" in rendered
    assert "page 1/3" in rendered


def test_header_marks_a_cut_pool_as_a_reach_not_a_total(archive_home) -> None:
    """A shape that ranks a bounded pool reports '≥N' and says so — rendering a
    cut as a total is the same silent truncation in a new place."""
    from dataclasses import replace

    from thread_archive._retrieval.params import DEFAULT

    _seed_many(archive_home, 30)
    rendered = format_results(
        search("widget", limit=3, params=replace(DEFAULT, pool_floor=4)),
        "widget",
    )
    assert "of ≥" in rendered
    assert "truncated" in rendered


def test_empty_query_browse_pages_over_an_exact_total(archive_home) -> None:
    """The shape pagination is exact for: a plain indexed SELECT, so the total is
    a count(*) and every row is reachable by OFFSET."""
    _seed_many(archive_home, 12)
    first = search("", limit=5)
    assert first.total == 12
    assert first.pages == 3
    assert first.exhaustive is True
    walked = []
    for page in range(1, 4):
        walked += [r["thread_id"] for r in search("", limit=5, page=page)]
    assert len(set(walked)) == 12

# ── match='substring' ────────────────────────────────────────────────────────


def _seed_within_token(archive_home) -> None:
    """One thread where the term stands alone, one where it lives inside a longer
    word — the distinction the FTS tokenizer cannot make."""
    init_db()
    a = archive_home / "tok.jsonl"
    _write_cc(a, [_cc_user("t1", "the p4 rollout is done", 1)])
    import_session_incremental(a, "proj:tok")
    b = archive_home / "sub.jsonl"
    _write_cc(b, [_cc_user("s1", "converted it to mp4 last night", 2)])
    import_session_incremental(b, "proj:sub")


def test_token_match_does_not_reach_inside_a_word(archive_home) -> None:
    _seed_within_token(archive_home)
    rows = search("p4")
    assert len(rows) == 1
    assert "p4 rollout" in (rows[0].get("thread_title") or "")


def test_substring_match_reaches_inside_a_word(archive_home) -> None:
    _seed_within_token(archive_home)
    rows = search("p4", match="substring")
    assert len({r["thread_id"] for r in rows}) == 2


def test_substring_totals_are_exact_and_pageable(archive_home) -> None:
    init_db()
    for i in range(8):
        f = archive_home / f"s{i}.jsonl"
        _write_cc(f, [_cc_user(f"s{i}", f"encoded clip{i} as mp4 today", (i % 28) + 1)])
        import_session_incremental(f, f"proj:s{i}")
    rows = search("p4", limit=3, match="substring")
    assert rows.total_threads == 8
    assert rows.exhaustive is True
    walked = []
    for page in (1, 2, 3):
        walked += [r["thread_id"] for r in search("p4", limit=3, page=page, match="substring")]
    assert len(set(walked)) == 8


def test_substring_or_unions_alternatives(archive_home) -> None:
    """``OR`` / ``|`` separate alternative substrings rather than folding into
    one literal that occurs nowhere — the fold was both the wrong answer and the
    scan's worst case (a nothing-matches pattern has no LIMIT to stop at, so the
    LIKE reads the whole corpus to return zero)."""
    _seed_within_token(archive_home)
    rows = search('"mp4" OR "p4 rollout"', match="substring")
    assert len({r["thread_id"] for r in rows}) == 2
    rows = search("mp4 | p4 rollout", match="substring")
    assert len({r["thread_id"] for r in rows}) == 2


def test_substring_or_set_agrees_with_the_pool(archive_home) -> None:
    """The exact-set tally resolves the same union predicate the pool matched —
    a set that answered the single-literal question would disagree with every
    page it is supposed to describe."""
    _seed_within_token(archive_home)
    n_events, n_threads, capped = count_matches(
        '"mp4" OR "p4 rollout"', match_mode="substring", content_types=["user"]
    )
    assert (n_threads, capped) == (2, False)


def test_substring_terms_split_on_or_and_pipe() -> None:
    from thread_archive._retrieval.fts import _substring_terms

    assert _substring_terms('"git=" OR "git axis" OR "git scope"') == [
        "git=", "git axis", "git scope"]
    assert _substring_terms("mp4 | p400") == ["mp4", "p400"]
    assert _substring_terms("plain old text") == ["plain old text"]
    assert _substring_terms("dup OR dup") == ["dup"]
    assert _substring_terms("trailing OR") == ["trailing"]


def test_an_unknown_match_mode_is_rejected(archive_home) -> None:
    import pytest

    init_db()
    with pytest.raises(ValueError, match="token.*substring"):
        search("p4", match="regex")


# ── the exact-set primitives ─────────────────────────────────────────────────


def test_count_matches_counts_events_not_threads(archive_home) -> None:
    """Two matching turns in one thread are two events and one thread — the two
    numbers answer different questions and a page header shows both."""
    init_db()
    f = archive_home / "m.jsonl"
    _write_cc(f, [_cc_user("m1", "widget one", 1),
                  _cc_user("m2", "widget two", 1, "11:00")])
    import_session_incremental(f, "proj:m")
    assert count_matches("widget", content_types=["user"]) == (2, 1, False)


def test_count_matches_reports_events_and_threads(archive_home) -> None:
    _seed_many(archive_home, 5)
    n_events, n_threads, capped = count_matches("widget", content_types=["user"])
    assert (n_events, n_threads, capped) == (5, 5, False)


# ── bounding a scanning predicate ────────────────────────────────────────────
# SET_SCAN_CAP counts rows that MATCHED, which describes a MATCH's work and not a
# LIKE's: a LIKE has no index to walk, so it reads every row to find out. Left at
# that the cost inverts — a corpus-common substring hits the cap early and a rare
# one never does, making the *selective* query, which is the whole point of the
# mode, the expensive one.


def test_a_scanning_predicate_is_the_one_that_carries_a_row_window() -> None:
    from thread_archive._retrieval.fts import _primary_predicate

    def scans(query, *, match="token", startswith=None):
        return _primary_predicate(query, match_mode=match, startswith=startswith)[2]

    assert scans("widget report", match="substring") is True
    assert scans("", startswith="widget") is True
    assert scans("widget report") is False
    assert scans("widget | report") is False  # the OR pass is still a MATCH


def test_the_row_window_bounds_the_scan_and_says_so() -> None:
    """Past the cap the scan cannot have seen the whole corpus, so the answer is a
    floor — the same degradation the match cap already promises, reported the same
    way."""
    from thread_archive._retrieval.fts import SET_EXAMINE_CAP, _scan_window

    below, floored = _scan_window(SET_EXAMINE_CAP - 1)
    assert floored is False and below["scan_floor"] == 0

    above, floored = _scan_window(SET_EXAMINE_CAP + 500)
    assert floored is True and above["scan_floor"] == 500


def test_an_unreadable_watermark_declines_to_bound_the_scan() -> None:
    """Slow is recoverable; silently truncated is not. With no watermark to
    measure the window from, the scan runs unbounded rather than guessing a floor
    that might cut the corpus in half."""
    from thread_archive._retrieval.fts import _scan_window

    params, floored = _scan_window(object())
    assert floored is False and params["scan_floor"] == 0


def test_only_a_scanning_predicate_pays_for_the_window_clause() -> None:
    """A MATCH walks one term's doclist — already proportional to what it finds —
    so bounding it by rowid would drop matches for nothing."""
    from thread_archive._retrieval.fts import _set_scan_sql

    scanning = _set_scan_sql("thread_id", "content LIKE :sub", [], scan_floor=True)
    assert "rowid > :scan_floor" in scanning
    matching = _set_scan_sql("thread_id", "event_search MATCH :q", [])
    assert "scan_floor" not in matching


def test_a_substring_set_within_the_window_is_still_exact(archive_home) -> None:
    """The window is a ceiling on work, not a haircut: a corpus smaller than it
    resolves the same set it always did, within-token matches included."""
    init_db()
    f = archive_home / "mp4.jsonl"
    _write_cc(f, [_cc_user("s1", "the mp4 encode failed", 1),
                  _cc_user("s2", "unrelated report", 2)])
    import_session_incremental(f, "proj:mp4")
    n_events, n_threads, capped = count_matches(
        "p4", match_mode="substring", content_types=["user"])
    assert capped is False  # nothing was cut, so the enumeration is a total
    assert (n_events, n_threads) == (1, 1)


def test_the_exact_set_honors_the_same_scope_as_the_pool(archive_home) -> None:
    """A filter that applied to the pool but not the tally would make the two
    disagree about the same corpus — and the tally is what a paginated caller
    trusts to know when it has seen everything."""
    _seed_many(archive_home, 6)
    scoped = search("widget", limit=2, source=["nonesuch"])
    assert scoped.total_threads == 0
    assert list(scoped) == []


# ── the exact-set memo ───────────────────────────────────────────────────────
# The set scan does not depend on the page: a browse resolves the whole match set
# to decide membership and totals, then slices one page out of it. Walking N pages
# re-ran the identical scan N times, and it is the largest stage in that walk.
def test_paging_a_reconciled_search_resolves_the_exact_set_once(archive_home) -> None:
    from thread_archive._retrieval import SearchParams
    from thread_archive._retrieval.fts import reset_set_memo, set_memo_stats

    _seed_many(archive_home, 12)
    reset_set_memo()
    # Saturate the pool so the exact-set reconciliation runs at all; it is the
    # scan whose one-per-walk cost this test is about.
    p = SearchParams(pool_floor=1)
    first = search("widget", limit=2, params=p, page=1)
    assert set_memo_stats()["misses"] == 1  # page 1 pays the scan
    for page in (2, 3):
        later = search("widget", limit=2, params=p, page=page)
        assert later.total_threads == first.total_threads
    stats = set_memo_stats()
    assert stats["hits"] == 2 and stats["misses"] == 1


def test_the_memoized_set_still_sees_newly_indexed_threads(archive_home) -> None:
    """A thread that arrives between two searches is counted by the second — a
    stale set would drop it from the page it legitimately ranks onto, not merely
    date the total."""
    _seed_many(archive_home, 4)
    before = search("widget", limit=10).total_threads
    f = archive_home / "late.jsonl"
    _write_cc(f, [_cc_user("late1", "the widget report number 99", 5)])
    import_session_incremental(f, "proj:late")
    assert search("widget", limit=10).total_threads == before + 1


def test_the_ledger_says_which_of_the_two_the_set_stage_did(archive_home) -> None:
    """The memo problem this stage had was invisible in ``set_ms``: a defeated memo
    and an expensive corpus produce the same number. Each outcome now names itself,
    so the question is one field in the ledger rather than an inference."""
    from thread_archive._retrieval import _probe
    from thread_archive._retrieval.fts import reset_set_memo

    _seed_many(archive_home, 5)
    reset_set_memo()

    with _probe.install() as first:
        count_matches("widget", content_types=["user"])
    assert first.as_record()["set_scans"] == 1

    with _probe.install() as repeat:
        count_matches("widget", content_types=["user"])
    rec = repeat.as_record()
    assert rec["set_hits"] == 1 and "set_scans" not in rec

    f = archive_home / "late.jsonl"
    _write_cc(f, [_cc_user("late1", "the widget report number 99", 5)])
    import_session_incremental(f, "proj:late")

    # An append moves the watermark, and a count cannot be carried across one.
    with _probe.install() as after_ingest:
        count_matches("widget", content_types=["user"])
    rec = after_ingest.as_record()
    assert rec["set_scans"] == 1 and "set_hits" not in rec


def test_resetting_the_memo_forces_a_fresh_scan(archive_home) -> None:
    """Reindex drops the memo outright: it changes the set in ways the append
    watermark cannot see."""
    from thread_archive._retrieval.fts import reset_set_memo, set_memo_stats

    _seed_many(archive_home, 4)
    reset_set_memo()
    assert count_matches("widget", content_types=["user"])[0] == 4
    assert count_matches("widget", content_types=["user"])[0] == 4
    assert set_memo_stats() == {"entries": 1, "hits": 1, "misses": 1}
    reset_set_memo()
    assert count_matches("widget", content_types=["user"])[0] == 4
    assert set_memo_stats() == {"entries": 1, "hits": 0, "misses": 1}
