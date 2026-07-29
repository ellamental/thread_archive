"""BEIR calibration harness — run archive's retrieval stack over a public IR
benchmark and compare to published numbers.

The in-house harness (``retrieval_eval.py``) answers *is search over the real
archive good* — mined query logs, labeled cases, behavioral signals. This one
answers the different, external question: *are the components embarrassing?* It
loads a standard BEIR dataset (a fixed corpus + queries + human relevance
judgments), ingests the corpus into a throwaway archive, runs every query
through the real ``api.search`` pipeline, and scores the ranking with the field's
standard metrics (nDCG@10, Recall@10/100, MRR@10) — the same metrics every public
retriever reports, so archive's stack lands next to a known leaderboard instead
of a number with no scale. BM25 and strong zero-shot dense reference values from
the BEIR paper are printed alongside, so "not embarrassing" is judgeable at a
glance.

What it measures is the *components* — the FTS5/BM25 lexical arm, the weighted
ranker, and (with ``--vectors``) the semantic
arms — on out-of-domain scientific/argument text that looks nothing like a
conversation archive. A BM25-competitive lexical number means the lexical
plumbing is sound; it says nothing about conversational-archive quality, which
only the in-house harness can. Treat the two as complementary tiers.

Never touches the real archive: ``search_lab.eval_home`` refuses any home that
overlaps one. Both halves of the cost are **cached** under ``--data-dir``
(default ``~/.cache/thread-evals``, the root every benchmark's downloads and homes
share): the downloaded dataset, and — keyed by the corpus the run asked for — the
built archive itself (the SQLite/FTS index *and* the embeddings). So the first
``--vectors`` run pays the ~15-min CPU embed once; every run after reuses it.
``--rebuild`` discards a cached build; ``--fresh`` opts out of caching entirely
(throwaway home, deleted on exit).

    # fast lexical baseline (the always-on floor); ingests the corpus once, then cached:
    .venv/bin/python search_lab/beir_eval.py --dataset scifact
    .venv/bin/python search_lab/beir_eval.py --dataset nfcorpus

    # add the semantic arm; add the cross-encoder head re-rank (slow: loads torch).
    # the embed pass is cached, so the second of these reuses the first's vectors:
    .venv/bin/python search_lab/beir_eval.py --dataset scifact --vectors
    .venv/bin/python search_lab/beir_eval.py --dataset scifact --vectors

nfcorpus (3.6k docs) and scifact (5.2k docs) are the standard small smoke sets.
Larger sets work but the per-doc import and (with ``--vectors``) the embed pass
scale with corpus size — and are what the cache exists to avoid repeating.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
# The lab dir too, so bare sibling imports (eval_home, eval_core, …) resolve
# however this file was loaded: as a script, by path, or as search_lab.X.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_core  # noqa: E402
import eval_home  # noqa: E402

from thread_archive._retrieval import _probe  # noqa: E402

BEIR_URL ="https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{name}.zip"

# Published reference points (nDCG@10) so the archive number has a scale.
# ``bm25`` is Anserini BM25 from the BEIR paper (Thakur et al., 2021, Table 2) —
# the fair yardstick for archive's lexical arm. ``dense`` is a representative
# strong zero-shot dense retriever (Contriever / E5-class, rounded) — the band a
# good semantic arm reaches, not a hard target. Values are approximate; they
# exist to answer "same ballpark?", not to certify a delta.
REFERENCE = {
    "scifact":     {"bm25": 0.665, "dense": 0.68},
    "nfcorpus":    {"bm25": 0.325, "dense": 0.33},
    "scidocs":     {"bm25": 0.158, "dense": 0.20},
    "trec-covid":  {"bm25": 0.656, "dense": 0.60},
    "fiqa":        {"bm25": 0.236, "dense": 0.30},
    "quora":       {"bm25": 0.789, "dense": 0.85},
    "dbpedia-entity": {"bm25": 0.313, "dense": 0.40},
    "fever":       {"bm25": 0.753, "dense": 0.75},
    "hotpotqa":    {"bm25": 0.603, "dense": 0.63},
    "nq":          {"bm25": 0.329, "dense": 0.50},
    "webis-touche2020": {"bm25": 0.367, "dense": 0.20},
    "climate-fever": {"bm25": 0.213, "dense": 0.24},
}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _read_json(path: Path):
    """The JSON at ``path``, or None if it isn't there / is unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- dataset load

