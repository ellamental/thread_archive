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

# gold-file basename -> floors (MRR, recall@10), each set a few points under the
# measured value — a ratchet against regression, not an aspiration. Raise a floor
# when a shipped change lifts the measured number and holds; the gate prints
# measured-vs-floor on every run, so the headroom is always visible. Small pools
# swing on a single case (judged n≈21 → ~0.02 MRR per rank-1→2 flip; a topic pool
# n≈7 → ~0.07 MRR, ~0.14 recall), so the headroom is roughly one case flip: a
# lone borderline case must not fire the gate, a systemic drop must.
FLOORS: dict[str, dict[str, float]] = {
    "judged-cases.jsonl": {"mrr": 0.40, "recall10": 0.80},
    "topic-cases-suicide.jsonl": {"mrr": 0.58, "recall10": 0.85},
    "topic-cases-frustration.jsonl": {"mrr": 0.50, "recall10": 0.70},
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
    MRR and recall@10 are the two interpretable regression signals; a floor may
    set either or both."""
    breaches = []
    mrr = report["mrr"]
    if mrr < floor["mrr"]:
        breaches.append(f"{name}: MRR {mrr:.3f} < floor {floor['mrr']}")
    if "recall10" in floor:
        r10 = report["recall"][10]
        if r10 < floor["recall10"]:
            breaches.append(f"{name}: recall@10 {r10:.3f} < floor {floor['recall10']}")
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


def main() -> int:
    snap = Path(os.environ.get("THREAD_ARCHIVE_SNAP", str(DEFAULT_SNAP)))
    gold_dir = Path(os.environ.get("THREAD_ARCHIVE_GOLD_DIR", str(DEFAULT_GOLD_DIR)))

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
        mrr, r10 = report["mrr"], report["recall"][10]
        floor = FLOORS.get(name)
        if floor is None:
            print(f"  {name:34s} MRR {mrr:.3f}  R@10 {r10:.3f}  "
                  f"ungated (no floor — add one to gate)")
            continue
        fb = check_floors(name, report, floor)
        status = "BELOW FLOOR" if fb else "ok"
        r10_floor = floor.get("recall10")
        print(f"  {name:34s} MRR {mrr:.3f} (floor {floor['mrr']})  "
              f"R@10 {r10:.3f} (floor {r10_floor if r10_floor is not None else '-'})  {status}")
        breaches += fb
        scored += 1

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
    sys.exit(main())
