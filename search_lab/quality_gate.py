#!/usr/bin/env python3
"""The quality gate: the bench's numbers against a frozen, checked-in baseline.

``python -m search_lab benchmark`` measures; this decides whether what it
measured is releasable. The two are split because they answer different
questions — the bench prints a delta against *the last run at a different
configuration*, which is the number a tuning pass is read on and a moving
reference by construction. A release needs the other thing: a fixed set of
accepted numbers that a change has to clear, and that only moves when somebody
decides it should.

    python -m search_lab gate --run --quick     # the release gate: measure, then compare
    python -m search_lab gate --quick           # compare the recorded quick tier
    python -m search_lab gate --run             # the full tier — every query, hours
    python -m search_lab gate --quick --update  # accept what is recorded as the new bar

**A release is gated on the quick tier.** It is the depth that fits a preflight —
sampled where a row is too large to score whole, every query everywhere else, the
same seven datasets either way — and damage detection is what a gate is for. The
full tier is what a *published* number would have to come from; it is not run
routinely, and nothing here requires it to be. Both tiers keep accepted numbers in
this one file, under their own row names, and an ``--update`` at one depth leaves
the other's alone.

**The baseline is checked in** (``search_lab/quality-baseline.json``), unlike
every other artifact this lab writes. It has to be: the ledger is per-box, and a
release cut from any checkout must be gated against the same accepted numbers.
It is a record of a decision, not a measurement — which is why nothing writes it
automatically and ``--update`` is a verb somebody types.

**What this can and cannot certify.** Every gated row is a public benchmark whose
labels somebody else made, so a breach is real evidence that the retrieval
components got worse. It is still not evidence about *this archive* — see
``docs/search-quality.md`` on why no protocol scoring this corpus qualifies. The
gate detects damage on other people's labels. That is the whole claim.

**A stale row fails.** Not being measured at the code under release is
indistinguishable, from here, from being measured and having regressed — and the
failure mode of guessing is a release that ships an unmeasured ranking change
while the gate reads green. ``--run`` is the fix (rows unchanged since their last
run are fresh and cost milliseconds); ``--allow-stale`` is the escape hatch for
reading the ledger mid-tuning, and it says so in its output.

**Tolerance is two cases wide.** Scoring is deterministic — same code, same
corpus, same numbers to the digit — so a movement is never noise, and the band
exists for a different reason: on ``n`` scored queries a single case going from
found to unfound moves any of these metrics by ``1/n``, which is the resolution
the row actually has. Anything under that is rank shuffling inside cases that
already worked. The default band is :data:`SLACK_CASES` of them, floored at
:data:`MIN_TOLERANCE` so a very large row still gets a usable one, and a row may
override it explicitly.

A real trade — a change that lifts five rows and costs one — trips this
deliberately. The gate's job is to make that a decision somebody makes
(``--update``, with the movement in the diff) rather than one that happens.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_runs  # noqa: E402
import benchmark  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

#: The accepted numbers. Beside the code rather than in the archive home, because
#: a release is cut against the code and this is part of what "releasable" means.
BASELINE_PATH = Path(__file__).resolve().parent / "quality-baseline.json"

#: How many scored cases a row may lose before the gate calls it a regression.
#: The unit is cases rather than points because points are not comparable across
#: rows — 0.01 is three cases on a 300-query row and twenty on a 1,981-query one.
SLACK_CASES = 2

#: The floor under the derived band. A 5,000-query row's 2/n is 0.0004, which is
#: below the third decimal the ledger rounds its measures to, so without a floor
#: the gate would fail on the rounding rather than on the ranking.
MIN_TOLERANCE = 0.002

#: States that fail the gate. ``ungated`` is a manifest row with no baseline
#: entry (never measured, or deliberately not gated) — reported, never fatal:
#: the set of rows a box can run is a fact about the box, and a gate that
#: demanded all of them would be unrunnable everywhere but here.
FAILING = ("regressed", "stale", "missing", "corpus-changed", "unknown-row")


# ── the baseline file ────────────────────────────────────────────────────────


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    """The accepted numbers, or an empty baseline when none is recorded yet.

    A missing file is "nothing has been accepted", which the gate reports as
    every row ungated — not an error. An unparseable one *is* an error: a
    corrupted baseline that read as empty would silently gate nothing."""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"rows": {}}
    if not isinstance(blob, dict) or not isinstance(blob.get("rows"), dict):
        raise ValueError(f"{path} is not a baseline file")
    return blob


def tolerance_for(entry: dict[str, Any], measure: str) -> float:
    """How far ``measure`` may fall before it counts as a regression.

    An explicit ``tolerance`` on the entry wins — per-measure first, then a
    row-wide number — so a row with a known noise source (a corpus whose labels
    carry a documented error rate, say) can widen its own band without loosening
    every other row. Otherwise the band is derived from the row's resolution."""
    explicit = entry.get("tolerance")
    if isinstance(explicit, dict) and isinstance(explicit.get(measure), (int, float)):
        return float(explicit[measure])
    if isinstance(explicit, (int, float)):
        return float(explicit)
    n = entry.get("n")
    if isinstance(n, int) and n > 0:
        return max(SLACK_CASES / n, MIN_TOLERANCE)
    return MIN_TOLERANCE


