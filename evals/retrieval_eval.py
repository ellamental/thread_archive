"""Retrieval eval harness — measure search quality so ranking changes are measurable.

Three case protocols, one scoring loop:

``--auto-titles N`` is a zero-curation proxy: sample N titled conversation
threads, use each *title* as the query, and score whether the thread's own
content ranks. Thread-meta docs (title/summary) are excluded from the searched
scope so the eval never matches the query against itself. Cheap and stable, but
titles are LLM distillations of the thread they name, so vocabulary overlap is
built in — treat the numbers as a regression ratchet, not real-world quality.

``--from-log N`` scores against real usage: the archive's own tool-use trail
holds every ``thread_search`` call agents have made (the query) and the
``thread_read`` calls that followed in the same session (the click). Each
search paired with its subsequent reads is a relevance judgment made by the
searcher at the moment of searching — real query vocabulary, multi-gold, no
curation. Pairing rules: a read labels the most recent prior search in its
session; reads of threads the agent had already opened before searching don't
count (it knew them without the search); the originating session is skipped
during ranking (it quotes the query verbatim). Read refs in the trail come in
every shape the read tool accepts — legacy integer ids, ULIDs, provider
session ids — and are canonicalized through the same resolver the tool uses;
reads that resolve to nothing are dropped. A click is the pick from what
past search surfaced, not a corpus-wide judgment: golds are incumbent-shaped,
so credit for surfacing relevant threads past search never reached is
invisible here, and a clicked thread wasn't necessarily satisfying (opened is
not answered). Every cross-stack comparison on these labels favors whatever
resembles the system that generated the log — including the comparison a
regression gate makes. The one reading the bias can't fake: the golds are
(mostly) relevant threads, so a *collapse* against them means something real
broke. Treat the metric as a collapse alarm — a modest drop under a
deliberately reshaped ranker may be divergence from the incumbent's shape,
not regression; no number from this protocol certifies improvement.

``--cases FILE`` evaluates a JSONL file of ``{"query": ..., "gold": [ids]}``
rows (optional ``"grades"``: a ``thread id -> 0|1|2`` candidate pool nDCG
scores against — grade the whole pool, not one golden result; optional
``"sessions"``: thread ids to skip while ranking) — the hook for hand-curated
or generated query sets. Mined and curated case files contain real usage; keep
them out of the repo.

``--behavior`` runs no ranking at all: it reports zero-label behavioral
signals from the whole trail — per search, did the agent click a result,
reformulate, or abandon? Proxies, not judgments; their value is the trend.

Reports MRR and recall@1/5/10/20 (binary, over each case's gold) and, when a
case file carries a graded pool, nDCG@1/5/10/20 (graded), overall and per
query-shape (so a lexical regression can't hide behind semantic wins).
``--mined-after`` restricts the log protocols to trail events after a date
(the time-based holdout); ``--trend-out`` appends any run's report as one
JSONL row at ~/.thread/archive/retrieval-trend.jsonl, turning point
measurements into a time series (LLM-judged relevance grades from
evals/retrieval_judge.py land beside it). ``--probes-only`` skips the metric
run entirely and exits after the ``--require-*`` arm-liveness probes — the CI
gate's mode: the gate asserts the model arms are alive and leaves quality
measurement to the snapshot-bound gold files.

Read-only. The title / log protocols run against the live archive:

    .venv/bin/python evals/retrieval_eval.py --auto-titles 200
    .venv/bin/python evals/retrieval_eval.py --from-log 500

The agent-mined ``--cases`` protocol runs against the frozen corpus snapshot the
cases were mined against — point ``THREAD_ARCHIVE_HOME`` at that snapshot. Each
case carries the snapshot's content fingerprint (``snapshot_id``); the run
refuses any case whose id does not match the home, so golds are never scored
against a corpus that has changed under them (re-mine after a new snapshot):

    export THREAD_ARCHIVE_HOME=~/.thread/archive-snap
    .venv/bin/python evals/retrieval_eval.py --cases ~/.thread/archive/judged-cases.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from thread_archive import _api as api  # noqa: E402

# The scoring engine lives in the package so the shipped `thread_archive eval` command
# and this dev bench score off one code path. Re-exported at module scope
# because the sibling harnesses (retrieval_mine_gold, retrieval_judge,
# search_arena, graph_eval) and tests/test_retrieval_eval.py load this file by
# path and reach these names as attributes on it.
from thread_archive._eval import (  # noqa: E402,F401
    EXCLUDE_META,
    RECALL_KS,
    _trail_events,  # noqa: E402,F401
    behavior_report,
    classify_tool,
    evaluate,
    load_case_file,
    mine_log_cases,
    ndcg_at_k,
    pair_log_events,
    query_shape,
    resolve_read_refs,
    sample_title_cases,
)
from thread_archive._store import use_session  # noqa: E402

# The rerank liveness pair: a query, its answer, and a decoy no working
# cross-encoder confuses with it. Deliberately trivial — the probe asserts the
# model loads and discriminates at all, not that it ranks well (BEIR measures
# that). Failing this pair means the rerank arm is dead or scrambled.
PROBE_QUERY = "what is the largest animal on earth"
PROBE_ANSWER = "The blue whale is the largest animal known to have ever existed."
PROBE_DECOY = "Set the compiler's optimization flags before an incremental build."


def rerank_probe(reranker) -> str | None:
    """Prove the cross-encoder arm is alive: load the real model and score one
    trivial pair. Returns a breach message, or None when the arm works.

    ``is_available()`` alone can't carry this check — it is deliberately cheap
    (never loads the model), so a corrupt model file or a broken torch install
    still reports available and only degrades at call time, exactly the silent
    production failure this probe exists to catch."""
    if not reranker.is_available():
        return ("rerank arm unavailable (switched off, [embeddings] extra "
                "absent, or a prior load failed)")
    scores = reranker.rerank_scores(PROBE_QUERY, [PROBE_ANSWER, PROBE_DECOY])
    if scores is None:
        return "rerank arm degraded at scoring time (model failed to load or predict)"
    if len(scores) != 2 or not all(math.isfinite(s) for s in scores):
        return f"rerank arm returned malformed scores: {scores!r}"
    if scores[0] <= scores[1]:
        return (f"rerank arm cannot discriminate the liveness pair "
                f"(answer {scores[0]:.3f} <= decoy {scores[1]:.3f}) — "
                f"model {reranker.name!r} is loaded but scrambled")
    return None


def _require_matching_snapshot(cases: list[dict], cases_path) -> None:
    """Refuse to score agent-mined cases unless the home is the snapshot they were
    mined against. Each case carries the corpus's content fingerprint; the run
    exits rather than scoring golds against a corpus that has moved under them.

    The current home must be a snapshot (``snapshot.json`` with a ``snapshot_id``)
    and every case's id must match it. A mismatch means the snapshot changed since
    mining — re-mine against the new one. Cases with no ``snapshot_id`` are
    pre-binding (old format) and count as a mismatch."""
    from thread_archive._ops.snapshot import read_snapshot_id

    current = read_snapshot_id()
    if current is None:
        raise SystemExit(
            f"--cases must run against the corpus snapshot the cases were mined "
            f"against, but THREAD_ARCHIVE_HOME is not a snapshot. Run "
            f"`thread_archive snapshot <dir>` and point THREAD_ARCHIVE_HOME at it "
            f"(the same snapshot {cases_path} was mined against)."
        )
    stale = sorted({c.get("snapshot_id") for c in cases} - {current})
    if stale:
        raise SystemExit(
            f"{cases_path} was mined against snapshot(s) {stale}, but the current "
            f"snapshot is {current} — the corpus has changed and these golds are "
            f"stale. Re-mine against this snapshot with retrieval_mine_gold.py."
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    proto = ap.add_mutually_exclusive_group(required=True)
    proto.add_argument("--auto-titles", type=int, metavar="N",
                       help="sample N thread titles as queries (proxy protocol)")
    proto.add_argument("--from-log", type=int, metavar="N",
                       help="mine up to N real search->read cases from the "
                       "archive's own tool-use trail (click protocol)")
    proto.add_argument("--cases", type=Path, metavar="FILE",
                       help="evaluate a JSONL case file: "
                       '{"query", "gold": [thread ids], "sessions": [...], '
                       '"grades": {id: 0|1|2}} — grades enable nDCG')
    proto.add_argument("--probes-only", action="store_true",
                       help="no metric run: exit after the --require-* arm "
                       "probes (the CI gate's mode)")
    proto.add_argument("--behavior", action="store_true",
                       help="no ranking run at all: report zero-label "
                       "behavioral signals from the whole trail — click rate, "
                       "reformulation rate, abandonment rate per search")
    ap.add_argument("--mined-after", metavar="ISO", default=None,
                    help="--from-log/--behavior: only trail events at or after "
                    "this date — the time-based holdout (cases mined after a "
                    "ranking change shipped carry less of the old incumbent's "
                    "shape)")
    ap.add_argument("--trend-out", type=Path, metavar="FILE", default=None,
                    help="append this run's report as one JSONL row (~ ok) — "
                    "the time series that turns a snapshot number into a "
                    "trend; the CI gate row points it at "
                    "~/.thread/archive/retrieval-trend.jsonl")
    ap.add_argument("--seed", type=int, default=7, help="case sampling seed")
    ap.add_argument("--limit", type=int, default=20, help="results per query (recall ceiling)")
    ap.add_argument("--rerank", choices=["auto", "on", "off"], default="auto",
                    help="cross-encoder head re-rank (default: the pipeline's auto-gate)")
    ap.add_argument("--lexical-only", action="store_true",
                    help="evaluate the FTS arm alone (semantic + rerank arms off) — "
                    "the search a core install without the [embeddings] extra gets. "
                    "The CI gate instead runs the fused pipeline with --rerank off: "
                    "the production path minus the cross-encoder, whose per-query "
                    "inference would multiply the row's runtime")
    ap.add_argument("--content-type", default=None,
                    help="restrict the searched scope to one content type")
    ap.add_argument("--include-meta", action="store_true",
                    help="auto-titles only: leave title/summary docs in the "
                    "searched scope (the log protocol always searches them — "
                    "its queries aren't derived from any document)")
    ap.add_argument("--exclude-content-type", action="append", default=None,
                    metavar="TYPE",
                    help="drop a content type from the searched scope "
                    "(repeatable; overrides the protocol's default exclusions "
                    "— e.g. --exclude-content-type summary measures a scope "
                    "without stored summaries)")
    ap.add_argument("--require-semantic", action="store_true",
                    help="exit 1 if the semantic arm is unavailable — without "
                    "this, a dead embeddings model silently degrades the "
                    "'fused' pipeline under test to lexical-only and the "
                    "metrics measure the wrong stack")
    ap.add_argument("--require-rerank", action="store_true",
                    help="exit 1 unless the real cross-encoder loads and "
                    "discriminates a trivial pair — the rerank analog of "
                    "--require-semantic. Costs one model load; the metric run "
                    "itself may still skip per-query rerank (--rerank off), "
                    "so the gate proves the arm is alive without paying "
                    "per-query inference")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--dump-cases", type=Path, metavar="FILE", default=None,
                    help="also write the evaluated cases as JSONL (real usage "
                    "data — keep it out of the repo)")
    gate = ap.add_argument_group(
        "gate", "regression floors — any breach exits 1 (the CI rows set these; "
        "floors are a ratchet calibrated under measured values, not a target)"
    )
    gate.add_argument("--min-mrr", type=float, default=None)
    gate.add_argument("--min-recall10", type=float, default=None)
    gate.add_argument("--min-recall20", type=float, default=None)
    args = ap.parse_args()

    if args.lexical_only and args.require_rerank:
        ap.error("--require-rerank contradicts --lexical-only "
                 "(which switches the rerank arm off for this process)")

    if args.lexical_only:
        # The product's own switch: both model arms report unavailable for the rest
        # of this process, so the measured pipeline is the one a lexical-only box runs.
        os.environ["THREAD_ARCHIVE_EMBED"] = "off"
        os.environ["THREAD_ARCHIVE_RERANK"] = "off"

    api.open_archive()

    def append_trend(row: dict) -> None:
        if not args.trend_out:
            return
        from datetime import datetime, timezone

        out = args.trend_out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        stamped = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   **row}
        with out.open("a") as f:
            f.write(json.dumps(stamped) + "\n")

    if args.behavior:
        with use_session() as s:
            report = behavior_report(_trail_events(s, args.mined_after))
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"searches: {report['n_searches']}   "
                  f"sessions: {report['n_sessions']}   "
                  f"click: {report['click_rate']:.3f}   "
                  f"reformulate: {report['reformulation_rate']:.3f}   "
                  f"abandon: {report['abandonment_rate']:.3f}   "
                  f"reads/click: {report['reads_per_click']:.2f}")
        append_trend({"protocol": "behavior",
                      "mined_after": args.mined_after, **report})
        return

    if args.require_semantic:
        from thread_archive._retrieval import embed

        if not embed.is_available():
            print("RETRIEVAL GATE BREACH: semantic arm unavailable "
                  "(embeddings model failed to load?)", file=sys.stderr)
            raise SystemExit(1)
    if args.require_rerank:
        from thread_archive._retrieval import rerank as rerank_mod

        breach = rerank_probe(rerank_mod.default())
        if breach:
            print(f"RETRIEVAL GATE BREACH: {breach}", file=sys.stderr)
            raise SystemExit(1)
    if args.probes_only:
        if not (args.require_semantic or args.require_rerank):
            ap.error("--probes-only without --require-semantic/--require-rerank "
                     "checks nothing")
        print("retrieval arm probes passed")
        return
    if args.auto_titles is not None:
        cases = sample_title_cases(args.auto_titles, args.seed)
        exclude = None if args.include_meta else EXCLUDE_META
    elif args.from_log is not None:
        cases = mine_log_cases(args.from_log, args.seed, args.mined_after)
        exclude = None
    else:
        cases = load_case_file(args.cases)
        exclude = None
        _require_matching_snapshot(cases, args.cases)
    if args.exclude_content_type:
        exclude = args.exclude_content_type
    if not cases:
        raise SystemExit("no eval cases")
    if args.dump_cases:
        args.dump_cases.write_text(
            "".join(json.dumps(c) + "\n" for c in cases))
    rerank = None if args.rerank == "auto" else (args.rerank == "on")

    report = evaluate(cases, limit=args.limit, rerank=rerank,
                      content_type=args.content_type, exclude_content_types=exclude)

    protocol = ("auto-titles" if args.auto_titles is not None
                else "from-log" if args.from_log is not None else "cases")
    append_trend({
        "protocol": protocol, "seed": args.seed, "limit": args.limit,
        "rerank": args.rerank, "lexical_only": args.lexical_only,
        "mined_after": args.mined_after,
        "n": report["n"], "mrr": report["mrr"],
        "recall": {str(k): v for k, v in report["recall"].items()},
        "ndcg": {str(k): v for k, v in report["ndcg"].items()},
        "latency_p50_ms": report["latency_p50_ms"],
    })

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"cases: {report['n']}   MRR: {report['mrr']:.3f}   "
              + "   ".join(f"R@{k}: {v:.3f}" for k, v in report["recall"].items()))
        print("   ".join(f"nDCG@{k}: {v:.3f}" for k, v in report["ndcg"].items()))
        print(f"latency p50: {report['latency_p50_ms']:.0f} ms")
        for shape, stats in report["per_shape"].items():
            print(f"  {shape:>15}: n={stats['n']:<4} MRR={stats['mrr']:.3f}")

    breaches = []
    if args.min_mrr is not None and report["mrr"] < args.min_mrr:
        breaches.append(f"MRR {report['mrr']:.3f} < floor {args.min_mrr}")
    if args.min_recall10 is not None and report["recall"][10] < args.min_recall10:
        breaches.append(f"recall@10 {report['recall'][10]:.3f} < floor {args.min_recall10}")
    if args.min_recall20 is not None and report["recall"][20] < args.min_recall20:
        breaches.append(f"recall@20 {report['recall'][20]:.3f} < floor {args.min_recall20}")
    if breaches:
        for b in breaches:
            print(f"RETRIEVAL GATE BREACH: {b}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
