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

**Failing early.** A full pass is ~140 searches; scored to the end, every verdict
costs the same whether the ranking is fine or catastrophically broken. Three
mechanisms cut the failing case short, and they compose:

- ``--fail-early`` stops a file the moment its floor is *provably* out of reach —
  every unscored case counted as perfect still lands under the floor — and stops
  the run at the first file that breaches. Sound by construction: it can only
  abort a run that was going to fail, so it is safe on the CI path and not just
  in a tuning loop. It buys nothing on a passing run, which is the point: a
  passing run has to score everything.
- ``--max-regressions N`` is the aggressive companion, and needs the recorded
  per-case baseline: abort once ``N`` cases that *used to* find their gold stop
  finding it at all. Unlike the bound this is a heuristic — those N cases might
  have been paid for by gains elsewhere — so it answers "show me the damage fast"
  during tuning, not "is this shippable".
- Case **order**. With a baseline on hand each file is scored best-first, which
  is what makes the other two bite: the cases with the most to lose are the ones
  a regression shows in, and they are the ones whose failure drags the provable
  bound down fastest. Without a baseline the gate runs in file order and the
  bound simply tightens more slowly.

**Tuning through the gate.** ``--set field=value`` scores a candidate
``SearchParams`` instead of the shipped one, and ``--cache`` persists the
candidate *pools* between processes (:mod:`thread_archive._retrieval.pool_cache`)
— the arms and the fusion don't depend on ranking weights, so a second run at new
weights re-scores pools it already has. Together they turn "change a weight, wait
for a full scoring pass" into a loop worth running. Two rules keep the instrument
honest: an overridden run is flagged ``overrides`` in the ledger so an experiment
can't be read as a baseline movement, and it never writes the per-case baseline
file (nor does a subset or aborted run) — that reference belongs to the shipped
configuration alone.

**The speed axis.** ``--latency [REPS]`` adds warm-latency measurement over the
same queries (:mod:`thread_archive._ops.speed`), printing the joint quality+speed
report — the two numbers a ``--set`` decision trades between, since the
cross-encoder re-rank is both the top quality lever and the top latency. The pool
cache is forced OFF for the latency pass (the arms are the cost under
measurement); ``--latency`` and ``--cache`` therefore describe different runs. A
``latency-baseline.json`` and ``latency-runs.jsonl`` sit beside the quality
ledgers. With ``--fail-early`` a latency smoke test runs the ``--smoke-queries``
slowest-at-baseline queries against a p95 ceiling (``--budget-ms``, else 1.5× the
baseline) and bails before the full pass — the speed analog of the quality bound,
though a heuristic rather than sound (see ``evals/README.md`` → "The speed axis").
``--latency-smoke`` runs *only* that smoke test — a ~1-minute speed check for the
interactive loop, the full ~10-minute pass reserved for the confirm.

Snapshot binding: each gold file is bound by ``snapshot_id`` to the corpus
snapshot it was mined against; this gate scores over that snapshot
(``THREAD_ARCHIVE_SNAP``, default ``~/.thread/archive-snap``). When the snapshot
is absent, or a gold file's id no longer matches it (the corpus moved, the golds
are mid-re-mine), the affected file is SKIPPED, not failed — a stale or missing
fixture is an operator-maintenance state, not a code regression, and must not
wedge the commit gate red. A file that is present and fresh but carries no floor
entry is scored and reported but left ungated (newly mined, not yet calibrated):
add its floor below to start gating it.

**Failing closed.** The skip-don't-wedge default is right for a dev box that has
no snapshot, but on a box that is *supposed* to measure — the CI lane — a silent
skip is a gate that stopped measuring and still reads green. ``--require`` closes
that: the calibrated manifest (every basename in :data:`FLOORS`) must be present,
fresh, readable, and actually scored, and an absent snapshot, an empty gold dir,
or any expected file missing / stale / unreadable / unscored is a **failure**, not
a skip. A genuine maintenance window still needs to pass CI, so it takes an
explicit, visible skip rather than looking like a pass: set
``THREAD_ARCHIVE_GOLD_GATE_MAINTENANCE=<reason>`` and ``--require`` prints a loud
``MAINTENANCE SKIP`` and exits 0. The reason is the audit trail — a skip nobody
declared can't happen.

