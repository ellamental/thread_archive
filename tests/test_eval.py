"""The search-quality scoring core (:mod:`thread_archive._eval`).

The pure pieces — event pairing, the MRR/recall loop against a fake ranker, the
behavioral rollup — are pinned in ``test_retrieval_eval.py`` (which loads the
dev bench that re-exports this module). This file covers the DB-backed case
*builders* that need a real store: the title-recall sampler and the trail miner
behind ``thread_archive eval``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from thread_archive import _eval
from thread_archive._retrieval import index_events
from thread_archive._store import Event, Thread, get_session, init_db


def _seed_titled_thread(title: str, *, user_turns: int = 3) -> str:
    """A conversation thread with a title and ``user_turns`` user events indexed."""
    with get_session() as s:
        t = Thread(name=f"conv:{title}", title=title, thread_type="conversation",
                   source="claude-code", source_id=f"sess:{title}")
        s.add(t)
        s.flush()
        evs = []
        for i in range(user_turns):
            e = Event(thread_id=t.id, stream_id="turns", event_type="user_message_sent",
                      payload={"content": f"{title} — user turn {i}"},
                      occurred_at=datetime(2026, 1, 1, 10, i, tzinfo=timezone.utc))
            s.add(e)
            s.flush()
            evs.append(e)
        index_events(s, evs)
        tid = t.id
        s.commit()
    return tid


def _seed_trail(session_name: str, pairs: list[tuple[str, str | None]]) -> str:
    """A session thread carrying a tool-use trail of ``(query, read_ref)`` events.

    Each pair emits a ``thread_search`` tool_use followed by a ``thread_read``
    (when ``read_ref`` is not None), in order — the shape ``_trail_events`` mines.
    """
    with get_session() as s:
        sess = Thread(name=f"sess:{session_name}", thread_type="conversation",
                      source="claude-code", source_id=f"agent:{session_name}")
        s.add(sess)
        s.flush()
        clock = iter(range(10, 60))
        for query, read_ref in pairs:
            s.add(Event(thread_id=sess.id, stream_id="trail",
                        event_type="tool_use_complete",
                        payload={"tool_name": "mcp__thread-archive__thread_search",
                                 "input": {"query": query}},
                        occurred_at=datetime(2026, 1, 1, 12, next(clock),
                                             tzinfo=timezone.utc)))
            if read_ref is not None:
                s.add(Event(thread_id=sess.id, stream_id="trail",
                            event_type="tool_use_complete",
                            payload={"tool_name": "mcp__thread-archive__thread_read",
                                     "input": {"thread_id": read_ref}},
                            occurred_at=datetime(2026, 1, 1, 12, next(clock),
                                                 tzinfo=timezone.utc)))
        sid = sess.id
        s.commit()
    return sid


def test_sample_title_cases_uses_the_title_as_query(archive_home) -> None:
    init_db()
    tid = _seed_titled_thread("how does token auth work in this system")

    cases = _eval.sample_title_cases(50, seed=7)

    assert len(cases) == 1
    (case,) = cases
    assert case["query"] == "how does token auth work in this system"
    assert case["gold"] == [tid]


def test_sample_title_cases_skips_thin_threads(archive_home) -> None:
    """Under the min-content bar (fewer than 3 user turns) a thread can't be gold —
    it has nothing for its own title to rank against."""
    init_db()
    _seed_titled_thread("a title with too little conversation behind it",
                        user_turns=1)

    assert _eval.sample_title_cases(50, seed=7) == []


def test_mine_log_cases_pairs_search_with_the_thread_read_after_it(archive_home) -> None:
    init_db()
    gold = _seed_titled_thread("the thread the searcher actually opened")
    sess = _seed_trail("agent-1", [("what did we decide about tokens", gold)])

    cases = _eval.mine_log_cases(50, seed=7)

    assert len(cases) == 1
    (case,) = cases
    assert case["query"] == "what did we decide about tokens"
    assert case["gold"] == [gold]
    # The originating session is recorded so scoring skips it (it quotes the query).
    assert case["sessions"] == [sess]


def test_mine_log_cases_drops_a_search_with_no_read(archive_home) -> None:
    """A search nobody clicked yields no relevance label — no gold, no case."""
    init_db()
    _seed_trail("agent-2", [("a query that led nowhere", None)])

    assert _eval.mine_log_cases(50, seed=7) == []


def test_mine_log_cases_drops_gold_that_is_not_a_live_thread(archive_home) -> None:
    """A read ref that resolves to nothing (thread never existed / unresolvable)
    can't be gold."""
    init_db()
    _seed_trail("agent-3", [("query reading a bogus id", "01BOGUSREADREFTHATNOEXIST0")])

    assert _eval.mine_log_cases(50, seed=7) == []


def test_mine_log_cases_drops_gold_excluded_from_search(archive_home) -> None:
    """A read that resolves to a real but search-excluded thread is filtered too —
    it exists, but it isn't a live search target, so it can't be gold."""
    init_db()
    gold = _seed_titled_thread("a real thread that is hidden from search")
    with get_session() as s:
        s.get(Thread, gold).exclude_from_search = True
        s.commit()
    _seed_trail("agent-4", [("query reading a hidden thread", gold)])

    assert _eval.mine_log_cases(50, seed=7) == []


def test_mine_log_cases_after_bound_holds_out_earlier_trail(archive_home) -> None:
    """The date bound keeps only trail events at or after it — the time-based holdout."""
    init_db()
    gold = _seed_titled_thread("thread opened before the holdout date")
    _seed_trail("agent-5", [("a search from before the cutoff", gold)])  # stamped 2026-01-01

    assert _eval.mine_log_cases(50, seed=7, after="2026-01-01") != []
    assert _eval.mine_log_cases(50, seed=7, after="2026-06-01") == []


def test_load_case_file_round_trips_and_carries_the_snapshot_bound(tmp_path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps({"query": "q1", "gold": ["A", "B"], "sessions": ["S"]}) + "\n"
        + json.dumps({"query": "q2", "gold": ["C"], "until": "2026-07-01"}) + "\n"
        + "\n"  # blank line tolerated
    )

    cases = _eval.load_case_file(path)

    assert cases[0] == {"query": "q1", "gold": ["A", "B"], "sessions": ["S"]}
    assert cases[1]["until"] == "2026-07-01"
    assert cases[1]["sessions"] == []
