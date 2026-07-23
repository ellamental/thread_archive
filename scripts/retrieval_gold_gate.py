"""Retrieval-quality regression floors over the snapshot-bound gold case files.

The measurement of record for search quality is the agent-mined gold files
(``evals/README.md`` → "Taking a baseline"): graded, corpus-grounded pools
scored over a frozen corpus snapshot, so the number moves only when the ranking
code moves. This gate scores every gold file that has a calibrated floor and
fails when one drops below it — a ratchet against regression, not a target.
Green here means exactly "search still finds what the golds say it should," the
grounded-label analog of tier 0's synthetic floors; it never means "search is
good" (that is a deliberate gold-delta measurement, not a per-commit number).

Why a floor and not the number: a displayed per-commit metric invites being read
as a quality score, which the click-label protocols are censored against being
(see the ``retrieval-gate`` row in ``ci.toml``). A floor answers one question —
did search break below the grounded baseline — and only that.

The gate's *verdict* stays a floor check, but each run's measured numbers — which
it already prints — are also appended to a run ledger
(``<home>/gold-runs.jsonl``, :mod:`thread_archive._ops.gold_runs`): per-file
MRR/success/recall/nDCG/p50 under the ``SearchParams`` and commit that produced
them. So the baseline is a recorded timeseries, and the before/after of a
defaults change (e.g. a re-rank budget cut) is a lookup — ``--history`` — not a
re-run of the old configuration. The same caveat rides the ledger as the printed
line: these are click-label re-find scores, a regression signal, never a
certification that search is *good*.

The gate *reads* the gold files; it does not tune against them, so it does not
consume the hold-out (``evals/README.md`` → "Hold-out discipline"). That
discipline governs the human tuning loop — don't validate on the file you tuned
against — and is orthogonal to scoring both files as regression floors.

Snapshot binding: each gold file is bound by ``snapshot_id`` to the corpus
snapshot it was mined against; this gate scores over that snapshot
(``THREAD_ARCHIVE_SNAP``, default ``~/.thread/archive-snap``). When the snapshot
is absent, or a gold file's id no longer matches it (the corpus moved, the golds
are mid-re-mine), the affected file is SKIPPED, not failed — a stale or missing
fixture is an operator-maintenance state, not a code regression, and must not
wedge the commit gate red. A file that is present and fresh but carries no floor
entry is scored and reported but left ungated (newly mined, not yet calibrated):
add its floor below to start gating it.

Usage: python scripts/retrieval_gold_gate.py
Exit 0 when every calibrated floor holds (or its fixture is absent/stale);
exit 1 listing each breach.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Gold-file basename -> floors for complementary failure modes: MRR protects the
# first good answer, success@10 protects whether any answer remains reachable,
# true recall@10 protects coverage of every known grade-2 answer, and nDCG@10
# protects the ordering of the whole 2/1/0 graded pool. Each floor sits a few
# points under the measured value — a regression ratchet, not an aspiration.
# Raise a floor when a shipped change lifts the measured number and holds; the
# gate prints measured-vs-floor on every run, so the headroom stays visible.
FLOORS: dict[str, dict[str, float]] = {
    "judged-cases.jsonl": {
        "mrr": 0.40, "success10": 0.80, "recall10": 0.70, "ndcg10": 0.46,
    },
    "topic-cases-suicide.jsonl": {
        "mrr": 0.58, "success10": 0.85, "recall10": 0.78, "ndcg10": 0.58,
    },
    "topic-cases-frustration.jsonl": {
        "mrr": 0.50, "success10": 0.70, "recall10": 0.50, "ndcg10": 0.45,
    },
    # querygen findability — one gold/case, so recall10 tracks success10 (recover
    # the single target = surface it). The query is authored from the thread, so
    # these are paraphrase-match cases: the cross-encoder floats them, and with
    # auto-re-rank shipping off for latency (params.rerank_auto) the floor sits at
    # the lexical+semantic+coherence baseline. Recovering paraphrase recall without
    # the cross-encoder is the rebuild that re-raises this floor.
    "findability-cases.jsonl": {
        "mrr": 0.54, "success10": 0.83, "recall10": 0.83, "ndcg10": 0.60,
    },
    # rerank in-pool judgments — many golds/case (avg ~14), so recall10 is
    # structurally capped (can't fit ~14 golds in 10 slots) and floored low on
    # purpose; success10 and nDCG10 are the load-bearing signals here.
    "rerank-cases.jsonl": {
        "mrr": 0.52, "success10": 0.78, "recall10": 0.38, "ndcg10": 0.55,
    },
}

DEFAULT_SNAP = Path.home() / ".thread" / "archive-snap"
DEFAULT_GOLD_DIR = Path.home() / ".thread" / "archive"

# Sibling artifacts of the mining pipeline that share the "*cases*.jsonl" glob but
# are not gold case files: per-case detail dumps, seed/candidate pools, and the
# `.until-bak` rewrites. A gold file is `judged-cases.jsonl` or
# `topic-cases-<slug>.jsonl`; everything else is filtered out by name.
_NON_GOLD_MARKERS = ("detail", "seed", "candidate", "accepted", "-bak")


def discover_gold_files(gold_dir: Path) -> list[Path]:
    """Every gold case file in ``gold_dir`` — ``judged-cases.jsonl`` and
    ``topic-cases-<slug>.jsonl`` — excluding the mining pipeline's sibling
    artifacts that happen to share the glob."""
    if not gold_dir.is_dir():
        return []
    return sorted(
        p for p in gold_dir.glob("*cases*.jsonl")
        if not any(marker in p.name for marker in _NON_GOLD_MARKERS)
    )


def _first_row(path: Path) -> dict | None:
    """The first non-empty JSONL row, for the ``snapshot_id`` freshness check —
    a cheap read that needs no package import and no model load."""
    for line in path.read_text().splitlines():
        if line.strip():
            return json.loads(line)
    return None


def check_floors(name: str, report: dict, floor: dict[str, float]) -> list[str]:
    """Breach messages for a scored report against its floors (empty = holds).
    A floor may set any subset, though every calibrated production file uses all
    four complementary signals."""
    breaches = []
    mrr = report["mrr"]
    if mrr < floor["mrr"]:
        breaches.append(f"{name}: MRR {mrr:.3f} < floor {floor['mrr']}")
    if "success10" in floor:
        s10 = report["success"][10]
        if s10 < floor["success10"]:
            breaches.append(
                f"{name}: success@10 {s10:.3f} < floor {floor['success10']}")
    if "recall10" in floor:
        r10 = report["recall"][10]
        if r10 < floor["recall10"]:
            breaches.append(f"{name}: recall@10 {r10:.3f} < floor {floor['recall10']}")
    if "ndcg10" in floor:
        n10 = report["ndcg"][10]
        if n10 < floor["ndcg10"]:
            breaches.append(f"{name}: nDCG@10 {n10:.3f} < floor {floor['ndcg10']}")
    return breaches


def _score(cases: list[dict]) -> dict:
    """Score cases with the production search over the snapshot home. Imported
    lazily so the skip paths (absent snapshot, no gold files) stay import-free
    and fixture-free. A broken search pipeline raises here — and *should* fail
    the gate, unlike a stale/missing fixture, which is handled as a skip."""
    from thread_archive._eval import evaluate

    return evaluate(cases, limit=20, rerank=None, content_type=None,
                    exclude_content_types=None)


def _load(path: Path) -> list[dict]:
    from thread_archive._eval import load_case_file

    return load_case_file(path)


def _print_history(limit: int | None) -> int:
    """Print the recorded gold-run timeseries (newest first) — the baseline as it
    actually scored over time, per file, under the config that produced it. The
    read-back half of the ledger: what a defaults change is compared against
    without re-running the old configuration."""
    from thread_archive._config import default_home
    from thread_archive._ops import gold_runs

    home = Path(os.environ.get("THREAD_ARCHIVE_HOME") or default_home())
    runs = gold_runs.read_runs(home, limit=limit)
    if not runs:
        print(f"no recorded gold runs at {home / gold_runs.LEDGER_FILE}")
        return 0
    for run in runs:
        cfg = run.get("config", {}).get("params", {})
        knobs = f"pool={cfg.get('rerank_pool', '?')} doc={cfg.get('rerank_doc_chars', '?')}"
        flag = "ok" if run.get("passed") else "BELOW FLOOR"
        print(f"{run['at'][:19]}  {run.get('commit') or '-':>10}  [{knobs}]  {flag}")
        for name, m in run.get("files", {}).items():
            print(f"    {name:34s} MRR {m.get('mrr', 0):.3f}  S@10 {m.get('success10', 0):.3f}  "
                  f"R@10 {m.get('recall10', 0):.3f}  nDCG@10 {m.get('ndcg10', 0):.3f}  "
                  f"p50 {m.get('p50_ms', 0):.0f}ms")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--history", nargs="?", type=int, const=0, default=None, metavar="N",
                    help="print the recorded gold-run timeseries (newest first; N caps "
                    "the count) instead of scoring, then exit")
    # None (a programmatic main() call — tests, importers) means "no CLI args", not
    # "read sys.argv" (which under pytest is the test runner's argv). The __main__
    # entry passes the real argv explicitly.
    args = ap.parse_args(argv if argv is not None else [])
    if args.history is not None:
        return _print_history(args.history or None)

    snap = Path(os.environ.get("THREAD_ARCHIVE_SNAP", str(DEFAULT_SNAP)))
    gold_dir = Path(os.environ.get("THREAD_ARCHIVE_GOLD_DIR", str(DEFAULT_GOLD_DIR)))
    # The real archive home, captured before the scoring override below repoints
    # THREAD_ARCHIVE_HOME at the frozen snapshot — the run ledger lands here, beside
    # the other home-root ledgers, not inside the snapshot fixture.
    home = Path(os.environ.get("THREAD_ARCHIVE_HOME") or str(DEFAULT_GOLD_DIR))

    manifest = snap / "snapshot.json"
    if not manifest.is_file():
        print(f"gold gate: no snapshot at {snap} — skipping "
              f"(fixture absent; re-snapshot + re-mine to restore)")
        return 0
    current = json.loads(manifest.read_text()).get("snapshot_id")
    # Route the production search at the frozen snapshot for the scoring below.
    os.environ["THREAD_ARCHIVE_HOME"] = str(snap)

    files = discover_gold_files(gold_dir)
    if not files:
        print(f"gold gate: no gold files in {gold_dir} — skipping")
        return 0

    breaches: list[str] = []
    scored = 0
    measured: dict[str, dict] = {}  # per-file metrics for the run ledger
    for path in files:
        name = path.name
        try:
            row = _first_row(path)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  {name:34s} WARN — unreadable ({exc}); skipping")
            continue
        sid = row.get("snapshot_id") if row else None
        if sid != current:
            print(f"  {name:34s} SKIP — snapshot {sid} != {current} (stale / mid-re-mine)")
            continue

        cases = _load(path)
        report = _score(cases)
        mrr = report["mrr"]
        s10 = report["success"][10]
        r10 = report["recall"][10]
        n10 = report["ndcg"][10]
        floor = FLOORS.get(name)
        # Record every scored file — gated or not — with its full metric row.
        fb = check_floors(name, report, floor) if floor else []
        measured[name] = {
            "n": report["n"], "mrr": round(mrr, 4), "success10": round(s10, 4),
            "recall10": round(r10, 4), "ndcg10": round(n10, 4),
            "p50_ms": round(report["latency_p50_ms"], 1),
            "status": ("below_floor" if fb else "ok") if floor else "ungated",
        }
        if floor is None:
            print(f"  {name:34s} MRR {mrr:.3f}  S@10 {s10:.3f}  "
                  f"R@10 {r10:.3f}  nDCG@10 {n10:.3f}  "
                  f"ungated (no floor — add one to gate)")
            continue
        status = "BELOW FLOOR" if fb else "ok"
        print(f"  {name:34s} MRR {mrr:.3f} (floor {floor['mrr']})  "
              f"S@10 {s10:.3f} (floor {floor.get('success10', '-')})  "
              f"R@10 {r10:.3f} (floor {floor.get('recall10', '-')})  "
              f"nDCG@10 {n10:.3f} (floor {floor.get('ndcg10', '-')})  {status}")
        breaches += fb
        scored += 1

    # Record the run whenever anything was measured (gated or ungated) — the
    # timeseries wants the ungated files' numbers too, so a floor can be
    # calibrated from history. Fail-soft: never breaks the gate's verdict.
    if measured:
        from thread_archive._ops import gold_runs

        gold_runs.record_run(home, snapshot_id=current, files=measured,
                             passed=not breaches)

    if scored == 0 and not breaches:
        print("gold gate: no fresh calibrated gold files — skipping "
              "(all stale or ungated)")
        return 0
    if breaches:
        print("\nretrieval-quality regression:")
        for b in breaches:
            print(f"  {b}")
        return 1
    print(f"\ngold gate: {scored} file(s) above floor")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
