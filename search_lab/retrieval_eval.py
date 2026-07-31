"""Retrieval instruments over the live archive: the arm probes and the behavior report.

Neither produces a relevance label, and that is the point. **No protocol that
labels this archive's own corpus can certify that search is good** — labels made
by searching are circular, and labels fixed against a record outside search come
with queries nobody asked (``docs/public/search-quality.md`` → "The admission rule").
Quality claims live on the public benchmarks (``python -m search_lab benchmark``);
what runs here answers narrower questions that have honest answers.

``--probes-only`` runs no metric pass at all: it asserts the model arms are alive
and exits. This is the CI row's mode, and the reason it exists is that a dead
embeddings model silently degrades the fused pipeline to lexical-only — a
degradation invisible in any number that does not check for it, and one a direct
probe catches at zero queries::

    .venv/bin/python search_lab/retrieval_eval.py --probes-only --require-semantic

``--behavior`` runs no ranking at all: it reports zero-label behavioral signals
from the whole tool-use trail — per search, did the agent click a result,
reformulate, or abandon? Proxies, not judgments; their value is the trend, and a
single run's rates say nothing on their own. ``--after`` bounds the window and
``--trend-out`` appends the run as one JSONL row::

    .venv/bin/python search_lab/retrieval_eval.py --behavior --after 2026-06-01

Read-only against the live archive.

The scoring loop itself (:func:`eval_core.evaluate`) is re-exported here because
the tier-0 synthetic corpus scores through it (``tests/quality_corpus.py``). Its
labels are true by construction — nonce terms in a checked-in corpus — which is
what keeps it clear of the admission rule, and it detects damage rather than
crediting improvement.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
# The lab dir too, so bare sibling imports (eval_core, snapshot, …) resolve
# however this file was loaded: as a script, by path, or as search_lab.X.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The scoring engine (eval_core) re-exported at module scope, because the tier-0
# corpus loads this file by path and reaches these names as attributes on it.
from eval_core import (  # noqa: E402,F401
    EXCLUDE_META,
    RECALL_KS,
    _trail_events,  # noqa: E402,F401
    behavior_report,
    classify_tool,
    evaluate,
    ndcg_at_k,
    pair_log_events,
    query_shape,
    resolve_read_refs,
)

from thread_archive import _api as api  # noqa: E402
from thread_archive._store import use_session  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probes-only", action="store_true",
                      help="assert the model arms are alive and exit (the CI "
                      "gate's mode) — claims nothing about quality")
    mode.add_argument("--behavior", action="store_true",
                      help="no ranking run at all: report zero-label "
                      "behavioral signals from the whole trail — click rate, "
                      "reformulation rate, abandonment rate per search")
    ap.add_argument("--after", metavar="ISO", default=None,
                    help="--behavior: only trail events at or after this date")
    ap.add_argument("--trend-out", type=Path, metavar="FILE", default=None,
                    help="append this run's report as one JSONL row (~ ok) — "
                    "the time series that turns a point measurement into a trend")
    ap.add_argument("--require-semantic", action="store_true",
                    help="exit 1 if the semantic arm is unavailable — without "
                    "this, a dead embeddings model silently degrades the "
                    "'fused' pipeline to lexical-only and nothing says so")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args()

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
            report = behavior_report(_trail_events(s, args.after))
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"searches: {report['n_searches']}   "
                  f"sessions: {report['n_sessions']}   "
                  f"click: {report['click_rate']:.3f}   "
                  f"reformulate: {report['reformulation_rate']:.3f}   "
                  f"abandon: {report['abandonment_rate']:.3f}   "
                  f"reads/click: {report['reads_per_click']:.2f}")
        append_trend({"protocol": "behavior", "after": args.after, **report})
        return

    if args.require_semantic:
        from thread_archive._retrieval import embed

        if not embed.is_available():
            print("RETRIEVAL GATE BREACH: semantic arm unavailable "
                  "(embeddings model failed to load?)", file=sys.stderr)
            raise SystemExit(1)
    if not args.require_semantic:
        ap.error("--probes-only without --require-semantic checks nothing")
    print("retrieval arm probes passed")


if __name__ == "__main__":
    main()
