"""The speed regression gate: what it measures, and when it is allowed to red.

``search_lab/speed.py`` has carried the pieces of a latency ratchet for a long
time — the pathological-query pick, the relative ceiling, the per-set baseline —
with nothing invoking them, so a search that returned the right hits ever slower
was caught only when somebody thought to look. ``latency_smoke.py`` is what looks,
on every commit.

A gate is only as good as its false-positive rate: it shares a machine with the
rest of the sweep, so the properties pinned here are mostly about *not* crying
wolf — seed before you judge, confirm before you fail — and about measuring the
calls that were actually recorded rather than their query text alone.

The pipeline is stood in for at :func:`speed.measure`'s own documented seam, so
these run in milliseconds instead of loading a cross-encoder; what is under test
is the gate's judgement, not the engine's speed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "search_lab"))

import latency_smoke  # noqa: E402
import speed  # noqa: E402

from thread_archive._retrieval import usage  # noqa: E402

#: A "normal" stand-in call, and a "regressed" one. Deliberately far apart: these
#: tests measure real wall clock, they run six-wide on a machine that is also
#: doing everything else, and a sleep asked for a millisecond routinely takes ten
#: under that. Anything closer would make the suite fail for the reason the gate
#: itself exists to tolerate, which is not a lesson worth learning twice.
NORMAL_MS = 40.0
SLOW_MS = 600.0


def _search_costing(ms: float):
    """A stand-in pipeline that takes ``ms`` per call."""
    import time

    def search(query, **kwargs):
        time.sleep(ms / 1000.0)
        return []

    return search


def _seed_calls(n: int = 10) -> None:
    for i in range(n):
        usage.record_search(f"query {i}", params={"limit": 20}, hits=[], duration_ms=1.0)


# --- what gets measured ------------------------------------------------------


def test_the_pick_keeps_the_parameters_the_call_was_made_with() -> None:
    """The baseline knows query *text*; the ledger knows the call. Replaying the
    words at default arguments measures a workload nobody ran — a recorded browse
    walk understates by an order of magnitude that way."""
    calls = [("deep walk", {"group": "browse", "page": 30}), ("other", {"limit": 5})]
    baseline = {"by_query": {"deep walk": 900.0}}
    (picked,) = [c for c in latency_smoke.pick_calls(baseline, calls) if c[0] == "deep walk"]
    assert picked[1] == {"group": "browse", "page": 30}


def test_the_pick_is_the_slowest_at_baseline() -> None:
    calls = [(f"q{i}", {}) for i in range(5)]
    baseline = {"by_query": {"q0": 10.0, "q3": 900.0, "q1": 500.0}}
    picked = latency_smoke.pick_calls(baseline, calls)
    assert [q for q, _ in picked][:2] == ["q3", "q1"], "slowest first"


def test_a_short_pick_is_topped_up_rather_than_measured_short() -> None:
    """A baseline query that aged out of the ledger's window must not leave the
    gate computing a p95 over three samples."""
    calls = [(f"q{i}", {}) for i in range(latency_smoke.SMOKE_K + 3)]
    picked = latency_smoke.pick_calls({"by_query": {"q0": 900.0}}, calls)
    assert len(picked) == latency_smoke.SMOKE_K
    assert picked[0][0] == "q0"


def test_without_traffic_there_is_nothing_to_gate(archive_home, capsys) -> None:
    """A fresh install has no recorded calls. A gate that reds on that is a gate
    that gets disabled on day one."""
    assert latency_smoke.main([], search=_search_costing(0)) == 0
    assert "nothing to gate" in capsys.readouterr().out


# --- when it is allowed to red ----------------------------------------------


def test_the_first_run_seeds_and_passes(archive_home) -> None:
    """No reference means no verdict. Recording one and passing is the only
    honest thing a first run can do."""
    _seed_calls()
    assert latency_smoke.main(["--reps", "1"], search=_search_costing(1)) == 0
    baseline = speed.read_baseline(archive_home, query_set=speed.SMOKE_SET)
    assert baseline is not None and baseline["query_set"] == speed.SMOKE_SET


def test_an_unchanged_pipeline_passes(archive_home) -> None:
    _seed_calls(3)
    latency_smoke.main(["--reps", "1"], search=_search_costing(NORMAL_MS))  # seed
    assert latency_smoke.main(["--reps", "1"], search=_search_costing(NORMAL_MS)) == 0


def test_a_real_slowdown_reds(archive_home, capsys) -> None:
    _seed_calls(3)
    latency_smoke.main(["--reps", "1"], search=_search_costing(NORMAL_MS))
    rc = latency_smoke.main(["--reps", "1", "--factor", "2.0"],
                            search=_search_costing(SLOW_MS))
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    # A failure has to be diagnosable without a rerun: what it was, what it had to
    # beat, and what the machine was doing at the time.
    assert "ceiling" in out and "load" in out


def test_a_breach_is_confirmed_before_it_fails(archive_home, capsys) -> None:
    """One measurement on a shared machine is a sample of the machine as much as
    of the code. A gate that fails on that is worse than no gate — so a breach is
    re-measured, and a first pass that was noise passes on the second."""
    _seed_calls(3)
    latency_smoke.main(["--reps", "1"], search=_search_costing(NORMAL_MS))

    # Slow for the whole first pass (its warmup calls included), normal after.
    costs = iter([SLOW_MS] * 6 + [NORMAL_MS] * 100)
    import time

    def flaky(query, **kwargs):
        time.sleep(next(costs) / 1000.0)
        return []

    rc = latency_smoke.main(["--reps", "1", "--factor", "2.0"], search=flaky)
    out = capsys.readouterr().out
    assert rc == 0
    assert "re-measuring to confirm" in out and "was noise" in out


def test_seeding_replaces_a_baseline_the_archive_has_outgrown(archive_home) -> None:
    """The escape hatch a growing corpus needs: the gate measures the live
    archive, so a genuinely slower shape has to be re-referenced deliberately
    rather than by the gate quietly adjusting to whatever it just measured."""
    _seed_calls(3)
    latency_smoke.main(["--reps", "1"], search=_search_costing(1))
    before = speed.read_baseline(archive_home, query_set=speed.SMOKE_SET)

    assert latency_smoke.main(["--seed", "--reps", "1"],
                              search=_search_costing(SLOW_MS)) == 0
    after = speed.read_baseline(archive_home, query_set=speed.SMOKE_SET)
    assert after["total"]["p95"] > before["total"]["p95"]
    # And the re-seeded reference is what the next run is judged against.
    assert latency_smoke.main(["--reps", "1"], search=_search_costing(SLOW_MS)) == 0


def test_an_absolute_budget_overrides_the_ratchet(archive_home, monkeypatch) -> None:
    """A ratchet says "no slower than before"; a budget says "no slower than
    this". The second is what an operator with a client timeout actually has."""
    _seed_calls(3)
    latency_smoke.main(["--reps", "1"], search=_search_costing(1))
    monkeypatch.setenv(latency_smoke.BUDGET_ENV, "5")
    assert latency_smoke.main(["--reps", "1"], search=_search_costing(SLOW_MS)) == 1


def test_the_two_query_sets_keep_separate_baselines(archive_home) -> None:
    """The gate runs in the sweeper's background tier; a replay runs on a box
    somebody is waiting at. Same pipeline, different regimes — so neither may
    move the other's reference."""
    _seed_calls()
    latency_smoke.main(["--reps", "1"], search=_search_costing(1))
    assert speed.read_baseline(archive_home, query_set=speed.SMOKE_SET) is not None
    assert speed.read_baseline(archive_home, query_set=speed.OBSERVED_SET) is None


@pytest.mark.parametrize("outcome", ["seeded", "ok"])
def test_the_verdict_is_writable_as_json(archive_home, tmp_path, outcome) -> None:
    import json

    _seed_calls()
    out = tmp_path / "verdict.json"
    if outcome == "ok":
        latency_smoke.main(["--reps", "1"], search=_search_costing(1))
    latency_smoke.main(["--reps", "1", "--json", str(out)], search=_search_costing(1))
    verdict = json.loads(out.read_text(encoding="utf-8"))
    assert verdict["outcome"] == outcome
    assert verdict["query_set"] == speed.SMOKE_SET
    assert verdict["n_calls"] > 0
