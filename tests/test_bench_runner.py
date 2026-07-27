"""The benchmark runner: tier selection, freshness, and reading what a row produced.

Running a row needs a built corpus and the model arms, which live on the
operator's box — so what is exercised here is everything around that: which rows a
tier selects, whether a recorded run still describes the current code, and the two
report shapes a row's numbers arrive in. The freshness rule is the load-bearing
one: skip too eagerly and the bench reports numbers from before the edit under
review.
"""

from __future__ import annotations

import json

from search_lab import bench_runs, benchmark


def test_tiers_nest_so_standard_includes_smoke() -> None:
    rows = benchmark.manifest()
    smoke = {r.name for r in benchmark.select(rows, tier="smoke", only=[])}
    standard = {r.name for r in benchmark.select(rows, tier="standard", only=[])}
    full = {r.name for r in benchmark.select(rows, tier="full", only=[])}

    assert smoke < standard < full
    # Smoke is exactly the instruments that can credit a ranking change.
    assert smoke == {"gold-gate:archive", "gold-gate:swe-chat"}


def test_only_narrows_by_name_fragment() -> None:
    chosen = benchmark.select(benchmark.manifest(), tier="full", only=["locomo"])
    assert chosen and all("locomo" in r.name for r in chosen)


def test_every_row_has_a_stable_unique_name() -> None:
    # The ledger keys on the name, so a duplicate would merge two rows' histories
    # and a renamed one would silently start a new series.
    names = [r.name for r in benchmark.manifest()]
    assert len(names) == len(set(names))


def test_gold_rows_take_their_corpus_id_from_the_snapshot_they_score(tmp_path) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "snapshot.json").write_text('{"snapshot_id": "abcdef0123456789"}')
    row = benchmark.Row(name="gold-gate:test", tier="smoke", cost_min=1,
                        argv=["gate.py", "--snap", str(snap), "--gold-dir", str(tmp_path)],
                        gold_dir=tmp_path, needs_json_out=False)

    assert row.snap == snap
    assert row.corpus_id() == "abcdef0123456789"


def test_a_corpus_that_is_not_built_has_no_id(tmp_path) -> None:
    row = benchmark.Row(name="beir:test", tier="standard", cost_min=1,
                        argv=["beir.py"], home=tmp_path / "never-built")
    assert row.corpus_id() is None


def test_source_hash_moves_with_an_edit_a_commit_would_not_show(tmp_path) -> None:
    # The tuning loop's unit of change is an edited default that is never
    # committed, so a commit-keyed cache would skip every row after the first edit
    # and report pre-edit numbers.
    (tmp_path / "ranking").mkdir()
    params = tmp_path / "ranking" / "params.py"
    params.write_text("fusion_weight = 400\n")
    (tmp_path / "ranking" / "notes.md").write_text("not source\n")
    before = bench_runs.hash_sources(tmp_path, ("ranking",))

    params.write_text("fusion_weight = 500\n")
    assert bench_runs.hash_sources(tmp_path, ("ranking",)) != before

    params.write_text("fusion_weight = 400\n")
    assert bench_runs.hash_sources(tmp_path, ("ranking",)) == before


def test_source_hash_covers_renames_and_ignores_non_source(tmp_path) -> None:
    (tmp_path / "ranking").mkdir()
    (tmp_path / "ranking" / "a.py").write_text("x = 1\n")
    before = bench_runs.hash_sources(tmp_path, ("ranking",))

    (tmp_path / "ranking" / "README.md").write_text("prose\n")
    assert bench_runs.hash_sources(tmp_path, ("ranking",)) == before

    (tmp_path / "ranking" / "a.py").rename(tmp_path / "ranking" / "b.py")
    assert bench_runs.hash_sources(tmp_path, ("ranking",)) != before


def test_source_hash_survives_a_missing_entry(tmp_path) -> None:
    # A partial checkout should measure what it has rather than refuse to record.
    assert bench_runs.hash_sources(tmp_path, ("absent",)) == bench_runs.hash_sources(
        tmp_path, ())


