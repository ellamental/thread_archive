"""MTRAG calibration harness — the retrieval half of IBM's Multi-Turn RAG
benchmark, run through the real pipeline and scored beside its published table.

Every other shared-corpus yardstick on this bench asks one question per corpus.
MTRAG ships the **same information need in three query shapes**, which is the axis
nothing else here measures:

- ``lastturn`` — the final user turn alone, median 46 chars, and unresolvable
  without the conversation ("Is there a limit on the file size?").
- ``rewrite`` — a human rewrite of that turn into a standalone question, median
  59 chars ("What is the file size limit for attachments in IBM Cloudant?").
- ``questions`` — the whole user-side history concatenated, median 228 chars.

The gold is identical across all three. So the ``rewrite`` − ``lastturn`` gap is a
clean read on how much the stack depends on a well-formed query, measured against
a published baseline that reports the same pair. That matters here because the
usage ledger says real traffic is ~4 words of keyword soup while every authored
query set on this bench is a 20-word grammatical sentence — ``lastturn`` is the
only external row anywhere near the short end.

Four domains, retrieved from separately: ``clapnq`` (Wikipedia, 183,408 passages),
``cloud`` (IBM technical documentation, 72,442), ``fiqa`` (finance, 61,022) and
``govt`` (government, 49,607). ``--domain all`` (the default) scores every domain
and reports the **macro-average over domains**, which is the shape the published
table reports and therefore the only shape comparable to it; a single ``--domain``
scores one corpus and prints its reference struck through, because no per-domain
baseline is published.

Published nDCG@10 (Apache-2.0, github.com/IBM/mt-rag-benchmark), macro-averaged
over the four domains — BM25 0.21 / 0.25, BGE-base-1.5 0.30 / 0.38, Elser 0.49 /
0.54 for last-turn / rewrite respectively. These are low by BEIR standards
because the task is hard, which is the useful part: there is room to move.

One deviation from the files as shipped, and it is deliberate. Every query text
carries ``|user|:`` role markers — a serialization artifact of the conversation
export, not part of the information need — and they are stripped before the query
runs. A lexical arm treats the marker as a constant term across every query and
barely notices; a dense arm embeds it as content. Left in, the multi-turn
``questions`` variant would carry one marker per turn.

Never touches the real archive: ``search_lab.eval_home`` refuses any home that
overlaps one. Each domain's corpus is built once into
``~/.cache/thread-evals/homes/mtrag-<domain>`` and reused, so the ingest and the
embed pass are paid once across every query set and every later run.

    # the corpus is shared across query sets — the first of these builds it,
    # the rest reuse it:
    .venv/bin/python search_lab/mtrag_eval.py --queries lastturn --vectors
    .venv/bin/python search_lab/mtrag_eval.py --queries rewrite --vectors
    .venv/bin/python search_lab/mtrag_eval.py --domain cloud --queries rewrite
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
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

import dataset_pins  # noqa: E402
import eval_core  # noqa: E402
import eval_home  # noqa: E402

from thread_archive._retrieval import _probe  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("beir_eval", _HERE / "beir_eval.py")
beir_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("beir_eval", beir_eval)
_SPEC.loader.exec_module(beir_eval)

_log = beir_eval._log
_read_json = beir_eval._read_json
score_run = beir_eval.score_run

#: The four corpora, smallest first so a partial build leaves the cheap ones done.
DOMAINS = ("govt", "fiqa", "cloud", "clapnq")

QUERY_SETS = ("lastturn", "rewrite", "questions")

#: Published nDCG@10 from the benchmark's own README, macro-averaged over the four
#: domains. ``questions`` has no published row — the paper reports last-turn and
#: query-rewrite only — so a run of that variant lands with no reference, which is
#: the honest state rather than a borrowed number.
REFERENCE = {
    "lastturn": {"bm25": 0.21, "dense": 0.30, "best": 0.49},
    "rewrite": {"bm25": 0.25, "dense": 0.38, "best": 0.54},
}

#: ``|user|:`` / ``|assistant|:`` turn markers, stripped from every query.
_ROLE_MARKER = re.compile(r"\|(?:user|assistant|system)\|:\s*")


def strip_roles(text: str) -> str:
    """A query's text with its conversation role markers removed and whitespace
    collapsed to single spaces.

    The multi-turn ``questions`` variant separates turns with newlines, so the
    collapse is what keeps a four-turn query one line in the per-query report."""
    return " ".join(_ROLE_MARKER.sub(" ", text or "").split())


# ---------------------------------------------------------------- dataset load

def domain_paths(root: Path, domain: str) -> tuple[Path, Path, Path]:
    """``(corpus, qrels, task_dir)`` for one domain under the fetched tree."""
    tasks = root / "retrieval_tasks" / domain
    return root / "corpora" / f"{domain}.jsonl", tasks / "qrels" / "dev.tsv", tasks


def load_queries(path: Path) -> dict[str, str]:
    """``{qid: text}`` from one MTRAG query file, role markers stripped."""
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            text = strip_roles(row.get("text") or "")
            if text:
                out[str(row["_id"])] = text
    return out


def load_qrels(path: Path) -> dict[str, dict[str, int]]:
    """Parse MTRAG's BEIR-format qrels TSV.

    The corpus ids here resolve directly against the **passage-level** corpus —
    verified 578/578, 494/494, 535/535 and 521/521 across the four domains. The
    benchmark's README describes stripping two trailing offsets from a qrels id;
    that applies to the document-level corpus, and doing it here would map every
    judgment to nothing."""
    return beir_eval.load_qrels(path)


# ------------------------------------------------------------------- one domain

def build_domain(api_mod, home: Path, corpus_path: Path, domain: str, *,
                 vectors: bool, max_docs: int | None, rebuild: bool) -> dict[str, str]:
    """Ingest (and optionally embed) one domain's corpus into ``home``, cached.

    Returns the ``thread_id -> passage_id`` map scoring needs. The marker carries
    every field that decides what went in, so a ``--max-docs`` smoke build can
    never read back as the full corpus."""
    marker_path = home / "mtrag_build.json"
    docmap_path = home / "mtrag_docmap.json"
    corpus_key = {"domain": domain, "max_docs": max_docs}
    marker = _read_json(marker_path)
    need_ingest = (rebuild or eval_home.marker_stale(marker, corpus_key)
                   or not docmap_path.exists())

    if need_ingest:
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(parents=True, exist_ok=True)

    os.environ["THREAD_ARCHIVE_HOME"] = str(home)
    api_mod.open_archive(str(home))

    if need_ingest:
        work = home / "_ingest"
        work.mkdir(parents=True, exist_ok=True)
        doc_of_thread = beir_eval.ingest_corpus(corpus_path, work, max_docs,
                                                label=f"mtrag/{domain}")
        shutil.rmtree(work, ignore_errors=True)
        docmap_path.write_text(json.dumps(doc_of_thread), encoding="utf-8")
        marker = {**corpus_key, "corpus_docs": len(doc_of_thread), "embedded": 0}
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
    else:
        doc_of_thread = _read_json(docmap_path)
        _log(f"  reusing cached {domain} archive ({len(doc_of_thread)} passages)")

    if vectors:
        if marker.get("embedded", 0) >= len(doc_of_thread) and not rebuild:
            _log(f"  reusing cached {domain} embeddings ({marker['embedded']} vectors)")
        else:
            _log(f"  embedding {domain} (real model) ...")
            t0 = time.monotonic()
            res = api_mod.embed()
            _log(f"  embedded {res.get('embedded')} events in {time.monotonic() - t0:.0f}s")
            marker["embedded"] = len(doc_of_thread)
            marker_path.write_text(json.dumps(marker), encoding="utf-8")

    eval_home.stamp_corpus(home, restamp=need_ingest)
    return doc_of_thread


def score_domain(api_mod, doc_of_thread: dict[str, str], queries: dict[str, str],
                 qrels: dict[str, dict[str, int]], *, domain: str,
                 ks: tuple[int, ...]) -> tuple[list[dict], list[float], list[dict], list[dict]]:
    """Run every judged query of one domain and score it.

    Returns ``(metric_rows, latencies, stage_samples, per_query)``. The corpus
    graph is warmed with the home swap flagged, because this harness opens one
    home per domain inside a single process."""
    eval_home.warm(swapped_home=True)

    scorable = [(qid, queries[qid]) for qid in qrels if qid in queries]
    scorable.sort()
    rows: list[dict] = []
    latencies: list[float] = []
    stage_samples: list[dict] = []
    per_query: list[dict] = []
    t0 = time.monotonic()
    for i, (qid, qtext) in enumerate(scorable):
        with _probe.install() as probe:
            s0 = time.monotonic()
            # Every ranked hit as its own row, for the same reason BEIR needs it:
            # this is flat passage retrieval, and folding hits by thread would hide
            # a distinct gold passage that shares text with another.
            hits = api_mod.search(qtext, limit=max(ks) * 2, content_types=["user"])
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
        rel = qrels[qid]
        m = score_run(ranked, rel, ks)
        m["domain"] = domain
        rows.append(m)
        per_query.append(eval_core.query_row(
            qid=qid, query=qtext, latency_s=elapsed, n_gold=len(rel),
            rank=next((j + 1 for j, d in enumerate(ranked) if d in rel), None),
            found=sum(1 for d in ranked[:10] if d in rel), group=domain,
            measures={"ndcg10": m["ndcg10"], "mrr10": m["mrr10"],
                      "recall10": m["recall"][10], "recall100": m["recall"][100]}))
        if (i + 1) % 100 == 0:
            _log(f"    scored {i + 1}/{len(scorable)} "
                 f"({(i + 1) / (time.monotonic() - t0):.1f}/s)")
    return rows, latencies, stage_samples, per_query


def mean_metrics(rows: list[dict], ks: tuple[int, ...]) -> dict:
    """Mean of each metric over ``rows`` (empty -> zeros)."""
    n = len(rows) or 1
    return {
        "n": len(rows),
        "ndcg10": sum(r["ndcg10"] for r in rows) / n,
        "mrr10": sum(r["mrr10"] for r in rows) / n,
        "recall": {k: sum(r["recall"][k] for r in rows) / n for k in ks},
    }


# ------------------------------------------------------------------------- run

def run(args) -> int:
    root = Path(args.data_dir).expanduser() / "mtrag"
    if not (root / "corpora").is_dir():
        raise SystemExit(
            f"MTRAG data not found at {root}. Fetch the passage-level corpora and "
            f"retrieval tasks from github.com/IBM/mt-rag-benchmark into "
            f"{root}/corpora/<domain>.jsonl and {root}/retrieval_tasks/<domain>/.")
    dataset_pins.verify("mtrag")

    domains = list(DOMAINS) if args.domain == "all" else [args.domain]
    ks = (10, 100)

    homes_root = (Path(tempfile.mkdtemp(prefix="mtrag-homes-")) if args.fresh
                  else Path(args.data_dir).expanduser() / "homes")
    homes_root = eval_home.guard_home(homes_root, what="MTRAG home root")

    eval_home.pin_arms(vectors=args.vectors)

    from thread_archive import _api as api

    all_rows: list[dict] = []
    per_domain: dict[str, dict] = {}
    latencies: list[float] = []
    stage_samples: list[dict] = []
    per_query: list[dict] = []
    corpus_ids: list[str] = []
    total_docs = 0

    _log(f"mtrag: {len(domains)} domain(s) {domains}, queries={args.queries}, "
         f"vectors={args.vectors}")
    t0 = time.monotonic()
    for domain in domains:
        corpus_path, qrels_path, tasks = domain_paths(root, domain)
        query_path = tasks / f"{domain}_{args.queries}.jsonl"
        for path in (corpus_path, qrels_path, query_path):
            if not path.exists():
                raise SystemExit(f"missing {path}")
        queries = load_queries(query_path)
        qrels = load_qrels(qrels_path)
        # Sampled *per domain*, not across the pooled query set: the headline is a
        # macro-average that weights every domain equally, so a draw that happened
        # to take 90 clapnq queries and 10 govt ones would leave one quarter of
        # the reported number resting on ten queries.
        if args.sample:
            per_domain_n = max(1, -(-args.sample // len(domains)))
            keep = set(eval_core.sample_queries(
                sorted(qrels), per_domain_n, key=lambda qid: qid))
            qrels = {qid: rel for qid, rel in qrels.items() if qid in keep}
        _log(f"  {domain}: {len(qrels)} judged queries")

        home = eval_home.guard_home(
            homes_root / f"mtrag-{eval_home.home_name(domain, max_docs=args.max_docs)}",
            what="MTRAG corpus home")
        doc_of_thread = build_domain(api, home, corpus_path, domain,
                                     vectors=args.vectors, max_docs=args.max_docs,
                                     rebuild=args.rebuild)
        total_docs += len(doc_of_thread)
        snap = _read_json(home / "snapshot.json") or {}
        if snap.get("snapshot_id"):
            corpus_ids.append(snap["snapshot_id"])

        rows, lat, stages, pq = score_domain(api, doc_of_thread, queries, qrels,
                                             domain=domain, ks=ks)
        all_rows += rows
        latencies += lat
        stage_samples += stages
        per_query += pq
        per_domain[domain] = mean_metrics(rows, ks)
        api.close()

    total_s = time.monotonic() - t0

    # The published table is a macro-average over the four domains — each domain
    # weighted equally regardless of how many queries it carries — so that is what
    # a comparable headline has to be. The micro-average over all queries is
    # reported beside it rather than instead of it, because with 180–208 queries
    # per domain the two barely differ and a reader should be able to see that.
    macro = {
        "n_domains": len(per_domain),
        "n": sum(d["n"] for d in per_domain.values()),
        "ndcg10": sum(d["ndcg10"] for d in per_domain.values()) / max(1, len(per_domain)),
        "mrr10": sum(d["mrr10"] for d in per_domain.values()) / max(1, len(per_domain)),
        "recall": {k: sum(d["recall"][k] for d in per_domain.values())
                   / max(1, len(per_domain)) for k in ks},
    }
    micro = mean_metrics(all_rows, ks)

    arms = eval_home.arm_labels(vectors=args.vectors)
    ref = REFERENCE.get(args.queries, {})
    comparable = args.domain == "all" and not args.max_docs and not args.sample
    print()
    print(f"=== MTRAG {args.queries} — archive stack [{' + '.join(arms)}] ===")
    print(f"domains: {'+'.join(domains)}   queries scored: {macro['n']}   "
          f"passages: {total_docs}   elapsed: {total_s:.0f}s")
    print()
    print(f"  nDCG@10 (macro)  {macro['ndcg10']:.3f}")
    print(f"  nDCG@10 (micro)  {micro['ndcg10']:.3f}")
    print(f"  MRR@10  (macro)  {macro['mrr10']:.3f}")
    for k in ks:
        print(f"  Recall@{k:<4}(macro) {macro['recall'][k]:.3f}")
    print()
    if ref:
        print(f"  published (4-domain macro): BM25 {ref['bm25']:.2f}   "
              f"BGE-base-1.5 {ref['dense']:.2f}   Elser {ref['best']:.2f}")
        if comparable:
            delta = macro["ndcg10"] - ref["bm25"]
            verdict = ("in BM25 ballpark" if abs(delta) < 0.05
                       else "ABOVE BM25" if delta > 0 else "BELOW BM25 — investigate")
            print(f"  vs BM25 reference: {delta:+.3f}  ({verdict})")
        else:
            print("  NOT COMPARABLE — the reference is a 4-domain macro-average over "
                  "the full corpora; this run is a subset.")
    else:
        print(f"  no published reference for --queries {args.queries} "
              f"(the paper reports last-turn and rewrite only)")
    print()
    print("  by domain:")
    for domain in domains:
        d = per_domain[domain]
        print(f"    {domain:<9} n={d['n']:<5} nDCG@10={d['ndcg10']:.3f}  "
              f"R@10={d['recall'][10]:.3f}  R@100={d['recall'][100]:.3f}")
    print()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "dataset": f"mtrag-{args.queries}", "queries": args.queries,
            "domains": domains, "arms": arms, "n": macro["n"],
            "corpus_docs": total_docs,
            # One row covers up to four homes, so its corpus identity is a digest
            # over theirs — the same binding the per-question haystacks use.
            "corpus_id": (hashlib.sha256(
                "\x00".join(sorted(corpus_ids)).encode()).hexdigest()[:16]
                if corpus_ids else None),
            "comparable_to_reference": comparable,
            "ndcg10": macro["ndcg10"], "mrr10": macro["mrr10"],
            "recall": {str(k): macro["recall"][k] for k in ks},
            "query_p50_ms": (sorted(latencies)[len(latencies) // 2] * 1000
                             if latencies else 0.0),
            "micro": micro, "per_domain": per_domain, "reference": ref,
            "per_query": per_query,
            "performance": eval_core.performance(
                latencies, stage_samples, scoring_s=total_s,
                corpus_docs=total_docs, arms=arms),
        }, indent=2, default=str) + "\n", encoding="utf-8")
        _log(f"wrote {args.json_out}")

    if args.fresh:
        shutil.rmtree(homes_root, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", default="all", choices=("all", *DOMAINS),
                    help="one corpus, or all four macro-averaged (default: all — "
                         "the only shape comparable to the published table)")
    ap.add_argument("--queries", default="rewrite", choices=QUERY_SETS,
                    help="which query form to retrieve with (default: rewrite)")
    ap.add_argument("--data-dir", default=str(eval_home.CACHE_ROOT),
                    help="cache root holding mtrag/ and the built homes")
    ap.add_argument("--vectors", action="store_true",
                    help="build + query the semantic arm with the real embedder")
    ap.add_argument("--max-docs", type=int, default=None,
                    help="cap ingested passages per domain (smoke runs)")
    ap.add_argument("--sample", type=int, default=None,
                    help="score a deterministic sample of this many queries "
                         "instead of all, split evenly across the domains "
                         "(the quick tier; see search_lab.eval_core.sample_queries)")
    ap.add_argument("--rebuild", action="store_true",
                    help="discard cached builds and re-ingest (+ re-embed with --vectors)")
    ap.add_argument("--fresh", action="store_true",
                    help="use throwaway homes deleted on exit (no caching)")
    ap.add_argument("--json-out", default=None, help="also write the report as JSON")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
