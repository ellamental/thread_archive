"""The quality gate: the bench's numbers against the frozen accepted ones.

The gate is the only thing in this repo that can fail a release on *ranking*, so
what it must get right is not "does it compute a subtraction" but the decision
table around the subtraction — which states are a regression, which are an
unmeasured row wearing a regression's clothes, and which are two different
measurements being compared as though they were one.

Split in three:

- the **band**, which is a resolution argument rather than a noise one: scoring
  is deterministic, so the tolerance exists because a row of ``n`` queries cannot
  express a movement finer than ``1/n``
- the **decision table** over ``compare_row``, pure and hermetic — every state
  reachable without a corpus, a ledger, or a bench pass
- the **repo-level ratchet**: the checked-in baseline still describes the bench
  it claims to. A row rename orphans its accepted numbers silently, and a release
  is far too late to find out the gate has been checking a number against
  nothing.

What is *not* here is the live gate — this box's recorded bench history against
the checked-in file. Nothing in the quality stack reads the archive home any more
(the bench measures public corpora, and its ledger lives in the lab's own state
root), but the suite is sandboxed off *every* machine location, so the ledger
resolves into a tmpdir here just as the archive home does. That is the isolation
working. The gate is answerable only once the bench has run at the code under
test anyway, so it runs as a command from the release preflight
(``python -m search_lab gate``, see ``docs/internal/releasing.md``) — the same shape
ci.toml's ``retrieval-gate`` row already takes.
"""

from __future__ import annotations

import json

import pytest

from search_lab import bench_runs, benchmark, quality_gate


def _entry(**over):
    base = {"n": 300, "code_id": "CODE", "corpus_id": "CORP",
            "measures": {"ndcg10": 0.700, "recall10": 0.800}}
    base.update(over)
    return base


def _record(**over):
    base = {"status": "ok", "code_id": "CODE", "corpus_id": "CORP",
            "measures": {"n": 300, "ndcg10": 0.700, "recall10": 0.800}}
    base.update(over)
    return base


def _measures(**over):
    m = {"n": 300, "ndcg10": 0.700, "recall10": 0.800}
    m.update(over)
    return m


# ── the band ─────────────────────────────────────────────────────────────────


def test_the_band_is_two_cases_wide_in_the_rows_own_units() -> None:
    """Points are not comparable across rows — 0.01 is three cases on a
    300-query row and twenty on a 1,981-query one — so the band is derived from
    ``n`` rather than fixed."""
    assert quality_gate.tolerance_for(_entry(n=300), "ndcg10") == pytest.approx(2 / 300)
    assert quality_gate.tolerance_for(_entry(n=354), "ndcg10") == pytest.approx(2 / 354)


def test_a_very_large_row_still_gets_a_usable_band() -> None:
    """2/n on a 5,000-query row is 0.0004, below the fourth decimal the baseline
    records — without the floor the gate would fail on the rounding rather than
    on the ranking."""
    assert quality_gate.tolerance_for(_entry(n=5000), "ndcg10") == quality_gate.MIN_TOLERANCE


def test_a_row_with_no_recorded_size_falls_back_to_the_floor() -> None:
    # Deriving from a missing n would hand out an infinite band, which is a gate
    # that passes everything while reading as though it checked.
    assert quality_gate.tolerance_for(_entry(n=None), "ndcg10") == quality_gate.MIN_TOLERANCE


def test_an_explicit_band_overrides_the_derived_one_per_measure() -> None:
    """A row-wide number widens the whole row; a mapping widens one measure. Both
    exist so a documented weakness in one corpus's labels can be stated where it
    applies instead of loosening every other row to accommodate it."""
    assert quality_gate.tolerance_for(_entry(tolerance=0.05), "ndcg10") == 0.05
    entry = _entry(tolerance={"ndcg10": 0.05})
    assert quality_gate.tolerance_for(entry, "ndcg10") == 0.05
    assert quality_gate.tolerance_for(entry, "recall10") == pytest.approx(2 / 300)


# ── the decision table ───────────────────────────────────────────────────────


def test_a_row_at_its_baseline_passes() -> None:
    verdict = quality_gate.compare_row("r", _entry(), _record(), current_code="CODE")
    assert verdict.state == "ok" and not verdict.failed


