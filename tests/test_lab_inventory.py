"""The search-lab page's data layer.

These pin the properties that make the difference between an inventory and a list
of hopeful names: a size that stopped early says so, the parts add up to the
whole, an unbuilt corpus is a row rather than an absence, and every descriptive
field is read off a registry rather than restated here.
"""

from __future__ import annotations

import json

from search_lab import inventory


def _tree(root, spec: dict) -> None:
    """Write ``{relative path: contents}`` under ``root``."""
    for rel, body in spec.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


# ── sizes ────────────────────────────────────────────────────────────────────

def test_dir_bytes_sums_the_tree(tmp_path) -> None:
    _tree(tmp_path, {"a.txt": "x" * 10, "sub/b.txt": "y" * 5, "sub/deep/c.txt": "z"})
    size = inventory.dir_bytes(tmp_path)
    assert size == {"bytes": 16, "files": 3, "truncated": False}


def test_a_walk_that_stops_early_says_so(tmp_path) -> None:
    # A partial sum reported as a total understates the corpus by whatever the
    # walk did not reach, and nothing in the number itself would reveal it.
    _tree(tmp_path, {f"f{i}.txt": "x" for i in range(20)})
    size = inventory.dir_bytes(tmp_path, budget=5)
    assert size["truncated"] is True
    assert size["files"] <= 20
    assert size["bytes"] < 20


def test_a_missing_directory_is_zero_not_an_error(tmp_path) -> None:
    assert inventory.dir_bytes(tmp_path / "nope") == {
        "bytes": 0, "files": 0, "truncated": False}


def test_the_cache_index_walks_each_subtree_once_and_the_parts_add_up(tmp_path) -> None:
    _tree(tmp_path, {
        "scifact/corpus.jsonl": "x" * 100,
        "homes/scifact/index.db": "y" * 200,
        "homes/swe-chat/index.db": "z" * 400,
        "stray.json": "w" * 8,
    })
    index = inventory.size_index(tmp_path)

    assert index[str(tmp_path / "homes" / "swe-chat")]["bytes"] == 400
    assert index[str(tmp_path / "scifact")]["bytes"] == 100
    # The homes roll-up is the sum of its children, and the root is the sum of
    # every child plus the loose files — so a page can print the total above the
    # rows without the two disagreeing.
    assert index[str(tmp_path / "homes")]["bytes"] == 600
    assert index[str(tmp_path)]["bytes"] == 708


def test_a_truncated_part_makes_the_total_a_floor(tmp_path) -> None:
    parts = [{"bytes": 10, "files": 1, "truncated": False},
             {"bytes": 5, "files": 1, "truncated": True}]
    assert inventory._sum_sizes(parts) == {"bytes": 15, "files": 2, "truncated": True}


# ── corpus homes ─────────────────────────────────────────────────────────────

def test_a_stamped_home_reports_its_own_counts(tmp_path) -> None:
    home = tmp_path / "homes" / "scifact"
    _tree(tmp_path, {"homes/scifact/snapshot.json": json.dumps({
        "snapshot_id": "abc123", "counts": {"threads": 5183, "vectors": 5848},
        "embedding_space": "local:nomic", "created_at": "2026-07-27T03:42:18Z"})})

    row = inventory._home_row(home, label="corpus")
    assert row["built"] is True
    assert row["snapshot_id"] == "abc123"
    assert row["counts"]["threads"] == 5183


def test_a_built_but_unstamped_home_still_reports_what_it_holds(tmp_path) -> None:
    # The haystack corpora are built and never stamped. Without the build marker
    # a real corpus reads as an empty one, which is the same as saying it is not
    # there.
    home = tmp_path / "homes" / "hay-locomo"
    _tree(tmp_path, {"homes/hay-locomo/haystack_corpus.json": json.dumps(
        {"benchmark": "locomo", "docs": 5880, "vectors": True})})

    row = inventory._home_row(home, label="corpus")
    assert row["counts"] == {}
    assert row["build"] == {"docs": 5880, "embedded": True}


def test_an_unbuilt_home_is_a_row_rather_than_an_absence(tmp_path) -> None:
    # "Available but not installed" is the question the page exists to answer, so
    # an unbuilt corpus must be distinguishable from one nobody defined.
    row = inventory._home_row(tmp_path / "homes" / "arguana", label="corpus")
    assert row["built"] is False
    assert row["snapshot_id"] is None
    assert "bytes" not in row