def test_code_id_reads_the_real_checkout() -> None:
    assert len(bench_runs.code_id()) == 16
    assert bench_runs.code_id() == bench_runs.hash_sources(
        bench_runs._repo_root(), bench_runs.CODE_PATHS)


def test_is_fresh_needs_the_same_code_and_corpus() -> None:
    record = {"status": "ok", "code_id": bench_runs.code_id(), "corpus_id": "aaaa"}

    assert bench_runs.is_fresh(record, corpus_id="aaaa")
    assert not bench_runs.is_fresh(record, corpus_id="bbbb")
    assert not bench_runs.is_fresh({**record, "code_id": "stale"}, corpus_id="aaaa")
    assert not bench_runs.is_fresh(None, corpus_id="aaaa")


def test_is_fresh_falls_back_to_code_when_the_corpus_has_no_cheap_identity() -> None:
    # The per-question haystacks are hundreds of homes; their corpora come from
    # immutable dataset files, so the code hash decides alone.
    record = {"status": "ok", "code_id": bench_runs.code_id(), "corpus_id": "aaaa"}
    assert bench_runs.is_fresh(record, corpus_id=None)


def test_a_failed_run_is_never_fresh() -> None:
    # Otherwise a row that broke would look measured forever, and the bench would
    # report a gap as a result.
    record = {"status": "failed", "code_id": bench_runs.code_id(), "corpus_id": "aaaa"}
    assert not bench_runs.is_fresh(record, corpus_id="aaaa")


def test_ledger_round_trips_and_reads_newest_first(tmp_path) -> None:
    for i in range(3):
        bench_runs.record_run(row="beir:test", argv=["x"], corpus_id=f"c{i}",
                              measures={"ndcg10": 0.5 + i / 100}, elapsed_s=1.0,
                              status="ok", tier="standard", home=tmp_path)
    bench_runs.record_run(row="other:test", argv=["y"], corpus_id="c9", measures={},
                          elapsed_s=1.0, status="failed", home=tmp_path)

    rows = bench_runs.read_runs(tmp_path, row="beir:test")
    assert [r["corpus_id"] for r in rows] == ["c2", "c1", "c0"]
    assert bench_runs.read_runs(tmp_path, limit=1)[0]["row"] == "other:test"
    assert bench_runs.latest_ok(tmp_path, row="other:test") is None


def test_measures_from_a_shared_corpus_report() -> None:
    payload = {"dataset": "scifact", "n": 300, "ndcg10": 0.579, "mrr10": 0.525,
               "recall": {"10": 0.773, "100": 0.915}, "query_p50_ms": 173.0}
    assert benchmark.measures_from_report(payload) == {
        "n": 300, "ndcg10": 0.579, "mrr10": 0.525,
        "recall10": 0.773, "recall100": 0.915, "query_p50_ms": 173.0}


def test_measures_from_a_per_question_haystack_report() -> None:
    # JSON turns the integer cutoffs into strings on the way out; a ledger row's
    # measures have to mean one thing whichever harness wrote them.
    payload = {"dataset": "locomo", "overall": {
        "n": 1986, "recall": {"5": 0.6, "10": 0.653},
        "ndcg": {"5": 0.5, "10": 0.55}, "recall_all": {"5": 0.2, "10": 0.3}}}
    assert benchmark.measures_from_report(payload) == {
        "n": 1986, "recall5": 0.6, "recall10": 0.653,
        "ndcg5": 0.5, "ndcg10": 0.55, "recall_all5": 0.2, "recall_all10": 0.3}


def test_gold_measures_pool_by_case_count(tmp_path) -> None:
    # Files differ in size by an order of magnitude; a plain mean over files would
    # let a 7-case topic file outvote a 75-case protocol one.
    (tmp_path / "gold-runs.jsonl").write_text(json.dumps({
        "kind": "gold-run", "at": "2026-07-27T04:00:00+00:00", "passed": True,
        "files": {
            "a-cases.jsonl": {"n": 75, "mrr": 0.8, "success10": 1.0,
                              "recall10": 0.9, "ndcg10": 0.7},
            "b-cases.jsonl": {"n": 25, "mrr": 0.4, "success10": 0.6,
                              "recall10": 0.5, "ndcg10": 0.3},
        }}) + "\n")

    got = benchmark.measures_from_gold_ledger(tmp_path, since="2026-07-27T03:00:00")
    assert got["mrr"] == 0.7 and got["ndcg10"] == 0.6  # weighted 75:25, not 50:50
    assert got["n"] == 100 and got["files"] == 2 and got["passed"] is True