def test_a_fall_past_the_band_is_a_regression() -> None:
    fallen = _record(measures=_measures(ndcg10=0.700 - 3 / 300))
    verdict = quality_gate.compare_row("r", _entry(), fallen, current_code="CODE")
    assert verdict.state == "regressed" and verdict.failed
    breached = [d for d in verdict.deltas if d.breached]
    assert [d.measure for d in breached] == ["ndcg10"]
    # The other measure held, and the render names only what actually moved —
    # a failure listing every metric buries which one to go and look at.
    assert "ndcg10" in verdict.render() and "recall10" not in verdict.render()


def test_a_fall_inside_the_band_passes() -> None:
    """One case's worth of movement on a two-case band. Not noise — scoring is
    deterministic — but below the resolution at which the row can distinguish a
    ranking change from a shuffle inside cases that already worked."""
    drifted = _record(measures=_measures(ndcg10=0.700 - 1 / 300))
    assert quality_gate.compare_row("r", _entry(), drifted, current_code="CODE").state == "ok"


def test_an_improvement_of_any_size_passes() -> None:
    """The band is a floor under the numbers, not a pin on them. A gate that
    flagged a rise would fail on exactly the outcome the bench exists to find."""
    lifted = _record(measures=_measures(ndcg10=0.95, recall10=0.99))
    assert quality_gate.compare_row("r", _entry(), lifted, current_code="CODE").state == "ok"


def test_an_unmeasured_row_fails_rather_than_passing_quietly() -> None:
    """A row with no run at all is the state a gate must never wave through: it
    is indistinguishable from here between 'the corpus is not built on this box'
    and 'nobody ran the bench', and both mean the ranking under release is
    unmeasured."""
    verdict = quality_gate.compare_row("r", _entry(), None, current_code="CODE")
    assert verdict.state == "missing" and verdict.failed


def test_a_row_measured_at_other_code_is_stale_not_passing() -> None:
    """The failure mode this exists for: a ranking edit lands, the bench is not
    re-run, and the ledger still holds numbers that clear the baseline. Reported
    as its own state so the fix (run the bench) is distinguishable from the fix
    for a regression (revert the change)."""
    verdict = quality_gate.compare_row("r", _entry(), _record(code_id="OTHER"),
                                       current_code="CODE")
    assert verdict.state == "stale" and verdict.failed
    assert "OTHER" in verdict.detail and "CODE" in verdict.detail


def test_allow_stale_compares_anyway_and_says_so_in_the_state() -> None:
    """The mid-tuning read. It must still be visible in the output that the
    comparison crossed a code boundary — a plain ``ok`` here would let a
    stale-but-passing row be quoted as a release check."""
    verdict = quality_gate.compare_row("r", _entry(), _record(code_id="OTHER"),
                                       current_code="CODE", allow_stale=True)
    assert verdict.state == "ok (stale)" and not verdict.failed


def test_allow_stale_still_fails_a_stale_row_that_regressed() -> None:
    # The escape hatch loosens which code a number came from, never whether the
    # number cleared the bar.
    fallen = _record(code_id="OTHER", measures=_measures(ndcg10=0.5))
    verdict = quality_gate.compare_row("r", _entry(), fallen,
                                       current_code="CODE", allow_stale=True)
    assert verdict.state == "regressed" and verdict.failed


def test_a_different_query_count_is_a_different_measurement() -> None:
    """A rebuilt corpus that scored a different number of queries is not a worse
    measurement of the same thing — it is a measurement of something else, and a
    delta read across that boundary files a corpus change as a ranking
    regression."""
    verdict = quality_gate.compare_row("r", _entry(), _record(measures=_measures(n=250)),
                                       current_code="CODE")
    assert verdict.state == "corpus-changed" and verdict.failed
    assert "250" in verdict.detail and "300" in verdict.detail


def test_a_rebuilt_corpus_is_caught_even_at_the_same_query_count() -> None:
    # The snapshot id moves when the documents do, so a re-download that happens
    # to keep the query count is still not the corpus the baseline was taken on.
    verdict = quality_gate.compare_row("r", _entry(), _record(corpus_id="OTHER"),
                                       current_code="CODE")
    assert verdict.state == "corpus-changed" and verdict.failed