def test_a_workspace_root_counts_its_homes(tmp_path) -> None:
    _tree(tmp_path, {f"homes/locomo/q{i}/index.db": "x" for i in range(3)})
    row = inventory._workspace_row(tmp_path / "homes" / "locomo", label="per-question")
    assert row["homes"] == 3


# ── benchmark rows ───────────────────────────────────────────────────────────

def test_no_corpus_outranks_stale() -> None:
    # A row whose corpus is absent cannot run at all; reporting it as stale would
    # suggest a re-run is what it needs.
    assert inventory._state(fresh=True, corpus_built=False, last={"at": "x"}) == "missing"
    assert inventory._state(fresh=True, corpus_built=True, last={"at": "x"}) == "fresh"
    assert inventory._state(fresh=False, corpus_built=True, last={"at": "x"}) == "stale"
    assert inventory._state(fresh=False, corpus_built=True, last=None) == "never-run"


def test_every_benchmark_row_in_the_manifest_is_reported() -> None:
    from search_lab import benchmark

    names = {row["name"] for row in inventory.benchmarks()}
    assert names == {row.name for row in benchmark.manifest()}


def test_a_row_carries_its_corpus_and_a_state_the_page_can_render() -> None:
    rows = inventory.benchmarks()
    assert rows, "the manifest is never empty"
    for row in rows:
        assert row["state"] in {"missing", "fresh", "stale", "never-run"}
        assert isinstance(row["measure_keys"], list) and row["measure_keys"]


def test_a_dataset_is_indexed_to_the_bench_rows_that_run_on_it() -> None:
    # Derived from the manifest's own row names, so a row added there joins its
    # dataset without an edit to the inventory.
    index = inventory._bench_index()
    assert "beir:scifact[lexical]" in index.get("scifact", [])
    assert index.get("cdr") == ["cdr[vectors]"]


# ── datasets ─────────────────────────────────────────────────────────────────

def test_every_beir_dataset_the_harness_supports_is_listed() -> None:
    # The un-downloaded ones are exactly the "available" half of the question, so
    # the catalog is the harness's reference table rather than what is on disk.
    from search_lab import beir_eval

    beir = {d["name"] for d in inventory.datasets() if d["family"] == "beir"}
    assert beir == set(beir_eval.REFERENCE)


def test_every_dataset_family_carries_a_description() -> None:
    families = {d["family"] for d in inventory.datasets()}
    assert families <= set(inventory.FAMILIES)
    assert families == set(inventory.FAMILIES)


def test_a_dataset_row_carries_the_published_reference_it_is_read_against() -> None:
    scifact = next(d for d in inventory.datasets() if d["name"] == "scifact")
    assert scifact["reference"]["metric"] == "nDCG@10"
    assert scifact["reference"]["bm25"] > 0


# ── the run ledger ───────────────────────────────────────────────────────────

def _ledger(home, records: list[dict]) -> None:
    """Write a bench-run ledger under ``home``, oldest line first — the shape the
    recorder appends and the reader reverses."""
    from search_lab import bench_runs

    for record in records:
        bench_runs.record_run(
            row=record["row"], argv=record.get("argv", ["x"]),
            corpus_id=record.get("corpus_id"), measures=record.get("measures", {}),
            elapsed_s=record.get("elapsed_s", 1.0), status=record.get("status", "ok"),
            code=record.get("code_id", "cafe0000"), home=home)


def test_the_ledger_keeps_what_the_summary_has_to_drop(tmp_path) -> None:
    # The three things a newest-successful-per-row summary cannot show, each the
    # only record of something: the pass a delta is read against, a row that
    # stopped being runnable, and a row the manifest no longer names.
    from search_lab import benchmark

    on_bench = benchmark.manifest()[0].name
    _ledger(tmp_path, [
        {"row": on_bench, "code_id": "old00000", "measures": {"ndcg10": 0.5}},
        {"row": on_bench, "code_id": "new00000", "measures": {"ndcg10": 0.6}},
        {"row": "gone:row", "status": "failed"},
    ])

    payload = inventory.runs(home=tmp_path)
    assert payload["total"] == 3 and len(payload["runs"]) == 3
    assert [r["row"] for r in payload["runs"]] == ["gone:row", on_bench, on_bench]
    assert [r["status"] for r in payload["runs"]][0] == "failed"
    # Both passes of the bench row survive, and only the newer one is the one the
    # summary above reports.
    passes = [r for r in payload["runs"] if r["row"] == on_bench]
    assert [r["current"] for r in passes] == [True, False]


