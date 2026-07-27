"""CDR calibration harness — run archive's retrieval stack over the Conversational
Data Retrieval benchmark and compare to its published leaderboard.

Where ``beir_eval.py`` scores the components on out-of-domain *document* text (a
BM25-competitive number there means the lexical plumbing is sound, nothing more),
this harness scores them on the task archive actually does: **retrieving whole
conversations from a corpus of conversations.** CDR ("Finding Diamonds in
Conversation Haystacks: A Benchmark for Conversational Data Retrieval", EMNLP 2025
Industry Track) is 1,583 analytical queries over 9,146 chat logs with human
relevance judgments across five analytical areas (emotion, intent, dynamics,
trust/safety, linguistic style). Its headline finding is the scale this harness
borrows: across 16 leading embedding models the *best* reaches only NDCG@10
0.5036 — conversational retrieval is hard, and that number is the yardstick a
result here lands next to.

It ingests every CDR conversation as its own thread, runs every query through the
real ``api.search`` pipeline, and scores the ranking with the field's standard
metrics (nDCG@10, MRR@10, Recall@10/100) — the same shape ``beir_eval`` reports,
so the two are directly comparable across domains. Unlike BEIR's graded qrels,
CDR relevance is binary (a query's relevant-doc list), and a query carries ~20
relevant conversations on average, so Recall@10 is bounded well under 1 by
construction — nDCG@10 is the number to read.

The domain still isn't Ella's archive: CDR queries are product-insight analytics
("assistant explains cloud computing concepts", "user expresses frustration"),
not an agent re-finding its own past session. So a strong CDR number certifies
the stack is competitive at conversational retrieval *in general*; only the
in-house harness (``retrieval_eval.py``) speaks to quality on the real corpus.
Treat them as complementary tiers, exactly as BEIR is treated.

Never touches the real archive: it refuses the default ``~/.thread/archive`` and
runs against its own throwaway/cached home. The built archive (SQLite/FTS index
*and* embeddings) is cached under ``--data-dir`` keyed to the benchmark, so the
per-doc ingest and the (~15-min CPU) embed pass are paid once and reused; the CDR
data itself ships in the cloned repo (no download).

    # clone once (the benchmark data lives in the repo):
    #   git clone https://github.com/l-yohai/CDR-Benchmark ~/.cache/thread-evals/CDR-Benchmark

    # fast lexical baseline (ingests the corpus once, then cached):
    .venv/bin/python search_lab/cdr_eval.py

    # add the semantic arm; add the auto-gated cross-encoder (the production stack).
    # the embed pass is cached, so the second reuses the first's vectors:
    .venv/bin/python search_lab/cdr_eval.py --vectors
    .venv/bin/python search_lab/cdr_eval.py --vectors
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
# The lab dir too, so bare sibling imports (eval_home, eval_core, …) resolve
# however this file was loaded: as a script, by path, or as search_lab.X.
sys.path.insert(0, str(_HERE))

import eval_core  # noqa: E402
import eval_home  # noqa: E402

from thread_archive._retrieval import _probe  # noqa: E402

# Reuse beir_eval's generic, task-agnostic primitives so the two harnesses ingest
# and score off one code path (the same reason the mining package imports the
# shared scoring engine): `_session_lines` (a doc as a one-turn session), `score_run`
# (standard IR metrics for one ranking), and `dcg`. Loaded by path — search_lab/ is a
# script dir, not an importable package.
_SPEC = importlib.util.spec_from_file_location("beir_eval", _HERE / "beir_eval.py")
beir_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("beir_eval", beir_eval)
_SPEC.loader.exec_module(beir_eval)

_session_lines = beir_eval._session_lines
score_run = beir_eval.score_run
_log = beir_eval._log
_read_json = beir_eval._read_json

# The published leaderboard's ceiling (NDCG@10) — the best of the 16 embedding
# models CDR evaluates. Not a target: it exists to answer "same ballpark as the
# field?", the CDR analog of beir_eval's BM25 reference.
BEST_MODEL_NDCG10 = 0.5036

DEFAULT_REPO = eval_home.CACHE_ROOT / "CDR-Benchmark"
# The test split's MTEB-shaped files inside the repo.
_DATA_SUBPATH = Path("cdr_benchmark_data") / "test_dataset" / "data" / "test"


def load_cdr(repo: Path) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, int]]]:
    """Load CDR's test split: ``(corpus, queries, qrels)``.

    ``corpus`` maps ``corpus_<n>`` -> the conversation text (``user:``/``assistant:``
    turns, newline-joined). ``queries`` maps ``query_<n>`` -> the analytical query
    string. ``qrels`` maps a query id -> ``{corpus_id: 1}`` — CDR relevance is
    binary and stored as a (sometimes duplicate-carrying) list, so it is deduped
    into a gain-1 judgment set here."""
    data = repo / _DATA_SUBPATH
    corpus = json.loads((data / "corpus.json").read_text(encoding="utf-8"))
    queries = json.loads((data / "queries.json").read_text(encoding="utf-8"))
    relevant = json.loads((data / "relevant_docs.json").read_text(encoding="utf-8"))
    qrels = {qid: {did: 1 for did in dids} for qid, dids in relevant.items()}
    return corpus, queries, qrels


def ingest_corpus(corpus: dict[str, str], work: Path, max_docs: int | None) -> dict[str, str]:
    """Import every CDR conversation as its own thread. Returns ``{thread_id:
    corpus_id}`` with string keys (they survive the JSON round-trip to the cache
    and match ``str(hit['thread_id'])`` at scoring time). Mirrors
    ``beir_eval.ingest_corpus`` — a distinct source path + id per doc so the
    importer's continuation detection never merges two conversations into one
    thread; the temp file is unlinked after import so the work dir stays small."""
    import logging

    from thread_archive import _api as api
    from thread_archive._ops.load_runs import load_run

    logging.getLogger("thread_archive._importers._cursor").setLevel(logging.ERROR)

    doc_of_thread: dict[str, str] = {}
    n = 0
    t0 = time.monotonic()
    total = min(len(corpus), max_docs) if max_docs else len(corpus)
    # Tracked like any archive load: live progress/ETA while the ~9k docs ingest,
    # a ledger row after — the health page sees this build like a real import.
    with load_run("import", note="CDR corpus build") as run:
        with run.phase("import", total=total) as ph:
            for doc_id, text in corpus.items():
                f = work / f"doc-{n}.jsonl"
                f.write_text(
                    "\n".join(json.dumps(x) for x in _session_lines(doc_id, text)) + "\n",
                    encoding="utf-8",
                )
                res = api.import_path(f, source_id=f"cdr-{doc_id}")
                f.unlink()
                doc_of_thread[str(res.thread_id)] = doc_id
                n += 1
                ph.advance()
                if n % 1000 == 0:
                    _log(f"  ingested {n} docs ({n / (time.monotonic() - t0):.0f}/s)")
                if max_docs and n >= max_docs:
                    break
    _log(f"ingested {n} docs in {time.monotonic() - t0:.0f}s")
    return doc_of_thread


def run(args) -> int:
    repo = Path(args.cdr_repo).expanduser()
    if not (repo / _DATA_SUBPATH / "corpus.json").exists():
        raise SystemExit(
            f"CDR data not found under {repo}. Clone it first:\n"
            f"  git clone https://github.com/l-yohai/CDR-Benchmark {repo}"
        )

    cache_root = Path(args.data_dir).expanduser()
    if args.fresh:
        home = Path(tempfile.mkdtemp(prefix="cdr-archive-home-"))
    elif args.home:
        home = Path(args.home)
    else:
        home = cache_root / "homes" / eval_home.home_name("cdr",
                                                          max_docs=args.max_docs)
    home = eval_home.guard_home(home, what="CDR corpus home")

    # Pin the home + arm switches BEFORE importing the package, so the store bakes
    # the right DSN and no model cold-loads unless asked.
    os.environ["THREAD_ARCHIVE_HOME"] = str(home)
    eval_home.pin_arms(vectors=args.vectors)

    corpus, queries, qrels = load_cdr(repo)
    scorable = [(qid, queries[qid]) for qid in qrels if qid in queries and queries[qid]]
    if args.max_queries:
        scorable = scorable[: args.max_queries]
    _log(f"CDR: {len(qrels)} judged queries, {len(corpus)} conversations, "
         f"scoring {len(scorable)}")

    # The marker carries every field that decides *what* was ingested, so a
    # --max-docs smoke build can never read back as the full corpus.
    marker_path = home / "cdr_build.json"
    docmap_path = home / "cdr_docmap.json"
    corpus_key = {"benchmark": "cdr", "max_docs": args.max_docs}
    marker = _read_json(marker_path)
    need_ingest = (
        args.rebuild or eval_home.marker_stale(marker, corpus_key)
        or not docmap_path.exists()
    )
    if need_ingest:
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(parents=True, exist_ok=True)

    from thread_archive import _api as api

    api.open_archive(str(home))

    if need_ingest:
        work = home / "_ingest"
        work.mkdir(parents=True, exist_ok=True)
        doc_of_thread = ingest_corpus(corpus, work, args.max_docs)
        docmap_path.write_text(json.dumps(doc_of_thread), encoding="utf-8")
        marker = {**corpus_key, "corpus_docs": len(doc_of_thread), "embedded": 0}
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
    else:
        doc_of_thread = _read_json(docmap_path)
        _log(f"reusing cached archive at {home} ({len(doc_of_thread)} docs)")

    if args.vectors:
        if marker.get("embedded", 0) >= len(doc_of_thread) and not args.rebuild:
            _log(f"reusing cached embeddings ({marker['embedded']} vectors)")
        else:
            _log("embedding corpus (real model) ...")
            t0 = time.monotonic()
            res = api.embed()
            _log(f"embedded {res.get('embedded')} events in {time.monotonic() - t0:.0f}s")
            marker["embedded"] = len(doc_of_thread)
            marker_path.write_text(json.dumps(marker), encoding="utf-8")

    # The corpus's content fingerprint, so a recorded number binds to the corpus
    # that produced it rather than to a directory name a rebuild reuses.
    corpus_id = eval_home.stamp_corpus(home, restamp=need_ingest)

    ks = (10, 100)
    agg = {"ndcg10": 0.0, "mrr10": 0.0, "recall": {k: 0.0 for k in ks}}
    latencies: list[float] = []
    # The per-stage breakdown rides along for free: these are the same searches a
    # latency run would pay for again, so scoring without recording it throws
    # away a profile already produced. The probe is a context-local slot and a
    # search nobody measures never checks it, so installing one costs nothing.
    stage_samples: list[dict] = []
    # Every query's own result, kept rather than only summed — see beir_eval.
    # This row scores 1,583 conversational queries, and which of them the ranker
    # fails is not derivable from the mean of them.
    per_query: list[dict] = []

    # Hold the ranker still before the first scored query, exactly as the gold
    # bench does: the coherence re-rank otherwise lands partway through and splits
    # a run into cases ranked with it and cases ranked without.
    eval_home.warm()

    t0 = time.monotonic()
    for i, (qid, qtext) in enumerate(scorable):
        with _probe.install() as probe:
            s0 = time.monotonic()
            # group='none': a flat doc-retrieval benchmark scores every hit as its
            # own row; dedup to one row per conversation happens below, on corpus_id.
            hits = api.search(
                qtext, limit=max(ks) * 2, content_types=["user"],
                group="none",
            )
            elapsed = time.monotonic() - s0
        latencies.append(elapsed)
        if probe.ran:
            sample = probe.as_record()
            sample["total_ms"] = elapsed * 1000.0
            stage_samples.append(sample)
        ranked: list[str] = []
        seen: set[str] = set()
        for h in hits:
            d = doc_of_thread.get(str(h["thread_id"]))
            if d is not None and d not in seen:
                seen.add(d)
                ranked.append(d)
        m = score_run(ranked, qrels[qid], ks)
        rel = qrels[qid]
        per_query.append(eval_core.query_row(
            qid=qid, query=qtext, latency_s=elapsed, n_gold=len(rel),
            rank=next((i + 1 for i, d in enumerate(ranked) if d in rel), None),
            found=sum(1 for d in ranked[:10] if d in rel),
            measures={"ndcg10": m["ndcg10"], "mrr10": m["mrr10"],
                      "recall10": m["recall"][10], "recall100": m["recall"][100]}))
        agg["ndcg10"] += m["ndcg10"]
        agg["mrr10"] += m["mrr10"]
        for k in ks:
            agg["recall"][k] += m["recall"][k]
        # Progress on every configuration, not only the slow one: 1583 queries is
        # tens of minutes even lexically, and a row that prints nothing for that
        # long is indistinguishable from a hung one.
        if (i + 1) % 200 == 0:
            _log(f"  scored {i + 1}/{len(scorable)} queries "
                 f"({(i + 1) / (time.monotonic() - t0):.1f}/s)")

    n = len(scorable)
    ndcg10 = agg["ndcg10"] / n
    mrr10 = agg["mrr10"] / n
    recall = {k: agg["recall"][k] / n for k in ks}
    p50 = sorted(latencies)[n // 2] * 1000 if n else 0.0
    total_s = time.monotonic() - t0

    arms = eval_home.arm_labels(vectors=args.vectors)
    print()
    print(f"=== CDR — archive stack [{' + '.join(arms)}] ===")
    print(f"queries scored: {n}   corpus docs: {len(doc_of_thread)}   "
          f"query p50: {p50:.0f}ms   scoring: {total_s:.0f}s")
    print()
    print(f"  nDCG@10   {ndcg10:.4f}    (CDR best-of-16 models {BEST_MODEL_NDCG10:.4f})")
    print(f"  MRR@10    {mrr10:.4f}")
    for k in ks:
        print(f"  Recall@{k:<3} {recall[k]:.4f}")
    print()
    delta = ndcg10 - BEST_MODEL_NDCG10
    verdict = ("at the CDR frontier" if abs(delta) < 0.03
               else "ABOVE the best CDR model" if delta > 0
               else "below the best CDR model")
    print(f"  vs CDR best-of-16: {delta:+.4f}  ({verdict})")
    print()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "benchmark": "cdr", "arms": arms, "n": n,
            "corpus_docs": len(doc_of_thread), "corpus_id": corpus_id,
            "ndcg10": ndcg10, "mrr10": mrr10,
            "recall": {str(k): recall[k] for k in ks},
            "query_p50_ms": p50, "reference_best_ndcg10": BEST_MODEL_NDCG10,
            "per_query": per_query,
            "performance": eval_core.performance(
                latencies, stage_samples, scoring_s=total_s,
                corpus_docs=len(doc_of_thread), arms=arms),
        }, indent=2) + "\n", encoding="utf-8")
        _log(f"wrote {args.json_out}")

    if args.fresh:
        api.close()
        shutil.rmtree(home, ignore_errors=True)
    else:
        _log(f"cached archive home at {home} (reused next run; --rebuild to refresh)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cdr-repo", default=str(DEFAULT_REPO),
                    help="cloned CDR-Benchmark repo (holds the test-split data)")
    ap.add_argument("--data-dir", default=str(eval_home.CACHE_ROOT),
                    help="cache dir for the built archive homes (persistent; "
                         "shared with the other benchmarks)")
    ap.add_argument("--vectors", action="store_true",
                    help="build + query the semantic arm with the real embedder (needs [embeddings])")
    ap.add_argument("--max-docs", type=int, default=None,
                    help="cap ingested corpus docs (smoke runs)")
    ap.add_argument("--max-queries", type=int, default=None,
                    help="cap scored queries (smoke runs)")
    ap.add_argument("--home", default=None,
                    help="pin the archive home (default: a persistent cache under --data-dir)")
    ap.add_argument("--rebuild", action="store_true",
                    help="discard the cached build and re-ingest (+ re-embed with --vectors)")
    ap.add_argument("--fresh", action="store_true",
                    help="use a throwaway home deleted on exit (no caching)")
    ap.add_argument("--json-out", default=None, help="also write the report as JSON")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