def test_corpus_identity_is_checked_before_the_numbers() -> None:
    """Order matters for the message: a row that both changed corpus and appears
    to have fallen must report the corpus, because the fall is not a fact yet."""
    both = _record(corpus_id="OTHER", measures=_measures(n=250, ndcg10=0.1))
    assert quality_gate.compare_row("r", _entry(), both,
                                    current_code="CODE").state == "corpus-changed"


def test_a_haystack_row_without_a_corpus_id_still_compares() -> None:
    """The per-question haystacks build hundreds of small homes and have no single
    snapshot to name. Absent on either side, the check sits out rather than
    failing every one of those rows forever."""
    entry = _entry(corpus_id=None)
    del entry["corpus_id"]
    verdict = quality_gate.compare_row("r", entry, _record(corpus_id=None),
                                       current_code="CODE")
    assert verdict.state == "ok"


def test_a_run_carrying_none_of_the_baselined_measures_is_not_a_pass() -> None:
    """An empty overlap subtracts nothing and would otherwise report ``ok`` on
    zero evidence — the most dangerous shape a gate can take."""
    verdict = quality_gate.compare_row("r", _entry(), _record(measures={"n": 300}),
                                       current_code="CODE")
    assert verdict.state == "missing" and verdict.failed


# ── the set ──────────────────────────────────────────────────────────────────


def _ledger(tmp_path, records) -> None:
    (tmp_path / bench_runs.LEDGER_FILE).write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_a_manifest_row_with_no_accepted_numbers_is_reported_not_failed(tmp_path) -> None:
    """Which rows a box can run is a fact about the box — several corpora are
    days of CPU to build. A gate that demanded every manifest row would be
    unrunnable anywhere but the machine that built them all."""
    _ledger(tmp_path, [])
    rows = benchmark.manifest()[:2]
    verdicts = quality_gate.check({"rows": {}}, rows, home=tmp_path)
    assert [v.state for v in verdicts] == ["ungated", "ungated"]
    assert not any(v.failed for v in verdicts)


def test_a_baselined_row_the_manifest_dropped_fails(tmp_path) -> None:
    """A renamed row orphans its accepted numbers, and the orphan is worse than
    no baseline: it reads as a gated row while being checked against nothing."""
    _ledger(tmp_path, [])
    verdicts = quality_gate.check({"rows": {"gone[lexical]": _entry()}},
                                  benchmark.manifest()[:1], home=tmp_path)
    orphan = next(v for v in verdicts if v.row == "gone[lexical]")
    assert orphan.state == "unknown-row" and orphan.failed


def test_a_known_row_that_is_merely_unselected_is_not_reported_as_dropped(tmp_path) -> None:
    """``--only`` narrows what runs, not what exists. Deriving the drift check
    from the selection would report the entire rest of the bench as renamed."""
    _ledger(tmp_path, [])
    whole = benchmark.manifest()
    baseline = {"rows": {row.name: _entry() for row in whole}}
    verdicts = quality_gate.check(baseline, whole[:1],
                                  known={r.name for r in whole}, home=tmp_path)
    assert [v.row for v in verdicts] == [whole[0].name]


def test_the_other_tiers_baselined_rows_are_not_reported_as_dropped(tmp_path) -> None:
    """The baseline holds the quick tier's rows, so a full-tier gate meets names
    its own manifest does not contain. That is a depth it is not running, not a
    rename — reporting it as one would leave the unused tier permanently red."""
    _ledger(tmp_path, [])
    full = benchmark.manifest()
    known = {r.name for r in full} | {r.quick().name for r in full}
    sampled = next(r for r in full if r.quick() is not r)
    verdicts = quality_gate.check({"rows": {sampled.quick().name: _entry()}},
                                  full, known=known, home=tmp_path)
    assert not any(v.state == "unknown-row" for v in verdicts)
    assert not any(v.failed for v in verdicts)