def test_a_row_off_the_manifest_is_marked_rather_than_dropped(tmp_path) -> None:
    _ledger(tmp_path, [{"row": "gone:row", "measures": {"mrr": 0.4}}])

    row = inventory.runs(home=tmp_path)["runs"][0]
    assert row["on_bench"] is False
    # None, not False: there is no current code id for a harness the manifest no
    # longer names, and "measured under different code" would be inventing one.
    assert row["code_current"] is None
    # It declares no headline metrics either, so what it recorded is what there is.
    assert row["measure_keys"] == ["mrr"]


def test_a_run_knows_whether_it_measured_the_code_in_the_tree(tmp_path) -> None:
    # The only state under which a recorded number still describes what a run
    # today would produce — which is the whole basis of the bench's skip test.
    from search_lab import benchmark

    row = benchmark.manifest()[0]
    _ledger(tmp_path, [
        {"row": row.name, "code_id": row.code_id()},
        {"row": row.name, "code_id": "stale000"},
    ])

    by_code = {r["code_id"]: r["code_current"] for r in inventory.runs(home=tmp_path)["runs"]}
    assert by_code[row.code_id()] is True
    assert by_code["stale000"] is False


def test_every_run_is_addressable_and_the_id_survives_a_reread(tmp_path) -> None:
    from search_lab import bench_runs

    _ledger(tmp_path, [{"row": "a:row"}, {"row": "b:row"}, {"row": "c:row"}])

    first = inventory.runs(home=tmp_path)["runs"]
    ids = [r["id"] for r in first]
    assert len(set(ids)) == 3, "a link has to reach one run, not a set of them"
    # Appending does not renumber what is already there: the id is a hash of the
    # record, so a link into the history keeps working as the ledger grows.
    _ledger(tmp_path, [{"row": "d:row"}])
    assert [r["id"] for r in inventory.runs(home=tmp_path)["runs"]][1:] == ids
    assert bench_runs.run_id(first[0]) != ids[0], "the id is not part of what it hashes"


def test_a_bounded_read_says_what_it_was_cut_from(tmp_path) -> None:
    # The ledger is append-only and never pruned, so a read of it has to be
    # bounded — and a truncated history reported as the whole one would say the
    # bench has run five times when it has run five hundred.
    _ledger(tmp_path, [{"row": f"row:{i}"} for i in range(6)])

    payload = inventory.runs(home=tmp_path, limit=2)
    assert payload["total"] == 6 and payload["returned"] == 2
    assert len(payload["runs"]) == 2


def test_the_ledger_can_be_read_for_one_row(tmp_path) -> None:
    _ledger(tmp_path, [{"row": "a:row"}, {"row": "b:row"}, {"row": "a:row"}])

    payload = inventory.runs(home=tmp_path, row="a:row")
    assert {r["row"] for r in payload["runs"]} == {"a:row"}
    assert payload["total"] == 2


def test_an_empty_ledger_is_a_state_not_an_error(tmp_path) -> None:
    payload = inventory.runs(home=tmp_path)
    assert payload["runs"] == [] and payload["total"] == 0
    json.dumps(payload)


# ── per-query detail ─────────────────────────────────────────────────────────

def _q(qid, *, rank, n_gold=1, found=0, ndcg10=0.0, latency=100.0):
    return {"qid": qid, "query": f"query {qid}", "latency_ms": latency,
            "rank": rank, "n_gold": n_gold, "found": found,
            "measures": {"ndcg10": ndcg10}}


def test_the_worst_served_queries_lead(tmp_path) -> None:
    # Ordered by what went wrong, not by score: a query whose gold never came
    # back is worse than one that ranked it 40th, which is worse than one that
    # ranked it 3rd — and a score of 0.0 reports the first two identically.
    from search_lab import bench_runs

    bench_runs.write_queries("aaaaaaaaaaaa", [
        _q("found", rank=3, found=1, ndcg10=0.7),
        _q("miss", rank=None),
        _q("deep", rank=40, found=1, ndcg10=0.1),
    ], home=tmp_path)

    payload = inventory.queries("aaaaaaaaaaaa", home=tmp_path)
    assert [r["qid"] for r in payload["rows"]] == ["miss", "deep", "found"]
    assert payload["misses"] == 1 and payload["total"] == 3
    # The metric read off the harness's own ordering, not a catalog here.
    assert payload["lead"] == "ndcg10"


