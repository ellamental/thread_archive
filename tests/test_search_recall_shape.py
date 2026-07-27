"""Tier-0 recall-shape eval: exhaustive retrieval and chronology, offline.

Sibling to ``test_search_quality.py`` over the same synthetic corpus, asking the
question that file's metrics cannot ask. MRR and success@k score *ordering* —
which thread wins — and a ranker that returns one right answer per query
satisfies them. The shapes here are the ones a ranked window can't serve:

- **exhaustive** — "every thread that mentions X". Scored on the nonce-sentinel
  block, whose gold is true by construction (see ``quality_corpus.SENTINEL``):
  corpus noise cannot fuzz a term that exists nowhere else, so these assertions
  stay exact as the corpus grows. Note what is *not* the claim: a ranked call
  sized past the answer reaches the same set (the pool follows ``limit``), and a
  ranked result already reports ``total`` / ``pages`` / ``exhaustive``. The
  enumerating shapes' distinct property is that they do not fold cross-thread
  duplicates — see ``test_browse_does_not_fold_what_the_ranked_shape_folds``.
- **chronological** — "the first / last time we discussed X", a question about
  dates that relevance order answers wrongly by construction. Scored on the
  dated series block, built so the earliest mention is the *weakest* match.

Both are recall claims, so the assertions are two-sided: a thread that matches
must be *returned*, not merely ranked below something better. A test that
tolerates a missing match measures precision while claiming to measure recall.
"""

from __future__ import annotations

import pytest

from thread_archive._retrieval import search

from .quality_corpus import (
    SENTINEL,
    SENTINEL_N,
    SENTINEL_THREADS,
    SERIES,
    SERIES_DENSEST,
    SERIES_ORDER,
    build_corpus,
)


@pytest.fixture
def corpus(archive_home) -> dict[str, str]:
    return build_corpus(archive_home)


def _names(hits, corpus: dict[str, str]) -> list[str]:
    by_id = {v: k for k, v in corpus.items()}
    return [by_id[h["thread_id"]] for h in hits]


# ── exhaustive recall ────────────────────────────────────────────────────────

def test_every_thread_carrying_a_term_is_enumerable(corpus) -> None:
    """The list shape returns all ``SENTINEL_N`` threads — more than the default
    result window holds — and nothing else. This is the assertion the ordering
    metrics cannot make: not "the best one ranked", but "none went missing"."""
    rows = search(SENTINEL, group="browse", limit=SENTINEL_N + 10)
    assert sorted(_names(rows, corpus)) == sorted(SENTINEL_THREADS)
    assert len({r["thread_id"] for r in rows}) == SENTINEL_N  # one row per thread


def test_ranked_window_cuts_the_set_but_does_not_corrupt_it(corpus) -> None:
    """The ranked shape returns a *window* onto the match set, not the set: it
    fills the window exactly, and every row in it genuinely carries the term.
    Pinning both sides is what keeps the window honest — a ranker that returned
    fewer rows than it had matches, or padded with non-matches, fails here."""
    hits = search(SENTINEL, limit=20)
    assert len(hits) == 20
    assert set(_names(hits, corpus)) <= set(SENTINEL_THREADS)
    assert len({h["thread_id"] for h in hits}) == 20


def test_a_widened_ranked_window_reaches_the_whole_set(corpus) -> None:
    """The cut above is the window, not a ceiling: the candidate pool resolves to
    ``max(limit * 5, pool_floor)``, so widening ``limit`` past the answer's size
    widens the pool with it and the ranked shape returns every match too.

    Pinned because it is easy to lose: capping pool depth independently of
    ``limit`` would leave threads unreachable at *any* window, turning "sized it
    too small" into a silent recall ceiling with nothing to distinguish them."""
    hits = search(SENTINEL, limit=SENTINEL_N + 26)
    assert sorted(set(_names(hits, corpus))) == sorted(SENTINEL_THREADS)


def test_a_cut_window_says_so_rather_than_looking_complete(corpus) -> None:
    """A short window is a cut, not an ambiguity. The ranked result reports the
    size of the match *set* beside the page, so "these are all of them" and
    "these are 20 of 24" are distinguishable without a second query — which is
    the whole reason a ranked search can be trusted to say when it is done."""
    cut = search(SENTINEL, limit=20)
    assert len(cut) == 20
    assert cut.total_threads == SENTINEL_N
    assert cut.pages == 2

    whole = search(SENTINEL, limit=SENTINEL_N + 26)
    assert len(whole) == SENTINEL_N
    assert whole.total_threads == SENTINEL_N
    assert whole.pages == 1