def test_the_gate_reads_the_rows_latest_successful_run(tmp_path) -> None:
    """A failed row carries no numbers, so it must not shadow the last run that
    did — otherwise one broken pass makes a healthy row read as unmeasured."""
    row = benchmark.manifest()[0]
    _ledger(tmp_path, [
        {"row": row.name, **_record(code_id=row.code_id())},
        {"row": row.name, "status": "failed", "code_id": row.code_id(), "measures": {}},
    ])
    baseline = {"rows": {row.name: _entry()}}
    assert quality_gate.check(baseline, [row], home=tmp_path)[0].state == "ok"


# ── accepting a baseline ─────────────────────────────────────────────────────


def test_update_records_only_rows_that_have_actually_run(tmp_path) -> None:
    """A row with no run contributes nothing rather than a zero, which would read
    as an accepted number and gate every future run against a floor of zero."""
    rows = benchmark.manifest()[:3]
    _ledger(tmp_path, [{"row": rows[0].name, **_record(code_id=rows[0].code_id())}])
    built = quality_gate.build_baseline(rows, home=tmp_path)
    assert list(built["rows"]) == [rows[0].name]


def test_update_carries_an_explicit_band_forward(tmp_path) -> None:
    """A widened band encodes a judgment about a corpus, not about the numbers it
    was holding — re-accepting numbers must not silently retighten it."""
    row = benchmark.manifest()[0]
    _ledger(tmp_path, [{"row": row.name, **_record(code_id=row.code_id())}])
    previous = {"rows": {row.name: {"tolerance": 0.05}}}
    built = quality_gate.build_baseline([row], home=tmp_path, previous=previous)
    assert built["rows"][row.name]["tolerance"] == 0.05


def test_updating_one_tier_leaves_the_other_tiers_accepted_numbers_alone(tmp_path) -> None:
    """Both depths keep their accepted numbers in the one file. An update at one
    of them that dropped the other's rows would ungate them — which reads green,
    because an ungated row never fails."""
    full = benchmark.manifest()
    sampled = next(r for r in full if r.quick() is not r)
    known = {r.name for r in full} | {r.quick().name for r in full}
    quick_rows = [r.quick() for r in full]
    _ledger(tmp_path, [{"row": sampled.quick().name,
                        **_record(code_id=sampled.quick().code_id())}])
    previous = {"rows": {sampled.name: _entry()}}

    built = quality_gate.build_baseline(quick_rows, home=tmp_path,
                                        previous=previous, known=known)

    assert sampled.quick().name in built["rows"], "the measured quick row"
    assert sampled.name in built["rows"], "the full tier's row, carried forward"


def test_an_update_drops_rows_no_tier_runs_any_more(tmp_path) -> None:
    """Carrying entries forward must not resurrect orphans: a row nothing runs is
    an accepted number checked against nothing, which is what `unknown-row`
    exists to catch."""
    row = benchmark.manifest()[0]
    _ledger(tmp_path, [{"row": row.name, **_record(code_id=row.code_id())}])
    previous = {"rows": {"gone[lexical]": _entry()}}

    built = quality_gate.build_baseline([row], home=tmp_path, previous=previous,
                                        known={row.name})

    assert "gone[lexical]" not in built["rows"]


def test_an_accepted_row_records_what_it_was_measured_on(tmp_path) -> None:
    """The provenance is what makes a baseline auditable: a number whose commit,
    code hash and corpus are recorded can be re-derived, and one whose aren't is
    a value somebody typed."""
    row = benchmark.manifest()[0]
    _ledger(tmp_path, [{"row": row.name, "commit": "abc1234", "at": "2026-07-27T23:34:21",
                        **_record(code_id=row.code_id())}])
    entry = quality_gate.build_baseline([row], home=tmp_path)["rows"][row.name]
    assert entry["commit"] == "abc1234" and entry["code_id"] == row.code_id()
    assert entry["corpus_id"] == "CORP" and entry["n"] == 300


def test_a_round_trip_through_the_file_passes_its_own_gate(tmp_path) -> None:
    """The property that makes ``--update`` usable at all: what it writes is what
    the gate then accepts. A rounding or key mismatch between the two would make
    every fresh baseline fail on the pass right after accepting it."""
    row = benchmark.manifest()[0]
    _ledger(tmp_path, [{"row": row.name, **_record(code_id=row.code_id())}])
    path = tmp_path / "baseline.json"
    quality_gate.write_baseline(quality_gate.build_baseline([row], home=tmp_path), path)
    verdicts = quality_gate.check(quality_gate.load_baseline(path), [row], home=tmp_path)
    assert [v.state for v in verdicts] == ["ok"]