def test_gold_measures_ignore_a_ledger_row_older_than_this_run(tmp_path) -> None:
    # A row that failed before writing would otherwise report the previous run's
    # numbers as its own.
    (tmp_path / "gold-runs.jsonl").write_text(json.dumps({
        "kind": "gold-run", "at": "2026-07-26T01:00:00+00:00",
        "files": {"a-cases.jsonl": {"n": 1, "mrr": 1.0}}}) + "\n")

    assert benchmark.measures_from_gold_ledger(
        tmp_path, since="2026-07-27T03:00:00") == {}


def test_child_env_drops_the_home_and_arm_switches(monkeypatch) -> None:
    # Every row names its own corpus and its own arms; an inherited EMBED=off would
    # turn a [vectors] row into a lexical one under a name that says otherwise.
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", "/somewhere")
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED", "off")
    monkeypatch.setenv("THREAD_ARCHIVE_COHERENCE", "off")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = benchmark.child_env()
    assert "THREAD_ARCHIVE_HOME" not in env
    assert "THREAD_ARCHIVE_EMBED" not in env
    assert "THREAD_ARCHIVE_COHERENCE" not in env
    assert env["PATH"] == "/usr/bin"


def test_a_rows_code_id_covers_its_own_harness() -> None:
    # Editing one harness must re-run its rows and leave the others fresh, so a
    # row's code id is the shared ranking source plus the script it invokes.
    rows = {r.name: r for r in benchmark.manifest()}
    beir = rows["beir:scifact[lexical]"]
    gate = rows["gold-gate:archive"]

    assert beir.code_id() != gate.code_id()
    assert beir.code_id() != bench_runs.code_id()
    assert beir.code_id() == bench_runs.code_id(("search_lab/beir_eval.py",))


def test_two_rows_of_the_same_harness_share_a_code_id() -> None:
    # The arms differ, not the code; what separates their measurements is the row
    # name, and each keeps its own series under it.
    rows = {r.name: r for r in benchmark.manifest()}
    assert (rows["beir:scifact[lexical]"].code_id()
            == rows["beir:scifact[vectors]"].code_id())


def test_previous_configuration_skips_a_re_measured_identical_config(tmp_path) -> None:
    # During a tuning loop the same configuration is often measured twice; a delta
    # against it is zero, which reads as "the change did nothing".
    for code, ndcg in (("old", 0.50), ("new", 0.60), ("new", 0.61)):
        bench_runs.record_run(row="r", argv=["x"], corpus_id="c",
                              measures={"ndcg10": ndcg}, elapsed_s=1.0,
                              status="ok", code=code, home=tmp_path)

    prior = benchmark.previous_configuration("r", current_code="new", home=tmp_path)
    assert prior["measures"]["ndcg10"] == 0.50
    # Nothing recorded under other code: no comparison rather than a false one.
    assert benchmark.previous_configuration("r", current_code="old",
                                            home=tmp_path)["measures"]["ndcg10"] == 0.61
    assert benchmark.previous_configuration("absent", current_code="new",
                                            home=tmp_path) is None


def test_estimate_prefers_what_the_row_actually_took() -> None:
    # A row's cost is dominated by whether its corpus is already built, which the
    # manifest cannot know and the last run's elapsed time does.
    row = benchmark.Row(name="r", tier="smoke", cost_min=20, argv=["x.py"])
    assert benchmark.estimate(row, {"elapsed_s": 60.0}) == 1.0
    assert benchmark.estimate(row, None) == 20.0
    assert benchmark.estimate(row, {"elapsed_s": 0}) == 20.0
