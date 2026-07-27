"""The benchmark runner: selection, freshness, and reading what a row produced.

Running a row needs a built corpus and the model arms, which live on the
operator's box — so what is exercised here is everything around that: which rows
``--only`` selects, whether a recorded run still describes the current code, and
the two report shapes a row's numbers arrive in. The freshness rule is the
load-bearing one: skip too eagerly and the bench reports numbers from before the
edit under review.
"""

from __future__ import annotations

from search_lab import bench_runs, benchmark


def test_every_row_is_a_published_benchmark() -> None:
    # The manifest is the whole of what this bench claims, and every row on it has
    # to be a corpus somebody else labeled. A row scored against labels made here
    # would be scored against what this ranker already finds.
    harnesses = {r.argv[0].rsplit("/", 1)[-1] for r in benchmark.manifest()}
    assert harnesses == {"beir_eval.py", "cdr_eval.py", "haystack_eval.py"}


def test_only_narrows_by_name_fragment() -> None:
    chosen = benchmark.select(benchmark.manifest(), only=["locomo"])
    assert chosen and all("locomo" in r.name for r in chosen)


def test_no_selection_runs_the_whole_manifest() -> None:
    rows = benchmark.manifest()
    assert benchmark.select(rows, only=[]) == rows


def test_every_row_has_a_stable_unique_name() -> None:
    # The ledger keys on the name, so a duplicate would merge two rows' histories
    # and a renamed one would silently start a new series.
    names = [r.name for r in benchmark.manifest()]
    assert len(names) == len(set(names))


def test_a_row_takes_its_corpus_id_from_its_built_home(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "snapshot.json").write_text('{"snapshot_id": "abcdef0123456789"}')
    row = benchmark.Row(name="beir:test", cost_min=1, argv=["beir.py"], home=home)

    assert row.corpus_id() == "abcdef0123456789"