# ── comparison ───────────────────────────────────────────────────────────────


@dataclass
class MeasureDelta:
    """One metric's movement against its accepted value."""

    measure: str
    baseline: float
    observed: float
    tolerance: float

    @property
    def delta(self) -> float:
        return self.observed - self.baseline

    @property
    def breached(self) -> bool:
        """Only a *fall* past the band breaches. A rise is the outcome the whole
        bench exists to produce, and a gate that flagged it would be a pin on the
        numbers rather than a floor under them."""
        return self.delta < -self.tolerance

    def render(self) -> str:
        return (f"{self.measure} {self.observed:.4f} vs {self.baseline:.4f} "
                f"({self.delta:+.4f}, band {self.tolerance:.4f})")


@dataclass
class Verdict:
    """One row's outcome: what state it is in, and why."""

    row: str
    state: str
    detail: str = ""
    deltas: list[MeasureDelta] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.state in FAILING

    def render(self) -> str:
        breaches = [d.render() for d in self.deltas if d.breached]
        body = "; ".join(breaches) or self.detail
        return f"{self.row:<30}{self.state:<16}{body}"


def compare_row(name: str, entry: dict[str, Any], record: Optional[dict[str, Any]],
                *, current_code: str, allow_stale: bool = False) -> Verdict:
    """One baselined row against its most recent successful run.

    Pure: the caller supplies the record, so the whole decision table is testable
    without a ledger, a corpus, or a bench pass behind it."""
    if record is None:
        return Verdict(name, "missing",
                       "never measured on this box — run the bench, or build its corpus")
    observed = record.get("measures") or {}

    # Corpus identity before numbers. A row scored over a different query set is
    # not a worse measurement of the same thing, it is a measurement of something
    # else, and reading a delta across that boundary is how a rebuilt corpus gets
    # filed as a ranking regression.
    want_n, got_n = entry.get("n"), observed.get("n")
    if isinstance(want_n, int) and isinstance(got_n, int) and want_n != got_n:
        return Verdict(name, "corpus-changed",
                       f"scored {got_n} queries, baseline was {want_n} — "
                       f"re-baseline rather than compare across it")
    want_corpus, got_corpus = entry.get("corpus_id"), record.get("corpus_id")
    if want_corpus and got_corpus and want_corpus != got_corpus:
        return Verdict(name, "corpus-changed",
                       f"corpus {got_corpus} is not the baselined {want_corpus}")

    if record.get("code_id") != current_code and not allow_stale:
        return Verdict(name, "stale",
                       f"last measured at code {record.get('code_id')}, "
                       f"now {current_code}")

    deltas = []
    for measure, accepted in sorted((entry.get("measures") or {}).items()):
        got = observed.get(measure)
        if not isinstance(accepted, (int, float)) or not isinstance(got, (int, float)):
            continue
        deltas.append(MeasureDelta(measure, float(accepted), float(got),
                                   tolerance_for(entry, measure)))
    if not deltas:
        return Verdict(name, "missing", "the run recorded none of the baselined measures")
    if any(d.breached for d in deltas):
        return Verdict(name, "regressed", deltas=deltas)
    best = max(deltas, key=lambda d: d.delta)
    state = "ok (stale)" if record.get("code_id") != current_code else "ok"
    return Verdict(name, state, f"best {best.render()}", deltas=deltas)