def test_comparing_two_runs_lists_what_moved_worst_first(tmp_path) -> None:
    """The reason the detail is kept at all. Two configurations whose aggregate
    differs by a little have usually moved a lot on a few queries, and which few
    is the whole content of the change — an aggregate cannot say it, and a re-run
    cannot recover it, because the earlier configuration is gone."""
    from search_lab import bench_runs

    bench_runs.write_queries("bbbbbbbbbbbb", [
        _q("same", rank=1, found=1, ndcg10=0.9),
        _q("gained", rank=2, found=1, ndcg10=0.8),
        _q("lost", rank=None, ndcg10=0.0),
    ], home=tmp_path)
    bench_runs.write_queries("cccccccccccc", [
        _q("same", rank=1, found=1, ndcg10=0.9),
        _q("gained", rank=9, found=1, ndcg10=0.3),
        _q("lost", rank=2, found=1, ndcg10=0.85),
    ], home=tmp_path)

    payload = inventory.queries("bbbbbbbbbbbb", vs="cccccccccccc", home=tmp_path)
    assert payload["order"] == "moved" and payload["compared_to"] == "cccccccccccc"
    # Biggest regression first, and the query that did not move is absent — a
    # table led by hundreds of unchanged rows buries the ones carrying the change.
    assert [r["qid"] for r in payload["rows"]] == ["lost", "gained"]
    assert payload["rows"][0]["moved"] == -0.85
    # And what it was before, so the movement is readable as a rank change too.
    assert payload["rows"][0]["before"]["rank"] == 2


def test_a_query_only_one_run_scored_is_not_a_regression(tmp_path) -> None:
    # Reporting it as a fall from nothing would put the loudest rows in the table
    # on the queries that moved least.
    from search_lab import bench_runs

    bench_runs.write_queries("dddddddddddd", [_q("a", rank=1, ndcg10=0.5),
                                              _q("new", rank=1, ndcg10=0.5)],
                             home=tmp_path)
    bench_runs.write_queries("eeeeeeeeeeee", [_q("a", rank=1, ndcg10=0.5)],
                             home=tmp_path)

    payload = inventory.queries("dddddddddddd", vs="eeeeeeeeeeee", home=tmp_path)
    assert [r["qid"] for r in payload["rows"]] == []


def test_a_run_says_whether_its_detail_is_still_on_disk(tmp_path) -> None:
    # The store is capped, so an old run keeps its numbers and loses its detail —
    # and the page has to be able to say which rather than link to nothing.
    from search_lab import bench_runs

    _ledger(tmp_path, [{"row": "a:row"}, {"row": "b:row"}])
    runs = inventory.runs(home=tmp_path)["runs"]
    assert [r["has_queries"] for r in runs] == [False, False]

    bench_runs.write_queries(runs[0]["id"], [_q("x", rank=1)], home=tmp_path)
    assert [r["has_queries"] for r in inventory.runs(home=tmp_path)["runs"]] == [True, False]


def test_a_bounded_query_read_says_what_it_was_cut_from(tmp_path) -> None:
    from search_lab import bench_runs

    bench_runs.write_queries("ffffffffffff",
                             [_q(str(i), rank=None) for i in range(10)], home=tmp_path)

    payload = inventory.queries("ffffffffffff", limit=3, home=tmp_path)
    assert payload["total"] == 10 and payload["returned"] == 3
    assert len(payload["rows"]) == 3


def test_a_run_with_no_detail_reads_as_empty_not_an_error(tmp_path) -> None:
    payload = inventory.queries("999999999999", home=tmp_path)
    assert payload["rows"] == [] and payload["total"] == 0 and payload["lead"] is None
    json.dumps(payload)


# ── the assembled payload ────────────────────────────────────────────────────

def test_the_payload_is_json_serializable_and_carries_every_section() -> None:
    # It is served straight to the viewer, so a Path or a set anywhere in it is a
    # 500 rather than a page.
    payload = inventory.inventory()
    assert set(payload) >= {"cache_root", "cache", "code_id", "families",
                            "benchmarks", "datasets"}
    json.dumps(payload)


def test_the_payload_carries_no_scored_case_files() -> None:
    # The page inventories instruments, not labels. A section of case files with
    # case counts beside them reads as a measurement surface, which is exactly the
    # claim nothing on this archive's own corpus can make.
    assert "gold" not in inventory.inventory()