def test_a_corpus_that_is_not_built_has_no_id(tmp_path) -> None:
    row = benchmark.Row(name="beir:test", cost_min=1,
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


def test_a_report_carries_what_the_run_cost_beside_what_it_scored() -> None:
    # Kept apart from `measures` on purpose: one is what the row is scored on and
    # moves against a published baseline, the other is what the box did to produce
    # it. A p99 listed among the nDCGs would read as a result.
    payload = {
        "n": 300, "ndcg10": 0.579,
        "performance": {"queries": 300, "qps": 5.45,
                        "total": {"p50": 170.7, "p95": 296.9, "p99": 377.2},
                        "stages": {"rank_ms": {"p50": 118.8, "p95": 237.9, "p99": 309.4}}},
    }
    perf = benchmark.performance_from_report(payload)
    assert perf["total"]["p99"] == 377.2
    assert perf["stages"]["rank_ms"]["p50"] == 118.8
    assert "p99" not in benchmark.measures_from_report(payload)


def test_the_in_house_report_shape_still_yields_a_profile() -> None:
    # The evaluator predates the shared block and reports `latency` instead. Its
    # rows are the oldest history in the ledger, and dropping their profile would
    # put a hole in exactly the comparison the ledger exists for.
    perf = benchmark.performance_from_report({
        "n": 75,
        "latency": {"n": 70, "total": {"p50": 210.0, "p95": 900.0, "p99": 1200.0},
                    "stages": {"fts_ms": {"p50": 40.0, "p95": 70.0, "p99": 90.0}},
                    "cold": 1, "pool_p50": 800},
    })
    assert perf["total"]["p50"] == 210.0 and perf["queries"] == 75
    assert perf["staged"] == 70 and perf["cold"] == 1


def test_a_report_with_no_profile_yields_none_rather_than_empty_numbers() -> None:
    # An absent profile and a run that was instant are different facts, and zeros
    # would make the first unreadable as anything but the second.
    assert benchmark.performance_from_report({"n": 5, "ndcg10": 0.5}) == {}


def test_a_run_records_its_cost_and_omits_the_key_when_it_has_none(tmp_path) -> None:
    bench_runs.record_run(row="a:row", argv=["x"], corpus_id=None, measures={},
                          elapsed_s=90.0, status="ok", home=tmp_path,
                          performance={"total": {"p50": 12.0}, "scoring_s": 30.0})
    bench_runs.record_run(row="b:row", argv=["x"], corpus_id=None, measures={},
                          elapsed_s=1.0, status="failed", home=tmp_path)

    rows = {r["row"]: r for r in bench_runs.read_runs(tmp_path)}
    assert rows["a:row"]["performance"]["total"]["p50"] == 12.0
    # The scored loop is 30s of a 90s run: the rest is the corpus being built,
    # which is most of a cold row's wall-clock and none of its search cost.
    assert rows["a:row"]["elapsed_s"] - rows["a:row"]["performance"]["scoring_s"] == 60.0
    assert "performance" not in rows["b:row"]


def test_a_query_row_separates_a_deep_rank_from_a_miss() -> None:
    """The distinction the per-query record exists for. A gold document ranked
    40th is a ranking problem and one that never came back is a recall problem;
    they are fixed in different places, and both score 0.0 at k=10."""
    from search_lab import eval_core

    deep = eval_core.query_row(qid=1, query="q", latency_s=0.2, rank=40, n_gold=1,
                               found=0, measures={"ndcg10": 0.0})
    miss = eval_core.query_row(qid=2, query="q", latency_s=0.2, rank=None, n_gold=1,
                               found=0, measures={"ndcg10": 0.0})
    assert deep["rank"] == 40 and miss["rank"] is None
    assert deep["measures"] == miss["measures"], "the score cannot tell them apart"
    assert deep["latency_ms"] == 200.0


def test_a_query_row_caps_the_text_it_keeps() -> None:
    # A conversational benchmark's "query" is a whole multi-turn context, and
    # 1,583 of them uncapped would make the detail file the largest thing here.
    from search_lab import eval_core

    row = eval_core.query_row(qid=1, query="x " * 500, latency_s=0.0, rank=1,
                              n_gold=1, found=1, measures={})
    assert len(row["query"]) == eval_core.QUERY_TEXT_CAP + 1  # + the ellipsis
    assert row["query"].endswith("…")


def test_per_query_detail_is_stored_beside_the_ledger_and_read_back(tmp_path) -> None:
    rows = [{"qid": "a", "query": "one", "rank": None, "measures": {"ndcg10": 0.0}}]
    bench_runs.write_queries("abc123", rows, home=tmp_path)

    assert bench_runs.read_queries("abc123", home=tmp_path) == rows
    # A run with no detail is not an error: one from before the harnesses
    # reported any, and one the cap has pruned, both read as "nothing here".
    assert bench_runs.read_queries("nothere", home=tmp_path) == []


def test_a_run_id_from_a_url_cannot_name_a_path(tmp_path) -> None:
    # The id reaches the store from a URL segment. Hex-only, so no traversal and
    # no absolute path can be spelled in it.
    (tmp_path / "secret.json").write_text('["x"]', encoding="utf-8")
    for hostile in ("../secret", "/etc/passwd", "..%2Fsecret", "a/b"):
        assert bench_runs.read_queries(hostile, home=tmp_path) == []


def test_the_detail_store_is_capped_and_drops_the_oldest_first(tmp_path) -> None:
    # The aggregate history is small and kept forever; this is the bulky half,
    # and it is worth keeping only while the configuration is one you are still
    # deciding about.
    import os
    import time

    for i in range(5):
        bench_runs.write_queries(f"{i:012x}", [{"qid": str(i)}], home=tmp_path)
        os.utime(bench_runs.queries_dir(tmp_path) / f"{i:012x}.json",
                 (time.time() + i, time.time() + i))

    bench_runs.prune_queries(home=tmp_path, keep=2)
    kept = {p.stem for p in bench_runs.queries_dir(tmp_path).glob("*.json")}
    assert kept == {f"{3:012x}", f"{4:012x}"}


def test_per_query_lifts_both_report_shapes() -> None:
    external = benchmark.per_query_from_report({
        "per_query": [{"qid": "1", "query": "q", "rank": 3, "measures": {"ndcg10": 0.5}}]})
    assert external[0]["rank"] == 3

    # The in-house evaluator predates the shared shape and records a reciprocal
    # rank; the rank it came from is recoverable exactly, and 0 means not found.
    inhouse = benchmark.per_query_from_report({
        "per_case": [{"query": "a", "rr": 0.25, "latency_ms": 12.0},
                     {"query": "b", "rr": 0.0, "latency_ms": 9.0, "difficulty": "intent"}]})
    assert [r["rank"] for r in inhouse] == [4, None]
    assert inhouse[1]["group"] == "intent"

    assert benchmark.per_query_from_report({"n": 3}) == []


def test_a_run_id_is_a_hash_of_the_record_and_not_its_place_in_the_file() -> None:
    # A link into the history has to survive the ledger growing under it, which a
    # line number would not — the file is appended to on every bench pass.
    a = {"at": "2026-07-27T03:43:13+00:00", "row": "beir:scifact[lexical]",
         "status": "ok", "elapsed_s": 55.7}
    assert bench_runs.run_id(a) == bench_runs.run_id(dict(reversed(list(a.items()))))
    assert bench_runs.run_id(a) != bench_runs.run_id({**a, "elapsed_s": 55.8})


def test_ledger_round_trips_and_reads_newest_first(tmp_path) -> None:
    for i in range(3):
        bench_runs.record_run(row="beir:test", argv=["x"], corpus_id=f"c{i}",
                              measures={"ndcg10": 0.5 + i / 100}, elapsed_s=1.0,
                              status="ok", home=tmp_path)
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
    hay = rows["locomo[lexical]"]

    assert beir.code_id() != hay.code_id()
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
    row = benchmark.Row(name="r", cost_min=20, argv=["x.py"])
    assert benchmark.estimate(row, {"elapsed_s": 60.0}) == 1.0
    assert benchmark.estimate(row, None) == 20.0
    assert benchmark.estimate(row, {"elapsed_s": 0}) == 20.0