def check(baseline: dict[str, Any], rows: list[benchmark.Row], *,
          known: Optional[set[str]] = None, home: Optional[Path] = None,
          allow_stale: bool = False) -> list[Verdict]:
    """Every row's verdict, in manifest order, with baseline-only rows last.

    A row in the baseline that the manifest no longer knows is its own failure
    (``unknown-row``) rather than a silent omission: a renamed or dropped row
    means the baseline stopped describing the bench, and the number it still
    carries is being checked against nothing.

    ``known`` is the full manifest's row names, and it is separate from ``rows``
    for exactly that check: under a ``--only`` selection every unselected row is
    absent from ``rows`` while being perfectly well known, so deriving the drift
    check from the selection would report the whole rest of the bench as
    dropped. Defaulted to the selection for a caller gating everything."""
    entries = dict(baseline.get("rows") or {})
    names = known if known is not None else {row.name for row in rows}
    verdicts: list[Verdict] = []
    for row in rows:
        entry = entries.pop(row.name, None)
        if entry is None:
            verdicts.append(Verdict(row.name, "ungated", "no accepted numbers"))
            continue
        verdicts.append(compare_row(
            row.name, entry, bench_runs.latest_ok(home, row=row.name),
            current_code=row.code_id(), allow_stale=allow_stale))
    for name in sorted(entries):
        if name in names:
            continue  # known, merely not selected
        verdicts.append(Verdict(name, "unknown-row",
                                "baselined but not in the manifest — renamed or dropped"))
    return verdicts


# ── accepting a new baseline ─────────────────────────────────────────────────


def build_baseline(rows: list[benchmark.Row], *, home: Optional[Path] = None,
                   previous: Optional[dict[str, Any]] = None,
                   known: Optional[set[str]] = None) -> dict[str, Any]:
    """The baseline the currently recorded runs would establish.

    Only successful runs, and only rows that have one — a row whose corpus is not
    built on this box contributes nothing rather than a hole that would read as
    an accepted zero. An existing entry's explicit ``tolerance`` is carried
    forward: it encodes a judgment about that row, not about the numbers it
    happened to be holding.

    ``known`` is both tiers' row names, and it makes an update *tier-local*: an
    accepted entry this tier does not produce is carried forward when the other
    tier still runs it, and dropped when nothing does. Without it a
    ``gate --quick --update`` would rewrite the file to the quick rows alone and
    ungate the full tier — which reads as green, because an ungated row never
    fails. Removing an accepted number should take the same deliberate act that
    adding one does."""
    kept = ((previous or {}).get("rows") or {})
    out: dict[str, Any] = {}
    for row in rows:
        record = bench_runs.latest_ok(home, row=row.name)
        if not record:
            continue
        measures = record.get("measures") or {}
        scored = {k: round(float(v), 4) for k in row.measure_keys
                  if isinstance((v := measures.get(k)), (int, float))}
        if not scored:
            continue
        entry: dict[str, Any] = {
            "at": record.get("at"),
            "commit": record.get("commit"),
            "code_id": record.get("code_id"),
            "n": measures.get("n"),
            "measures": scored,
        }
        if record.get("corpus_id"):
            entry["corpus_id"] = record["corpus_id"]
        if "tolerance" in kept.get(row.name, {}):
            entry["tolerance"] = kept[row.name]["tolerance"]
        out[row.name] = entry
    for name, entry in kept.items():
        if name not in out and known is not None and name in known:
            out[name] = entry
    return {
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": out,
    }


def write_baseline(baseline: dict[str, Any], path: Path = BASELINE_PATH) -> None:
    path.write_text(json.dumps(baseline, indent=1, sort_keys=False) + "\n",
                    encoding="utf-8")