def test_near_identical_threads_each_keep_a_row(corpus) -> None:
    """The series threads mention the term in identical assistant text — an agent
    fan-out's shape, one prompt spawned many ways. They are still eleven separate
    conversations, so "which threads mention this" must count eleven: a fold that
    *removes* rows answers a smaller number than the truth, and identical wording
    is not identical work.

    The near-duplicate relation is still reported — ``_dup_thread_ids`` marks the
    rows that share content — it just no longer decides membership."""
    hits = search(SERIES, limit=len(SERIES_ORDER) + 10)
    assert sorted(_names(hits, corpus)) == sorted(SERIES_ORDER)
    assert hits.total_threads == len(SERIES_ORDER)
    assert any(h.get("_dup_thread_ids") for h in hits), (
        "the duplicate relation must still be visible, just not enforced by deletion")


def test_collapse_is_available_for_callers_that_want_brevity(corpus) -> None:
    """``collapse=True`` restores the fold for a caller spending result slots on
    distinct content rather than on completeness — and it annotates rather than
    discards, so every collapsed thread is still named on the row that absorbed
    it."""
    folded = search(SERIES, limit=len(SERIES_ORDER) + 10, collapse=True)
    assert len(folded) < len(SERIES_ORDER)
    absorbed = {t for h in folded for t in (h.get("_dup_thread_ids") or [])}
    assert len(folded) + len(absorbed) == len(SERIES_ORDER), (
        "a collapsed thread must be named on the row that absorbed it, not dropped")


def test_count_output_tallies_every_matching_thread(corpus) -> None:
    """``output='count'`` answers "how many" over the whole unranked pool, so
    its thread tally is the full answer set even though the ranked window isn't."""
    pool = search(SENTINEL, output="count")
    assert {h["thread_id"] for h in pool} == {corpus[n] for n in SENTINEL_THREADS}


# ── chronology ───────────────────────────────────────────────────────────────

def test_first_mention_is_the_oldest_not_the_strongest(corpus) -> None:
    """"When did this first come up" is a date question. The series is built so
    relevance and chronology disagree — the densest mention is late — so a
    chronological scan that merely echoed the ranker would fail this."""
    assert _names(search(f"{SERIES} rollout", limit=40), corpus)[0] == SERIES_DENSEST
    assert _names(search(SERIES, sort="oldest", limit=40), corpus)[0] == SERIES_ORDER[0]


def test_oldest_sort_enumerates_the_whole_series_in_order(corpus) -> None:
    """Strictly chronological *and* complete: every thread mentioning the term
    appears, once, in date order. A scan that ordered correctly while dropping
    mentions in the middle would pass an ordering-only assertion."""
    rows = search(SERIES, group="browse", sort="oldest", limit=len(SERIES_ORDER) + 10)
    assert _names(rows, corpus) == SERIES_ORDER

    stamps = [h["occurred_at"] for h in search(SERIES, sort="oldest", limit=40)]
    assert stamps == sorted(stamps)


def test_last_mention_is_answerable_from_the_enumerated_set(corpus) -> None:
    """There is no ``sort='newest'``: the most recent mention is read off the
    enumerated set, which means every row must carry the timestamp of the event
    that matched. Losing ``occurred_at`` on a list row would leave the question
    unanswerable while search still looked healthy."""
    rows = search(SERIES, group="browse", limit=len(SERIES_ORDER) + 10)
    assert all(r.get("occurred_at") for r in rows)
    latest = max(rows, key=lambda r: r["occurred_at"])
    assert _names([latest], corpus) == [SERIES_ORDER[-1]]


def test_an_unknown_sort_is_rejected_not_ignored(corpus) -> None:
    """A caller asking for a chronological order search doesn't have must hear
    so. Served silently as relevance order, the wrong answer is indistinguishable
    from the right one."""
    with pytest.raises(ValueError, match="sort"):
        search(SERIES, sort="newest")