def fetch_dataset(name: str, data_dir: Path) -> Path:
    """Download + unzip a BEIR dataset into ``data_dir`` (cached). Returns its dir."""
    dest = data_dir / name
    if (dest / "corpus.jsonl").exists() and (dest / "queries.jsonl").exists():
        return dest
    data_dir.mkdir(parents=True, exist_ok=True)
    zip_path = data_dir / f"{name}.zip"
    url = BEIR_URL.format(name=name)
    _log(f"downloading {url} ...")
    urllib.request.urlretrieve(url, zip_path)
    _log(f"extracting {zip_path.name} ...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(data_dir)
    zip_path.unlink()
    if not (dest / "corpus.jsonl").exists():
        raise SystemExit(f"unexpected BEIR layout: {dest} has no corpus.jsonl")
    return dest


def load_corpus(path: Path):
    """Yield ``(doc_id, text)`` from a BEIR corpus.jsonl (title folded into text)."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            title = (row.get("title") or "").strip()
            body = (row.get("text") or "").strip()
            text = f"{title}\n\n{body}" if title else body
            yield str(row["_id"]), text


def load_queries(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            out[str(row["_id"])] = (row.get("text") or "").strip()
    return out


def load_qrels(path: Path) -> dict[str, dict[str, int]]:
    """Parse a BEIR qrels TSV (``query-id  corpus-id  score``, with header)."""
    qrels: dict[str, dict[str, int]] = {}
    with path.open(encoding="utf-8") as fh:
        header = fh.readline()  # query-id\tcorpus-id\tscore
        if "query-id" not in header:
            fh.seek(0)  # no header — rewind
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            qid, did, score = parts[0], parts[1], parts[2]
            gain = int(float(score))
            if gain <= 0:
                continue
            qrels.setdefault(qid, {})[did] = gain
    return qrels


# ------------------------------------------------------------------- ingestion

def _session_lines(doc_id: str, text: str) -> list[dict]:
    """One doc as a one-turn claude-code session — its own thread. ``cwd`` is made
    distinct per doc so continuation detection never merges two docs into one."""
    return [
        {"type": "user", "uuid": f"beir-{doc_id}", "timestamp": "2026-01-01T10:00:00Z",
         "cwd": f"/beir/{doc_id}",
         "message": {"role": "user", "content": text}},
    ]


def ingest_corpus(corpus_path: Path, work: Path, max_docs: int | None,
                  label: str | None = None) -> dict[str, str]:
    """Import every corpus doc as its own thread. Returns ``{thread_id: doc_id}``
    with string thread-id keys (they survive a JSON round-trip to the cache, and
    match ``str(hit['thread_id'])`` at scoring time regardless of the id's type).

    ``label`` names the corpus in the load ledger. It defaults to the dataset
    directory a BEIR download lives in, which is the right answer only for that
    layout — a harness whose corpora sit side by side in one directory
    (``mtrag_eval``) passes its own, so ``thread-archive loads`` distinguishes a
    stalled ``clapnq`` build from a stalled ``govt`` one."""
    import logging

    from thread_archive import _api as api
    from thread_archive._ops.load_runs import load_run

    # Each doc is a first-time import under its own source id, so the importer's
    # per-source cursor bookkeeping (rewind/re-import notices) is just noise here.
    logging.getLogger("thread_archive._importers._cursor").setLevel(logging.ERROR)

    # One cheap line-count pass buys the phase a total, i.e. an ETA.
    with corpus_path.open(encoding="utf-8") as fh:
        total = sum(1 for line in fh if line.strip())
    if max_docs:
        total = min(total, max_docs)

    doc_of_thread: dict[str, str] = {}
    n = 0
    t0 = time.monotonic()
    # Tracked like any archive load: live progress/ETA to <home>/load-state.json,
    # a ledger row after — a corpus build is as visible as a real import.
    with load_run("import",
                  note=f"corpus build ({label or corpus_path.parent.name})") as run:
        with run.phase("import", total=total) as ph:
            for doc_id, text in load_corpus(corpus_path):
                # A distinct source path + id per doc: a reused path reads as the same
                # session being edited (cursor rewind), collapsing every doc into one
                # thread. Unlink after import so the work dir doesn't hold the whole corpus.
                f = work / f"doc-{n}.jsonl"
                f.write_text(
                    "\n".join(json.dumps(x) for x in _session_lines(doc_id, text)) + "\n",
                    encoding="utf-8",
                )
                res = api.import_path(f, source_id=f"beir-{doc_id}")
                f.unlink()
                doc_of_thread[str(res.thread_id)] = doc_id
                n += 1
                ph.advance()
                if n % 500 == 0:
                    rate = n / (time.monotonic() - t0)
                    _log(f"  ingested {n} docs ({rate:.0f}/s)")
                if max_docs and n >= max_docs:
                    break
    _log(f"ingested {n} docs in {time.monotonic() - t0:.0f}s")
    return doc_of_thread


# --------------------------------------------------------------------- metrics

def dcg(gains: list[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def score_run(ranked_doc_ids: list[str], rel: dict[str, int], ks: tuple[int, ...]) -> dict:
    """Standard IR metrics for one query's ranking against its judgments."""
    total_rel = len(rel)
    gains = [rel.get(d, 0) for d in ranked_doc_ids]

    # nDCG@10 (BEIR headline).
    idcg = dcg(sorted(rel.values(), reverse=True)[:10])
    ndcg10 = dcg(gains[:10]) / idcg if idcg else 0.0

    # MRR@10.
    mrr10 = 0.0
    for i, d in enumerate(ranked_doc_ids[:10]):
        if d in rel:
            mrr10 = 1.0 / (i + 1)
            break

    recall = {}
    for k in ks:
        found = sum(1 for d in ranked_doc_ids[:k] if d in rel)
        recall[k] = found / total_rel if total_rel else 0.0

    return {"ndcg10": ndcg10, "mrr10": mrr10, "recall": recall}


# ------------------------------------------------------------------------- run

def run(args) -> int:
    cache_root = Path(args.data_dir).expanduser()
    # The built archive is cached, keyed by the corpus asked for, so the
    # (minutes-long) ingest and the (~15-min CPU) embed pass are paid once and
    # reused. --fresh opts into a throwaway home deleted on exit; --home pins a
    # location the caller manages; --rebuild discards a cached build and re-ingests.
    if args.fresh:
        home = Path(tempfile.mkdtemp(prefix="beir-archive-home-"))
    elif args.home:
        home = Path(args.home)
    else:
        home = cache_root / "homes" / eval_home.home_name(args.dataset,
                                                          max_docs=args.max_docs)
    home = eval_home.guard_home(home, what="BEIR corpus home")

    # Pin the home + arm switches BEFORE importing the package, so the store bakes
    # the right DSN and no model cold-loads unless asked.
    os.environ["THREAD_ARCHIVE_HOME"] = str(home)
    eval_home.pin_arms(vectors=args.vectors)

    data = fetch_dataset(args.dataset, cache_root)
    queries = load_queries(data / "queries.jsonl")
    qrels = load_qrels(data / "qrels" / "test.tsv")
    # Only queries with judgments in the test split are scorable.
    scorable = [(qid, queries[qid]) for qid in qrels if qid in queries and queries[qid]]
    scorable = eval_core.sample_queries(scorable, args.sample, key=lambda q: q[0])
    _log(f"{args.dataset}: {len(qrels)} judged queries, scoring {len(scorable)}")

    # Cache bookkeeping lives beside the archive: a build marker (which corpus,
    # how many docs, how many embedded) and the thread_id→doc_id map scoring needs.
    # The marker carries every field that decides *what* was ingested, so a
    # --max-docs smoke build can never read back as the full corpus.
    marker_path = home / "beir_build.json"
    docmap_path = home / "beir_docmap.json"
    corpus_key = {"dataset": args.dataset, "max_docs": args.max_docs}
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
        doc_of_thread = ingest_corpus(data / "corpus.jsonl", work, args.max_docs)
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
            # Incremental (anti-join on missing vectors): embeds the whole fresh
            # corpus, or fills a gap left by an interrupted earlier run.
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
    # Every query's own result, kept rather than only summed. The aggregate says
    # this row scores 0.579; only this says which queries it failed and whether
    # their answers were ranked badly or never retrieved at all.
    per_query: list[dict] = []

    # Hold the ranker still before the first scored query, exactly as the gold
    # bench does: the coherence re-rank otherwise lands partway through and splits
    # a run into cases ranked with it and cases ranked without.
    eval_home.warm()

    t0 = time.monotonic()
    for i, (qid, qtext) in enumerate(scorable):
        with _probe.install() as probe:
            s0 = time.monotonic()
            # group='none': every ranked hit as its own row. A flat doc-retrieval
            # benchmark must not fold cross-thread duplicate content (group='thread'
            # would hide a distinct gold doc that shares text with another); dedup to
            # one row per doc happens below, on doc_id.
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
        # Map thread hits back to BEIR doc ids, preserving rank order, deduped.
        # A hit whose doc id *is* the query id is dropped, matching BEIR's own
        # ``ignore_identical_ids`` default. On every dataset carried here it never
        # fires — queries and documents are disjoint id spaces. It exists because
        # the published protocol specifies it: on a set where the two share one id
        # space, every query retrieves itself at rank 1, that self-hit is never in
        # the qrels, and scoring it as a miss reports a number a full point of nDCG
        # below what the same ranking scores under the published protocol.
        ranked: list[str] = []
        seen: set[str] = set()
        for h in hits:
            d = doc_of_thread.get(str(h["thread_id"]))
            if d is not None and d not in seen and d != qid:
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
        # Progress on every configuration, not only the slow one: a row that
        # prints nothing for minutes is indistinguishable from a hung one, which
        # matters most when the bench is running as a set.
        if (i + 1) % 50 == 0:
            _log(f"  scored {i + 1}/{len(scorable)} queries "
                 f"({(i + 1) / (time.monotonic() - t0):.1f}/s)")

    n = len(scorable)
    ndcg10 = agg["ndcg10"] / n
    mrr10 = agg["mrr10"] / n
    recall = {k: agg["recall"][k] / n for k in ks}
    p50 = sorted(latencies)[n // 2] * 1000 if n else 0.0
    total_s = time.monotonic() - t0

    arms = eval_home.arm_labels(vectors=args.vectors)
    ref = REFERENCE.get(args.dataset, {})
    print()
    print(f"=== BEIR {args.dataset} — archive stack [{' + '.join(arms)}] ===")
    print(f"queries scored: {n}   corpus docs: {len(doc_of_thread)}   "
          f"query p50: {p50:.0f}ms   scoring: {total_s:.0f}s")
    print()
    print(f"  nDCG@10   {ndcg10:.3f}"
          + (f"    (BEIR BM25 {ref['bm25']:.3f}"
             + (f", strong dense ~{ref['dense']:.2f})" if "dense" in ref else ")")
             if "bm25" in ref else ""))
    print(f"  MRR@10    {mrr10:.3f}")
    for k in ks:
        print(f"  Recall@{k:<3} {recall[k]:.3f}")
    print()
    if "bm25" in ref:
        delta = ndcg10 - ref["bm25"]
        verdict = ("in BM25 ballpark" if abs(delta) < 0.05
                   else "ABOVE BM25" if delta > 0 else "BELOW BM25 — investigate")
        print(f"  vs BM25 reference: {delta:+.3f}  ({verdict})")
    print()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "dataset": args.dataset, "arms": arms, "n": n,
            "corpus_docs": len(doc_of_thread), "corpus_id": corpus_id,
            "ndcg10": ndcg10, "mrr10": mrr10,
            "recall": {str(k): recall[k] for k in ks},
            "query_p50_ms": p50, "reference": ref,
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
    ap.add_argument("--dataset", default="scifact",
                    help="BEIR dataset name (scifact, nfcorpus, scidocs, trec-covid, ...)")
    ap.add_argument("--data-dir", default=str(eval_home.CACHE_ROOT),
                    help="cache dir for downloaded datasets AND built archive homes "
                         "(persistent; shared with the other benchmarks)")
    ap.add_argument("--vectors", action="store_true",
                    help="build + query the semantic arm with the real embedder (needs [embeddings])")
    ap.add_argument("--max-docs", type=int, default=None,
                    help="cap ingested corpus docs (smoke runs)")
    ap.add_argument("--sample", type=int, default=None,
                    help="score a deterministic sample of this many queries instead of all (the quick tier; see search_lab.eval_core.sample_queries)")
    ap.add_argument("--home", default=None,
                    help="pin the archive home (default: a persistent per-dataset cache under --data-dir)")
    ap.add_argument("--rebuild", action="store_true",
                    help="discard the cached build and re-ingest (+ re-embed with --vectors)")
    ap.add_argument("--fresh", action="store_true",
                    help="use a throwaway home deleted on exit (no caching)")
    ap.add_argument("--json-out", default=None, help="also write the report as JSON")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