def bench_argv(*, quick: bool, only: list[str]) -> list[str]:
    """What ``--run`` hands the bench, so the run and the comparison agree about
    which rows they are talking about.

    A dropped ``--quick`` here is the shape worth pinning: the bench would score
    the full rows while the gate compared their sampled names, and every gated
    row would read as never measured. The selection has to travel for the same
    reason in reverse — a ``--run --only scifact`` that ran everything would turn
    a targeted check into an overnight pass."""
    argv = ["--quiet"]
    if quick:
        argv.append("--quick")
    for fragment in only:
        argv += ["--only", fragment]
    return argv


# ── cli ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", action="store_true",
                    help="run the bench before comparing (fresh rows cost "
                         "milliseconds; a ranking edit re-runs everything)")
    ap.add_argument("--quick", action="store_true",
                    help="gate the quick tier, which is what a release is cut on "
                         "— its sampled rows carry their own names and therefore "
                         "their own accepted numbers")
    ap.add_argument("--only", action="append", default=[], metavar="FRAGMENT",
                    help="gate just the rows whose name contains this (repeatable)")
    ap.add_argument("--allow-stale", action="store_true",
                    help="compare rows last measured at other code instead of "
                         "failing them — for reading the ledger mid-tuning, never "
                         "for a release")
    ap.add_argument("--update", action="store_true",
                    help="accept the currently recorded numbers as the new "
                         "baseline and write it")
    ap.add_argument("--baseline", type=Path, default=BASELINE_PATH, metavar="FILE")
    ap.add_argument("--json-out", type=Path, default=None, metavar="FILE")
    args = ap.parse_args(argv)

    # Both tiers' names are "known", whichever tier is being gated. The baseline
    # holds the tier a release is cut on, and the *other* tier's gate must report
    # those rows as belonging to a depth it is not running — not as renamed or
    # dropped, which is a hard failure and would make the unused tier permanently
    # red for no reason anyone can act on.
    tiers = benchmark.manifest()
    known = {row.name for row in tiers} | {row.quick().name for row in tiers}
    whole = [row.quick() for row in tiers] if args.quick else tiers
    rows = benchmark.select(whole, only=args.only)
    if not rows:
        print("no rows selected")
        return 1

    if args.run:
        benchmark.main(bench_argv(quick=args.quick, only=args.only))
        print()

    previous = load_baseline(args.baseline)
    if args.update:
        # Built from the whole manifest rather than the selection: a --only
        # update would drop every unselected row from the file, quietly ungating
        # the rest of the bench.
        fresh = build_baseline(whole, previous=previous, known=known)
        write_baseline(fresh, args.baseline)
        print(f"accepted {len(fresh['rows'])} row(s) into {args.baseline}")
        return 0

    verdicts = check(previous, rows, known=known, allow_stale=args.allow_stale)
    accepted = previous.get("accepted_at", "never")
    print(f"quality gate: {len(verdicts)} row(s) against {args.baseline.name} "
          f"(accepted {accepted})")
    if args.allow_stale:
        print("  --allow-stale: rows measured at other code are compared anyway. "
              "Not a release check.")
    print()
    for verdict in verdicts:
        print(f"  {verdict.render()}")

    failed = [v for v in verdicts if v.failed]
    ungated = [v for v in verdicts if v.state == "ungated"]
    print()
    if ungated:
        print(f"{len(ungated)} row(s) ungated — no accepted numbers on this box.")
    if failed:
        print(f"FAIL: {len(failed)} row(s) — "
              + ", ".join(sorted({v.state for v in failed})))
        print("A real regression is fixed or reverted. A deliberate trade is "
              "accepted with --update, which puts the movement in the diff.")
    else:
        print(f"PASS: {len(verdicts) - len(ungated)} gated row(s) at or above baseline.")

    if args.json_out:
        args.json_out.write_text(json.dumps({
            "accepted_at": accepted,
            "verdicts": [{"row": v.row, "state": v.state, "detail": v.detail,
                          "deltas": [{"measure": d.measure, "baseline": d.baseline,
                                      "observed": d.observed, "delta": round(d.delta, 4),
                                      "tolerance": d.tolerance, "breached": d.breached}
                                     for d in v.deltas]}
                         for v in verdicts],
        }, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