def test_a_missing_baseline_gates_nothing_but_a_corrupt_one_raises(tmp_path) -> None:
    """Absent means "nothing has been accepted yet", which is a real state on a
    fresh checkout. Unparseable is not — read as empty it would silently gate
    nothing while the release lane reported a pass."""
    assert quality_gate.load_baseline(tmp_path / "nope.json") == {"rows": {}}
    bad = tmp_path / "bad.json"
    bad.write_text('{"rows": []}', encoding="utf-8")
    with pytest.raises(ValueError):
        quality_gate.load_baseline(bad)


# ── the checked-in baseline ──────────────────────────────────────────────────
# Fast, hermetic, and in every pytest pass: these are the checks that catch the
# baseline drifting away from the bench, and the release lane is far too late to
# discover the gate has been checking numbers against nothing.


def test_the_checked_in_baseline_is_a_baseline() -> None:
    baseline = quality_gate.load_baseline()
    assert baseline["rows"], "the repo ships an empty baseline — nothing is gated"
    assert baseline.get("accepted_at")


def test_every_baselined_row_is_a_row_the_bench_still_runs() -> None:
    known = {row.name for row in benchmark.manifest()}
    known |= {row.quick().name for row in benchmark.manifest()}
    orphans = set(quality_gate.load_baseline()["rows"]) - known
    assert not orphans, f"baselined rows the manifest no longer knows: {sorted(orphans)}"


def test_every_baselined_row_carries_provenance_and_a_scored_size() -> None:
    """Without ``n`` the band silently collapses to the floor, and without the
    code id there is nothing to say which ranking the number describes."""
    for name, entry in quality_gate.load_baseline()["rows"].items():
        assert entry.get("measures"), name
        assert isinstance(entry.get("n"), int) and entry["n"] > 0, name
        assert entry.get("code_id"), name
        assert all(isinstance(v, (int, float)) for v in entry["measures"].values()), name


def test_a_baselined_measure_is_one_its_row_actually_reports() -> None:
    """A measure the row never records reads as gated and is skipped at compare
    time — the same silent no-op as an orphaned row, one level down."""
    rows = {row.name: row for row in benchmark.manifest()}
    rows.update({row.quick().name: row.quick() for row in benchmark.manifest()})
    for name, entry in quality_gate.load_baseline()["rows"].items():
        assert set(entry["measures"]) <= set(rows[name].measure_keys), name


def test_run_hands_the_bench_the_same_tier_and_selection_it_will_compare() -> None:
    """A dropped ``--quick`` would have the bench score the full rows while the
    gate compared their sampled names — every gated row reading as never
    measured, off a flag that was passed."""
    assert quality_gate.bench_argv(quick=False, only=[]) == ["--quiet"]
    assert "--quick" in quality_gate.bench_argv(quick=True, only=[])
    assert quality_gate.bench_argv(quick=False, only=["scifact", "locomo"]) == [
        "--quiet", "--only", "scifact", "--only", "locomo"]


def test_the_quick_tier_gates_the_sampled_rows_own_names() -> None:
    """Sampled rows record under ``row~N`` and carry their own accepted numbers —
    a quick gate reading the full rows' baseline would compare two different
    query sets."""
    quick = {row.quick().name for row in benchmark.manifest()}
    verdicts = quality_gate.check({"rows": {}},
                                  [r.quick() for r in benchmark.manifest()])
    assert {v.row for v in verdicts} <= quick
    assert any("~" in v.row for v in verdicts)


def test_the_gate_is_reachable_by_the_name_the_release_runs_it_under() -> None:
    """``python -m search_lab gate`` is what the release preflight types, and the
    front door is the one part of this that no other test covers — the gate could
    be entirely correct and the release step still be a typo."""
    from search_lab import __main__ as front_door

    assert "gate" in front_door.COMMANDS
    # argparse exits on --help rather than returning; zero is the front door
    # having dispatched to a real parser instead of an unknown-command error.
    with pytest.raises(SystemExit) as exit_code:
        front_door.main(["gate", "--help"])
    assert exit_code.value.code == 0
