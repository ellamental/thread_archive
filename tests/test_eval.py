"""The search-quality scoring core (``search_lab/eval_core.py``).

The pure pieces — event pairing, the ranking-metric loop against a fake ranker, the
behavioral rollup — are pinned in ``test_retrieval_eval.py`` (which loads the dev
bench that re-exports this module). This file covers what needs a real store
underneath it: the latency instrumentation the scoring loop records as it runs,
and the throughput rollup built from it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from search_lab import eval_core as _eval
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


def test_percentiles_are_nearest_rank_and_never_interpolate() -> None:
    """At gold-file sizes a p99 is one sample. Interpolating between two of them
    would report a latency no search actually took, so every reported percentile
    has to be an observed value."""
    values = [float(v) for v in range(1, 101)]

    d = _eval.percentiles(values)

    assert d == {"p50": 50.0, "p95": 95.0, "p99": 99.0}
    assert _eval.percentiles([]) == {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    # every reported value is one of the samples, at any n
    small = _eval.percentiles([7.0, 3.0])
    assert set(small.values()) <= {3.0, 7.0}


def test_latency_profile_summarizes_the_stages_the_probes_recorded() -> None:
    """A scoring run's own probes carry the per-stage split. It rides the report so
    a regression can be located — "p95 doubled in the lexical arm" — which a single
    median cannot state."""
    samples = [
        {"fts_ms": 10.0, "semantic_ms": 4.0, "pool_size": 100, "total_ms": 12.0},
        {"fts_ms": 90.0, "semantic_ms": 6.0, "pool_size": 300, "total_ms": 95.0,
         "cold": True},
    ]

    prof = _eval.latency_profile([0.012, 0.095], samples)

    assert prof["n"] == 2
    assert prof["cold"] == 1                      # one search paid a model load
    assert prof["total"]["p50"] == 12.0           # seconds in, milliseconds out
    assert prof["stages"]["fts_ms"]["p95"] == 90.0
    assert "total_ms" not in prof["stages"]       # the total is not a stage
    assert "pool_size" not in prof["stages"]      # nor is a scalar fact


def test_latency_profile_omits_stages_that_never_ran() -> None:
    """An explicit zero reads as *measured and instant* rather than *did not
    happen* — a structural search sits the vector arm out entirely, and a stage
    that never ran must not land in the profile claiming it was free."""
    prof = _eval.latency_profile(
        [0.01], [{"fts_ms": 10.0, "semantic_ms": 0.0, "total_ms": 10.0}])

    assert "fts_ms" in prof["stages"]
    assert "semantic_ms" not in prof["stages"]
    assert _eval.latency_profile([], [])["n"] == 0


def test_evaluate_reports_per_case_latency_and_a_stage_profile(archive_home) -> None:
    """The instrumentation is on the scoring path, not beside it: a quality run
    executes the same searches a latency run would pay for again, so it records the
    profile it already produced."""
    init_db()
    tid = _seed_titled_thread("how does token auth work in this system")

    report = _eval.evaluate(
        [{"query": "token auth", "gold": [tid]}], limit=10,
        content_type=None, exclude_content_types=_eval.EXCLUDE_META)

    assert report["per_case"][0]["latency_ms"] >= 0.0
    assert set(report["latency"]) == {"n", "total", "stages", "cold", "pool_p50"}
    # the legacy scalar still agrees with the profile it was derived from
    assert round(report["latency_p50_ms"], 1) == report["latency"]["total"]["p50"]


def test_the_performance_block_reports_throughput_beside_the_distribution() -> None:
    """What every benchmark harness writes to its report and the ledger keeps.

    A row already runs the exact workload a latency measurement would run again,
    so recording only a median throws away a distribution and a per-stage profile
    already paid for."""
    perf = _eval.performance(
        [0.010, 0.020, 0.030, 0.400],
        [{"fts_ms": 5.0, "semantic_ms": 3.0, "total_ms": 10.0, "pool_size": 40}],
        scoring_s=2.0, corpus_docs=5183, arms=["lexical"])

    assert perf["queries"] == 4 and perf["qps"] == 2.0
    assert perf["mean_ms"] == 115.0 and perf["max_ms"] == 400.0
    assert perf["total"]["p50"] == 20.0
    assert perf["stages"]["fts_ms"]["p50"] == 5.0
    # Below the query count: three searches carried no breakdown, and they are
    # excluded rather than averaged in as instant ones.
    assert perf["staged"] == 1
    assert perf["corpus_docs"] == 5183 and perf["arms"] == ["lexical"]


def test_a_scoring_loop_that_took_no_time_reports_no_throughput() -> None:
    # A division that would be infinite: better an absent rate than one claiming
    # the box served queries at a speed nothing measured.
    assert _eval.performance([], [], scoring_s=0.0)["qps"] is None
    assert _eval.performance([0.01], [], scoring_s=0.0)["qps"] is None