Usage: python scripts/retrieval_gold_gate.py [--require]
Exit 0 when every calibrated floor holds (or, without ``--require``, its fixture
is absent/stale); exit 1 listing each breach — under ``--require`` that includes
any calibrated fixture that failed to be present, fresh, and scored.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

# Gold-file basename -> floors for complementary failure modes: MRR protects the
# first good answer, success@10 protects whether any answer remains reachable,
# true recall@10 protects coverage of every known grade-2 answer, and nDCG@10
# protects the ordering of the whole 2/1/0 graded pool. A regression ratchet, not
# an aspiration: raise a floor when a shipped change lifts the measured number,
# never lower one to accommodate a change.
#
# How much headroom: scoring is deterministic — same code, same snapshot, same
# numbers to the digit (the run ledger shows repeat runs agreeing exactly), so
# headroom is not absorbing measurement noise. It is a tolerance, and its natural
# unit is one case: on a file of ``n`` cases, a single case falling from rank 1 to
# unfound moves any of these metrics by at most ``1/n``. Each floor therefore sits
# ``1/n`` under its measured value, rounded down — one case may regress, two fail
# the gate. Small files buy the widest headroom because a case is worth more there
# (a 7-case topic file tolerates 0.143; the 64-case findability file, 0.016), which
# is why the absolute gaps below differ so much between files.
FLOORS: dict[str, dict[str, float]] = {
    "judged-cases.jsonl": {
        "mrr": 0.40, "success10": 0.90, "recall10": 0.85, "ndcg10": 0.51,
    },
    "topic-cases-suicide.jsonl": {
        "mrr": 0.78, "success10": 0.85, "recall10": 0.78, "ndcg10": 0.59,
    },
    "topic-cases-frustration.jsonl": {
        "mrr": 0.50, "success10": 0.71, "recall10": 0.50, "ndcg10": 0.45,
    },
    "topic-cases-needle.jsonl": {
        "mrr": 0.66, "success10": 0.80, "recall10": 0.44, "ndcg10": 0.50,
    },
    "topic-cases-context-compaction.jsonl": {
        "mrr": 0.90, "success10": 0.90, "recall10": 0.65, "ndcg10": 0.59,
    },
    # querygen findability — one gold/case, so recall10 tracks success10 (recover
    # the single target = surface it). The query is authored from the thread, so
    # these are paraphrase-match cases: the lexical arm has the least purchase here
    # and the fusion weight (params.fusion_weight) carries them, which makes this
    # the file that moves first when cross-arm agreement is weighted wrong. Recall
    # sits above what the cross-encoder reached; the head order is what still
    # trails it.
    #
    # This file carries difficulty tiers (verbatim / paraphrase / vague); the gate
    # scores and prints them apart, and a per-stratum floor goes under
    # ``by_difficulty`` here (see ``check_floors``) to keep an easy-verbatim
    # aggregate from masking a vague-recall collapse. Left unset until the tiers
    # are re-mined one-per-thread — the current fixture over-weights a few threads
    # with duplicate tiers, and a floor calibrated on that skew would bake it in.
    "findability-cases.jsonl": {
        "mrr": 0.65, "success10": 0.90, "recall10": 0.90, "ndcg10": 0.71,
    },
    # rerank in-pool judgments — many golds/case (avg ~14), so recall10 is
    # structurally capped (can't fit ~14 golds in 10 slots) and floored low on
    # purpose; success10 and nDCG10 are the load-bearing signals here.
    "rerank-cases.jsonl": {
        "mrr": 0.57, "success10": 0.84, "recall10": 0.44, "ndcg10": 0.60,
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
    four complementary signals.

    A floor may also carry a ``by_difficulty`` sub-map — ``{tier: {metric:
    floor}}`` — checked against the report's per-stratum numbers
    (``report["per_difficulty"]``). This is the guard against an aggregate floor
    masking the weakest stratum: a findability file whose ``vague`` MRR collapses
    can still clear an aggregate floor carried by its easy ``verbatim`` cases, so
    the tier that product recall actually lives in gets its own floor."""
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
    per_difficulty = report.get("per_difficulty") or {}
    for tier, tier_floor in floor.get("by_difficulty", {}).items():
        measured = per_difficulty.get(tier)
        if measured is None:
            breaches.append(f"{name}: {tier} tier floored but absent from the report")
            continue
        for metric, lo in tier_floor.items():
            val = measured.get(metric)
            if val is not None and val < lo:
                breaches.append(
                    f"{name}: {tier} {metric} {val:.3f} < floor {lo}")
    return breaches


def _print_strata(strata: dict[str, dict], floors: dict[str, dict] | None = None) -> None:
    """Print a difficulty-laddered file's per-tier numbers under its aggregate
    line, so the weakest stratum (``vague`` recall is where product findability
    lives) is visible next to the mean instead of hidden inside it. ``floors``
    annotates any tier that carries a per-stratum floor."""
    if not strata:
        return
    floors = floors or {}
    for tier in sorted(strata):
        m = strata[tier]
        tf = floors.get(tier, {})
        fl = (f"  [floor mrr {tf.get('mrr', '-')} s@10 {tf.get('success10', '-')}]"
              if tf else "")
        print(f"      {tier:11s} n {m['n']:>3}  MRR {m['mrr']:.3f}  "
              f"S@10 {m['success10']:.3f}  R@10 {m['recall10']:.3f}  "
              f"nDCG@10 {m['ndcg10']:.3f}{fl}", flush=True)


class FloorWatch:
    """The ``--fail-early`` predicate for one gold file: abort as soon as the
    verdict is settled.

    Two independent triggers, both fed the running state
    (:class:`~thread_archive._eval.EvalProgress`) after every case:

    *The bound.* Each metric's per-case contribution is capped at 1.0, so the
    highest value still reachable is ``(running sum + unscored cases) / n``. Once
    that drops below the floor, no ordering of the remaining cases can save the
    run, and scoring them buys nothing but latency. This is exact — it never
    aborts a run that would have passed — which is what lets it default on rather
    than being a tuning-only shortcut.

    *The regression count* (``max_regressions``, opt-in). ``baseline`` maps a
    query to what it scored under the shipped configuration; a case that scored
    above zero there and zero now is a case that stopped finding its gold
    entirely. N of those aborts the file. Deliberately impatient and not sound —
    a change can lose N cases and win more elsewhere — so it belongs to the
    tuning loop, where the useful answer is "here is the damage" as soon as the
    damage exists.
    """

    def __init__(self, floor: dict[str, float], *,
                 baseline: dict[str, float] | None = None,
                 max_regressions: int | None = None) -> None:
        self.floor = floor
        self.baseline = baseline or {}
        self.max_regressions = max_regressions
        self.regressions: list[str] = []

    def __call__(self, progress) -> str | None:
        if self.max_regressions is not None and self.baseline:
            if progress.case_rr == 0.0 and self.baseline.get(progress.query, 0.0) > 0.0:
                self.regressions.append(progress.query)
                if len(self.regressions) >= self.max_regressions:
                    return (f"{len(self.regressions)} cases that used to rank now "
                            f"find nothing (after {progress.scored}/{progress.n})")
        for metric, floor in self.floor.items():
            best = progress.best_possible(metric)
            if best < floor:
                return (f"{metric} cannot reach floor {floor} — at best "
                        f"{best:.3f} after {progress.scored}/{progress.n} cases")
        return None


def _order_cases(cases: list[dict], baseline: dict[str, float]) -> list[dict]:
    """Best-baseline-first, so a regression surfaces in the first searches rather
    than the last. The cases with the most to lose both fail most visibly and drag
    :meth:`FloorWatch` bound down fastest; cases with no baseline (newly mined) go
    last, since nothing is known about what they should score. Order affects only
    *when* a run aborts, never a completed run's metrics — they are means."""
    if not baseline:
        return cases
    return sorted(cases, key=lambda c: -baseline.get(c["query"], -1.0))


def _candidate_search(params):
    """The search callable a run scores through: production as-is (``None``
    params), or a closure that passes the candidate ``SearchParams`` — the one
    seam ``--set`` flows through, shared by the quality pass and the latency
    pass so both measure the *same* configuration."""
    if params is None:
        return None
    from thread_archive._retrieval import search as production

    def search(query, **kw):
        return production(query, params=params, **kw)

    return search


def _score(cases: list[dict], *, params=None, early_stop=None) -> dict:
    """Score cases with the production search over the snapshot home. Imported
    lazily so the skip paths (absent snapshot, no gold files) stay import-free
    and fixture-free. A broken search pipeline raises here — and *should* fail
    the gate, unlike a stale/missing fixture, which is handled as a skip.

    ``params`` overrides the shipped ``SearchParams`` (the ``--set`` tuning path);
    ``early_stop`` is the abort predicate."""
    from thread_archive._eval import evaluate

    return evaluate(cases, limit=20, rerank=None, content_type=None,
                    exclude_content_types=None, search=_candidate_search(params),
                    early_stop=early_stop)


def _print_latency(stats, baseline) -> None:
    """The joint report's speed half: the warm distribution, per stage and per
    query shape, with a delta against the recorded baseline where one exists."""
    from thread_archive._ops import speed

    def delta(cur: float, key: str) -> str:
        if not baseline:
            return ""
        b = baseline.get("total", {}).get(key)
        return f" ({cur - b:+.0f})" if b else ""

    t = stats.total
    print(f"  total     p50 {t['p50']:6.0f}{delta(t['p50'], 'p50')}  "
          f"p95 {t['p95']:6.0f}{delta(t['p95'], 'p95')}  p99 {t['p99']:6.0f}ms   "
          f"(rerank fired {stats.rerank_rate:.0%}, pool p50 {stats.pool_p50:.0f})", flush=True)
    for stage in speed.STAGES:
        st = stats.stages[stage]
        print(f"    {stage:11s} p50 {st['p50']:6.0f}  p95 {st['p95']:6.0f}", flush=True)
    for shape, m in stats.by_shape.items():
        print(f"    shape {shape:14s} n={int(m['n']):4d}  p50 {m['p50']:6.0f}  "
              f"p95 {m['p95']:6.0f}", flush=True)


def _latency_smoke(baseline, *, params, k: int, budget_ms, reps: int) -> str | None:
    """The pathological-query smoke test — the speed analog of quality's
    fail-early. Runs the ``k`` slowest-at-baseline queries against a p95 ceiling
    and returns an abort reason if the candidate blows it, before the full pass.
    Impatient and not sound (a change could make a baseline-fast query the new
    slow one), so it belongs to the tuning loop: it catches the common regression
    — a uniform slowdown, or a worsened heavy path — in tens of seconds. ``None``
    when there's nothing to check (no baseline to cherry-pick from, or no
    ceiling)."""
    from thread_archive._ops import speed

    queries = speed.smoke_set(baseline, k)
    ceiling = speed.ceiling_ms(baseline, budget_ms=budget_ms, factor=1.5)
    if not queries or ceiling is None:
        return None
    print(f"latency smoke: {len(queries)} slowest-at-baseline queries, "
          f"ceiling p95 {ceiling:.0f}ms", flush=True)
    stats = speed.measure(queries, search=_candidate_search(params), reps=reps)
    p95 = stats.total["p95"]
    ok = "ok" if p95 <= ceiling else "OVER"
    print(f"  smoke p50 {stats.total['p50']:.0f}  p95 {p95:.0f}ms  {ok}", flush=True)
    if p95 > ceiling:
        return (f"latency smoke p95 {p95:.0f}ms > ceiling {ceiling:.0f}ms on the "
                f"{len(queries)} pathological queries")
    return None


def _build_coherence_graph() -> None:
    """Build the corpus graph inline, before any case is scored.

    The community-coherence re-rank reads a cached graph and returns ``None``
    while a background build is still running, so on the request path a cold
    process simply ranks without the boost for its first queries. Under a scoring
    loop that same behaviour is a race against the scorer: the build lands partway
    through, the cases before it are ranked without coherence and the cases after
    it with, and where the boundary falls depends on wall-clock — how fast the box
    is, whether the pools came from a cache. Two runs of identical code then
    disagree, and every floor calibrated from them inherits the split.

    Building inline first costs one build and makes a run a function of the code
    and the snapshot alone. Fail-soft and a no-op when coherence is off or the
    graph can't be built — both leave every case ranked the same way, which is the
    property that matters."""
    from thread_archive._retrieval import embed_graph

    if embed_graph.coherence_gamma() <= 0.0:
        return
    print("gold gate: building corpus graph (coherence)...", flush=True)
    try:
        embed_graph.get(block=True)
    except Exception as exc:  # noqa: BLE001 — scoring without it beats not scoring
        print(f"gold gate: corpus graph unavailable ({exc}); "
              f"scoring without the coherence re-rank", flush=True)


def _run_scored(cases: list[dict], *, params, early_stop, cache) -> dict:
    """:func:`_score` with the pool cache installed for the duration, when there
    is one. Installed per file rather than around the whole run so the context
    slot is restored even if a file raises."""
    if cache is None:
        return _score(cases, params=params, early_stop=early_stop)
    from thread_archive._retrieval import pool_cache

    with pool_cache.install(cache):
        return _score(cases, params=params, early_stop=early_stop)


def _load(path: Path) -> list[dict]:
    from thread_archive._eval import load_case_file

    return load_case_file(path)


def _apply_overrides(assignments: list[str]):
    """Build a ``SearchParams`` from ``field=value`` strings — the ``--set`` half
    of the tuning loop. Values are coerced to the dataclass field's declared type,
    so ``--set fusion_weight=500`` yields a float and ``--set rerank_auto=true`` a
    bool. An unknown field is an error rather than a silently ignored typo: a
    mis-typed knob that scores identically to the baseline is indistinguishable
    from a knob that does nothing."""
    from thread_archive._retrieval import SearchParams

    fields = {f.name: f for f in dataclasses.fields(SearchParams)}
    overrides: dict[str, object] = {}
    for raw in assignments:
        if "=" not in raw:
            raise SystemExit(f"--set expects field=value, got {raw!r}")
        name, _, value = raw.partition("=")
        name, value = name.strip(), value.strip()
        if name not in fields:
            raise SystemExit(
                f"--set: no SearchParams field {name!r}; "
                f"known: {', '.join(sorted(fields))}")
        declared = fields[name].type
        try:
            if "bool" in str(declared):
                overrides[name] = value.lower() in ("1", "true", "yes", "on")
            elif "int" in str(declared) and "float" not in str(declared):
                overrides[name] = int(value)
            else:
                overrides[name] = float(value)
        except ValueError:
            raise SystemExit(f"--set {name}: cannot read {value!r} as {declared}") from None
    return dataclasses.replace(SearchParams(), **overrides), overrides


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
    ap.add_argument("--fail-early", action="store_true",
                    help="stop a file once its floor is provably unreachable, and the "
                    "run at the first breach (exact — never aborts a passing run)")
    ap.add_argument("--max-regressions", type=int, default=None, metavar="N",
                    help="also abort a file after N cases that ranked at the recorded "
                    "baseline stop ranking at all (impatient, not sound; for tuning)")
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="FIELD=VALUE",
                    help="score a candidate SearchParams field instead of the shipped "
                    "value (repeatable); flags the ledger row as an experiment")
    ap.add_argument("--cache", nargs="?", const="", default=None, metavar="PATH",
                    help="reuse candidate pools across runs (default path under the "
                    "archive home) — the arms don't depend on ranking weights, so a "
                    "re-run at new --set values skips them")
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="score just these gold files (substring match, repeatable)")
    ap.add_argument("--require", action="store_true",
                    help="fail closed: every calibrated gold file (the FLOORS "
                    "manifest) must be present, fresh, and scored — an absent "
                    "snapshot or a missing/stale fixture is a failure, not a skip. "
                    "Set THREAD_ARCHIVE_GOLD_GATE_MAINTENANCE=<reason> for an "
                    "explicit, visible maintenance skip.")
    ap.add_argument("--latency", nargs="?", type=int, const=3, default=None, metavar="REPS",
                    help="also measure warm latency over the same queries (REPS timed "
                    "runs each, default 3, pool cache OFF) and print the joint "
                    "quality+speed report — the speed axis of a --set tuning decision")
    ap.add_argument("--latency-smoke", action="store_true",
                    help="quick speed check: run ONLY the pathological-query smoke test "
                    "(seconds), skipping the full ~10-min latency pass — the "
                    "interactive-iteration counterpart of --latency")
    ap.add_argument("--budget-ms", type=float, default=None, metavar="MS",
                    help="absolute p95 ceiling for the latency smoke test (default: "
                    "1.5x the recorded latency baseline's p95)")
    ap.add_argument("--smoke-queries", type=int, default=8, metavar="K",
                    help="how many slowest-at-baseline queries the latency smoke test "
                    "runs before the full pass (with --latency --fail-early)")
    # None (a programmatic main() call — tests, importers) means "no CLI args", not
    # "read sys.argv" (which under pytest is the test runner's argv). The __main__
    # entry passes the real argv explicitly.
    args = ap.parse_args(argv if argv is not None else [])
    if args.history is not None:
        return _print_history(args.history or None)

    params, overrides = _apply_overrides(args.overrides) if args.overrides else (None, {})
    # max-regressions needs cases ordered by baseline to be worth anything, and
    # ordering is the fail-early machinery; asking for one implies the other.
    fail_early = args.fail_early or args.max_regressions is not None

    # Fail-closed CI mode. --require scores the whole calibrated manifest, so a
    # subset (--only) would make "everything scored" unprovable by construction.
    require = args.require
    if require and args.only:
        raise SystemExit("--require scores the full calibrated manifest; drop --only")
    maintenance = os.environ.get("THREAD_ARCHIVE_GOLD_GATE_MAINTENANCE", "").strip()
    if require and maintenance:
        # The declared-maintenance escape: loud and distinct from a pass, so a
        # window where the fixture is intentionally down is an operator decision
        # on the record, not a gate that quietly stopped measuring.
        print(f"gold gate: MAINTENANCE SKIP — {maintenance} "
              f"(explicit operator skip; not a pass)")
        return 0

    snap = Path(os.environ.get("THREAD_ARCHIVE_SNAP", str(DEFAULT_SNAP)))
    gold_dir = Path(os.environ.get("THREAD_ARCHIVE_GOLD_DIR", str(DEFAULT_GOLD_DIR)))
    # The real archive home, captured before the scoring override below repoints
    # THREAD_ARCHIVE_HOME at the frozen snapshot — the run ledger lands here, beside
    # the other home-root ledgers, not inside the snapshot fixture.
    home = Path(os.environ.get("THREAD_ARCHIVE_HOME") or str(DEFAULT_GOLD_DIR))

    manifest = snap / "snapshot.json"
    if not manifest.is_file():
        if require:
            print(f"gold gate: no snapshot at {snap} — FAIL (--require: the "
                  f"calibrated fixture must be present and fresh; set "
                  f"THREAD_ARCHIVE_GOLD_GATE_MAINTENANCE=<reason> to skip during "
                  f"maintenance)")
            return 1
        print(f"gold gate: no snapshot at {snap} — skipping "
              f"(fixture absent; re-snapshot + re-mine to restore)")
        return 0
    current = json.loads(manifest.read_text()).get("snapshot_id")
    # Route the production search at the frozen snapshot for the scoring below.
    os.environ["THREAD_ARCHIVE_HOME"] = str(snap)

    files = discover_gold_files(gold_dir)
    if args.only:
        files = [p for p in files if any(frag in p.name for frag in args.only)]
    if not files:
        if require:
            print(f"gold gate: no gold files in {gold_dir} — FAIL (--require; "
                  f"expected {', '.join(sorted(FLOORS))})")
            return 1
        print(f"gold gate: no gold files in {gold_dir} — skipping")
        return 0

    _build_coherence_graph()

    from thread_archive._ops import gold_runs

    # Per-case reference for the fail-early ordering and the regression watch.
    # Bound to this snapshot: scores from a different corpus describe different
    # documents. Absent (first run, or a fresh snapshot) the gate simply runs in
    # file order — slower to abort, identical verdict.
    baseline = gold_runs.read_baseline(home, snapshot_id=current)

    cache = None
    if args.cache is not None:
        from thread_archive._retrieval import pool_cache

        cache_path = Path(args.cache) if args.cache else (
            home / "gold-pool-cache" / f"{current}.pkl")
        cache = pool_cache.PoolCache(namespace=current or "", path=cache_path)
        print(f"gold gate: pool cache {cache_path} ({len(cache)} pools)", flush=True)

    if overrides:
        print(f"gold gate: TUNING RUN — {overrides} (not a baseline)", flush=True)

    # Speed fail-fast. The smoke runs the corpus's pathological queries against a
    # ceiling in seconds, before the slow work: standalone (--latency-smoke, the
    # interactive quick check) or as the front gate of a full --latency --fail-early
    # pass. Either way a breach bails here — the point is not paying the full pass
    # for a config already over budget.
    want_full_latency = args.latency is not None
    want_smoke = args.latency_smoke or (want_full_latency and fail_early)
    latency_baseline = None
    if want_smoke or want_full_latency:
        from thread_archive._ops import speed

        latency_baseline = speed.read_baseline(home, snapshot_id=current)
    if want_smoke:
        if latency_baseline is None:
            print("latency smoke: no baseline recorded — run a full --latency pass "
                  "once to seed it (nothing to cherry-pick pathological queries from)",
                  flush=True)
        else:
            reason = _latency_smoke(latency_baseline, params=params,
                                    k=args.smoke_queries, budget_ms=args.budget_ms,
                                    reps=args.latency or 3)
            if reason:
                print(f"\nlatency regression: {reason}")
                return 1

    breaches: list[str] = []
    scored = 0
    partial = False
    watch: FloorWatch | None = None
    measured: dict[str, dict] = {}  # per-file metrics for the run ledger
    per_case: dict[str, dict[str, float]] = {}  # for the baseline file
    latency_queries: list[str] = []  # the query set the --latency pass measures over
    # What happened to each calibrated (FLOORS) file — the input to the --require
    # manifest check. A file never reaching "scored" (missing, stale, unreadable,
    # or aborted) is a fail-closed breach; unread here means "missing".
    disposition: dict[str, str] = {}
    for path in files:
        name = path.name
        try:
            row = _first_row(path)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  {name:34s} WARN — unreadable ({exc}); skipping", flush=True)
            if name in FLOORS:
                disposition[name] = f"unreadable ({exc})"
            continue
        sid = row.get("snapshot_id") if row else None
        if sid != current:
            print(f"  {name:34s} SKIP — snapshot {sid} != {current} (stale / mid-re-mine)",
                  flush=True)
            if name in FLOORS:
                disposition[name] = f"stale (snapshot {sid} != {current})"
            continue

        cases = _load(path)
        latency_queries.extend(c["query"] for c in cases)
        floor = FLOORS.get(name)
        watch = None
        if fail_early and floor:
            cases = _order_cases(cases, baseline.get(name, {}))
            watch = FloorWatch(floor, baseline=baseline.get(name, {}),
                               max_regressions=args.max_regressions)
        report = _run_scored(cases, params=params, early_stop=watch, cache=cache)
        mrr = report["mrr"]
        s10 = report["success"][10]
        r10 = report["recall"][10]
        n10 = report["ndcg"][10]
        aborted = report.get("aborted")
        per_case[name] = {c["query"]: c["rr"] for c in report.get("per_case", [])}
        strata = report.get("per_difficulty") or {}
        # Record every scored file — gated or not — with its full metric row.
        fb = check_floors(name, report, floor) if floor else []
        measured[name] = {
            "n": report["n"], "mrr": round(mrr, 4), "success10": round(s10, 4),
            "recall10": round(r10, 4), "ndcg10": round(n10, 4),
            "p50_ms": round(report["latency_p50_ms"], 1),
            "status": ("aborted" if aborted else
                       ("below_floor" if fb else "ok") if floor else "ungated"),
        }
        if strata:
            # The per-tier numbers a difficulty-laddered file (querygen
            # findability) carries — recorded so a vague/paraphrase stratum's
            # trend is on the timeseries, not lost inside the aggregate.
            measured[name]["per_difficulty"] = {
                tier: {k: round(v, 4) for k, v in m.items() if k != "n"}
                | {"n": m["n"]}
                for tier, m in strata.items()
            }
        if aborted:
            # An aborted file's metrics are lower bounds over a prefix, not
            # averages — say so where they print, and flag the ledger row, so
            # neither reads as a comparable number later.
            partial = True
            measured[name]["scored"] = report["scored"]
            if name in FLOORS:
                disposition[name] = f"aborted after {report['scored']}/{report['n']}"
            print(f"  {name:34s} ABORTED after {report['scored']}/{report['n']} — {aborted}",
                  flush=True)
            breaches.append(f"{name}: {aborted}")
            scored += 1
            break
        if floor is None:
            print(f"  {name:34s} MRR {mrr:.3f}  S@10 {s10:.3f}  "
                  f"R@10 {r10:.3f}  nDCG@10 {n10:.3f}  "
                  f"ungated (no floor — add one to gate)", flush=True)
            _print_strata(strata)
            continue
        status = "BELOW FLOOR" if fb else "ok"
        print(f"  {name:34s} MRR {mrr:.3f} (floor {floor['mrr']})  "
              f"S@10 {s10:.3f} (floor {floor.get('success10', '-')})  "
              f"R@10 {r10:.3f} (floor {floor.get('recall10', '-')})  "
              f"nDCG@10 {n10:.3f} (floor {floor.get('ndcg10', '-')})  {status}", flush=True)
        _print_strata(strata, floor.get("by_difficulty", {}))
        disposition[name] = "scored"
        breaches += fb
        scored += 1
        if fb and fail_early:
            # The verdict is already 1; the remaining files can only add detail.
            partial = True
            break

    if cache is not None:
        cache.save()
        print(f"gold gate: pool cache {cache.hits} hit / {cache.misses} miss "
              f"({cache.hit_rate:.0%}), {len(cache)} pools saved", flush=True)

    # Fail-closed manifest check: under --require every calibrated file must have
    # reached "scored". A file that was missing (never in disposition), stale,
    # unreadable, or aborted is a breach — the gate can't report green while a
    # fixture it is supposed to defend silently stopped being measured. Computed
    # before the ledger write so its `passed` flag reflects the failure.
    if require:
        for expected in sorted(FLOORS):
            if disposition.get(expected) != "scored":
                why = disposition.get(expected, "missing (not present in gold dir)")
                breaches.append(
                    f"{expected}: expected a fresh calibrated fixture, but {why}")

    # Record the run whenever anything was measured (gated or ungated) — the
    # timeseries wants the ungated files' numbers too, so a floor can be
    # calibrated from history. Fail-soft: never breaks the gate's verdict.
    if measured:
        gold_runs.record_run(
            home, snapshot_id=current, files=measured, passed=not breaches,
            config=gold_runs.active_config(params), overrides=overrides or None,
        )
    # The per-case baseline describes the shipped configuration, whole and
    # passing. A tuning run scores a different ranking, a --only run a subset, an
    # aborted run a prefix, and a breaching run the regression itself — each would
    # silently redefine the reference every later run's ordering and regression
    # watch is read against, and the last would quietly absorb the very failure
    # the watch exists to name.
    clean_full_run = not overrides and not args.only and not partial and not breaches
    if per_case and clean_full_run:
        gold_runs.write_baseline(home, snapshot_id=current, files=per_case)

    # The speed half of the joint report: warm latency over the same queries the
    # quality pass covered (whole files, even where quality scored only a prefix),
    # pool cache off. Prints the distribution with a delta against the recorded
    # latency baseline, records the timeseries, and — on a clean full shipped run —
    # refreshes that baseline, exactly mirroring the quality side.
    if args.latency is not None and latency_queries:
        from thread_archive._ops import speed

        n = len(latency_queries)
        print(f"\nlatency: {n} queries × {args.latency} reps, warm, pool cache off "
              f"(~{n * (args.latency + 1)} searches)", flush=True)

        def _progress(i: int, total: int) -> None:
            if i and i % 25 == 0:
                print(f"  latency {i}/{total}...", flush=True)

        stats = speed.measure(latency_queries, search=_candidate_search(params),
                              reps=args.latency, on_query=_progress)
        _print_latency(stats, latency_baseline)
        speed.record_run(home, snapshot_id=current, stats=stats,
                         config=gold_runs.active_config(params),
                         overrides=overrides or None)
        if clean_full_run:
            speed.write_baseline(home, snapshot_id=current, stats=stats)

    if scored == 0 and not breaches:
        print("gold gate: no fresh calibrated gold files — skipping "
              "(all stale or ungated)")
        return 0
    if breaches:
        print("\nretrieval-quality regression:")
        for b in breaches:
            print(f"  {b}")
        if watch is not None and watch.regressions:
            print("  cases that ranked at the baseline and now find nothing:")
            for q in watch.regressions:
                print(f"    {q!r}")
        return 1
    print(f"\ngold gate: {scored} file(s) above floor")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
