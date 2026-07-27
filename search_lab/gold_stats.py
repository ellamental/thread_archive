#!/usr/bin/env python3
"""What is actually in a mined case file — the gold's own quality panel.

A bench instrument, read deliberately::

    .venv/bin/python search_lab/gold_stats.py <cases.jsonl>
    .venv/bin/python search_lab/gold_stats.py <cases.jsonl> --json

Every other module in this lab measures *search*. This one measures the
**benchmark**, which is a different question and the one nothing answered: a
mined file is agent output, and agent output is not gold because a miner wrote it
there. A case file can be internally fine and still measure almost nothing —
every case single-gold, every query the same sentence with the subject swapped,
every case drawn from one repo — and none of that is visible in a score. It is
visible here.

Four panels, and each exists because a specific failure was found by hand and
should never have needed to be:

:func:`yield_and_cost`
    What the run spent and what it dropped, off the mining ledger
    (:mod:`search_lab.mine_runs`) and the detail sidecar. Cases minted over units
    drawn is the yield; the drop breakdown is the denominator that says whether
    the file is a sample of the corpus or a sample of what one agent felt able to
    write about.

:func:`pool_shape`
    Gold-set and graded-pool sizes. A file of single-gold cases measures
    findability and nothing about completeness, and a file whose ``grades`` hold
    exactly one document is scoring nDCG over a pool of one — which reads as a
    real ordering metric and is not one.

:func:`query_shape`
    The distribution the queries are drawn from: length, and how concentrated
    their openings are. **This is the panel that catches a template.** An
    authoring agent handed one worked example will parrot it, and a file of 25
    queries opening "that time we…" is one phrasing with 25 fillers rather than 25
    samples of how anyone searches — which inflates nothing and invalidates
    everything, silently, because each individual query looks fine.

:func:`coverage`
    How the cases spread over whatever they were drawn from (repo, path,
    directory). Corpus supply is power-law skewed, so an unstratified file
    measures one codebase; the concentration number says whether stratification
    held.

Panels needing the corpus (lexical leakage between a query and its gold) are
skipped when no archive is open rather than failing — this runs against a file on
disk, and a file outlives the home it was mined from.

Reads only. It never rewrites a case file: a benchmark that edits itself in
response to its own quality panel is not a benchmark.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

#: Openings compared at this many words. Three is where a recall formula lives
#: ("that time we", "the session where") — one or two words catch every question
#: that starts "how", and four splits the template back into distinct strings.
OPENING_WORDS = 3

#: A distinctive identifier: snake_case, camelCase, or a dotted filename. What the
#: ``literal`` tier promises to name and the other tiers promise to avoid, so it is
#: the token class that decides whether a tier's contract held.
IDENTIFIER = re.compile(r"\b(?:[a-z]+_[a-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]+"
                        r"|[A-Za-z0-9_]+\.[a-z]{1,5})\b")

WORD = re.compile(r"[a-z0-9_]{3,}")


# ── loading ─────────────────────────────────────────────────────────────────

def read_jsonl(path: Path) -> list[dict]:
    """Rows of a JSONL file, skipping blanks and junk. A half-written mining run
    leaves a torn last line, and refusing to read the other 300 cases over it would
    make the panel useless exactly when something has gone wrong."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def detail_path_for(cases_path: Path) -> Path:
    """The reasoning sidecar beside a case file — the same rule the miners write
    by (:func:`search_lab.mine._framework.detail_path_for`), restated here so this
    instrument reads a file without importing the miner that wrote it."""
    return cases_path.with_name(cases_path.stem + "-detail.jsonl")


# ── small statistics ────────────────────────────────────────────────────────

def spread(values: Iterable[float]) -> dict[str, Any]:
    """min / p25 / median / p75 / max / mean over a sample, or an empty dict.

    Quantiles rather than a mean alone because every distribution on this page is
    skewed — one 200-word query or one 40-thread gold set moves a mean and tells a
    reader nothing about the body of the file."""
    xs = np.asarray([v for v in values if v is not None], dtype=float)
    if xs.size == 0:
        return {}
    return {"n": int(xs.size), "min": float(xs.min()),
            "p25": float(np.percentile(xs, 25)), "median": float(np.median(xs)),
            "p75": float(np.percentile(xs, 75)), "max": float(xs.max()),
            "mean": round(float(xs.mean()), 2)}


def concentration(counts: Counter) -> dict[str, Any]:
    """How much of a distribution sits on its most common value.

    ``top_share`` is the blunt number and ``entropy`` the calibrated one: Shannon
    entropy over the observed distribution, divided by the entropy of a uniform
    distribution over the same number of distinct values. 1.0 is perfectly spread,
    and low means the mass is piled up — the shape a template makes. Normalizing
    matters because raw entropy rises with sample size, so an unnormalized number
    would say a bigger file is always more diverse."""
    total = sum(counts.values())
    if not total:
        return {}
    probs = np.array([c / total for c in counts.values()], dtype=float)
    h = float(-(probs * np.log(probs)).sum())
    h_max = math.log(len(counts)) if len(counts) > 1 else 0.0
    top, top_n = counts.most_common(1)[0]
    return {"distinct": len(counts), "total": total,
            "top": top, "top_n": top_n, "top_share": round(top_n / total, 3),
            "entropy": round(h / h_max, 3) if h_max else 0.0}


def tokens(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


# ── panels ──────────────────────────────────────────────────────────────────

def yield_and_cost(cases: list[dict], details: list[dict],
                   runs: list[dict]) -> dict[str, Any]:
    """What the file cost and what it dropped.

    Yield is cases per unit *drawn*, not per unit that succeeded: a miner that
    silently discards two thirds of its sample is not producing a sample of the
    corpus, and only the denominator shows it. Cost comes off the detail sidecar's
    per-agent records, so it is what was actually billed rather than an estimate
    from a price list."""
    out: dict[str, Any] = {"cases": len(cases)}
    costs = [_agent_cost(d) for d in details]
    costs = [c for c in costs if c]
    if costs:
        out["cost_usd"] = {"total": round(sum(costs), 2),
                           "per_unit": round(sum(costs) / len(costs), 3),
                           "per_case": round(sum(costs) / len(cases), 3)
                           if cases else None}
    outcomes = Counter(d["outcome"] for d in details if d.get("outcome"))
    if outcomes:
        out["outcomes"] = dict(outcomes.most_common())
        drawn = sum(outcomes.values())
        out["units_drawn"] = drawn
        out["cases_per_unit"] = round(len(cases) / drawn, 2) if drawn else None
        out["drop_rate"] = round(1 - outcomes.get("ok", 0) / drawn, 3) if drawn else None
    mine_runs = [r for r in runs if r.get("kind") == "mine-run"]
    if mine_runs:
        out["runs"] = len(mine_runs)
        # The funnel is the newest run's, not a sum: stage counts across runs with
        # different targets do not add into anything meaningful.
        newest = mine_runs[0]
        if newest.get("funnel"):
            out["funnel"] = newest["funnel"]
            out["funnel_at"] = newest.get("at")
    return out


def _agent_cost(detail: dict) -> float:
    """The dollar cost recorded on one detail row, whichever key the miner used
    (``agent`` for the commit miner, ``stats`` for the others)."""
    for key in ("agent", "stats"):
        block = detail.get(key)
        if isinstance(block, dict) and block.get("cost_usd"):
            return float(block["cost_usd"])
    return 0.0


def pool_shape(cases: list[dict]) -> dict[str, Any]:
    """Gold-set and graded-pool sizes — whether the file can measure completeness,
    and whether its ordering metrics have a pool to order.

    ``single_gold`` and ``pool_of_one`` are the two failure modes worth naming
    outright. A single-gold file reads recall@k and success@k as the same number.
    A file whose graded pool is one document scores nDCG over nothing — the metric
    computes, and it is not measuring ordering."""
    golds = [len(c.get("gold") or []) for c in cases]
    pools = [len(c.get("grades") or {}) for c in cases]
    out: dict[str, Any] = {"gold_set": spread(golds), "graded_pool": spread(pools)}
    if cases:
        out["single_gold"] = sum(1 for g in golds if g == 1)
        out["single_gold_share"] = round(sum(1 for g in golds if g == 1) / len(cases), 3)
        out["pool_of_one"] = sum(1 for p in pools if p <= 1)
        # What one case going from rank 1 to unfound moves any metric by. Below
        # this a delta is a rank shuffle inside cases that already worked.
        out["resolution"] = round(1 / len(cases), 4)
        tiers = Counter()
        for c in cases:
            for grade in (c.get("grades") or {}).values():
                tiers[str(grade)] += 1
        if tiers:
            out["grade_census"] = dict(sorted(tiers.items()))
            out["cases_with_a_confound"] = sum(
                1 for c in cases if any(g == 0 for g in (c.get("grades") or {}).values()))
    return out


def query_shape(cases: list[dict]) -> dict[str, Any]:
    """The distribution the queries were drawn from, whole and per difficulty tier.

    The opening-n-gram concentration is the template detector. A low ``entropy``
    with a high ``top_share`` means one phrasing carries the file — the signature
    of an authoring prompt whose worked example got parroted, which no per-case
    reading catches because each query is individually plausible.

    ``carries_identifier`` validates the tier contract rather than trusting its
    label: the ``literal`` tier is defined as naming symbols from the source
    material and the other tiers as avoiding them, so the share of each tier
    carrying a distinctive identifier says whether the ladder is a ladder."""
    queries = [str(c.get("query") or "") for c in cases if c.get("query")]
    if not queries:
        return {}
    out: dict[str, Any] = {
        "words": spread(len(q.split()) for q in queries),
        "chars": spread(len(q) for q in queries),
        "distinct": len(set(queries)),
        "openings": concentration(Counter(
            " ".join(q.lower().split()[:OPENING_WORDS]) for q in queries)),
        "carries_identifier": round(
            sum(1 for q in queries if IDENTIFIER.search(q)) / len(queries), 3),
        # Real traffic uses these and authored queries essentially never do; a file
        # at zero cannot say anything about the scoped or operator-bearing paths.
        "uses_an_operator": sum(1 for q in queries
                                if re.search(r'\bOR\b|\||"', q)),
    }
    by_tier = {}
    for tier in sorted({str(c.get("difficulty")) for c in cases if c.get("difficulty")}):
        qs = [str(c["query"]) for c in cases
              if c.get("difficulty") == tier and c.get("query")]
        if not qs:
            continue
        by_tier[tier] = {
            "n": len(qs),
            "words": spread(len(q.split()) for q in qs),
            "openings": concentration(Counter(
                " ".join(q.lower().split()[:OPENING_WORDS]) for q in qs)),
            "carries_identifier": round(
                sum(1 for q in qs if IDENTIFIER.search(q)) / len(qs), 3),
        }
    if by_tier:
        out["by_tier"] = by_tier
    return out


def coverage(cases: list[dict]) -> dict[str, Any]:
    """How the file spreads over whatever its units were drawn from.

    Miners stratify on purpose (per-repo, per-directory caps) because corpus supply
    is power-law skewed. This is where that either shows or does not: a file whose
    top repo carries a third of the cases is measuring search on one codebase,
    whatever the sampler intended."""
    out: dict[str, Any] = {}
    for field_name in ("repo", "path", "target_thread"):
        values = [str(c[field_name]) for c in cases if c.get(field_name)]
        if values:
            out[field_name] = concentration(Counter(values))
    if any(c.get("path") for c in cases):
        dirs = Counter(str(c["path"]).rsplit("/", 1)[0]
                       for c in cases if c.get("path"))
        out["directory"] = concentration(dirs)
    snaps = {str(c.get("snapshot_id")) for c in cases if c.get("snapshot_id")}
    if snaps:
        out["snapshot_ids"] = sorted(snaps)
    protocols = Counter(str(c.get("protocol")) for c in cases if c.get("protocol"))
    if protocols:
        out["protocols"] = dict(protocols)
    # ``template_sha`` hashes the authoring prompt's *template*, so more than one
    # means the file was minted under changed instructions and is not one
    # population. ``prompt_sha`` hashes the rendered prompt, which embeds the
    # unit's own commit or diff — it is distinct per case by construction and says
    # nothing about population identity, so it is counted and never flagged.
    templates = {str(c["template_sha"]) for c in cases if c.get("template_sha")}
    if templates:
        out["template_shas"] = sorted(templates)
    prompts = {str(c.get("prompt_sha")) for c in cases if c.get("prompt_sha")}
    if prompts:
        out["distinct_prompt_shas"] = len(prompts)
    return out


def leakage(cases: list[dict], *, limit: int = 200) -> dict[str, Any]:
    """How much of each query's vocabulary appears in the thread it is gold for.

    The retrieval-free miners' claim is that the query was authored from an
    artifact outside the corpus, so a query should *not* read like its answer. High
    overlap means either the author saw the thread or the artifact quotes it, and
    either way the case is easier than the protocol says. Needs an open archive;
    returns a note instead of failing when there is none."""
    try:
        from sqlalchemy import text as sa_text

        from thread_archive._store import use_session
    except ImportError:                                    # pragma: no cover
        return {"skipped": "thread_archive not importable"}

    sampled = [c for c in cases if c.get("gold") and c.get("query")][:limit]
    if not sampled:
        return {}
    shares: list[float] = []
    try:
        with use_session() as s:
            for case in sampled:
                gold = str(case["gold"][0])
                rows = s.execute(sa_text(
                    "SELECT content FROM events_fts WHERE thread_id = :t LIMIT 400"),
                    {"t": gold}).all()
                if not rows:
                    continue
                body = tokens(" ".join(str(r[0] or "") for r in rows))
                q = tokens(str(case["query"]))
                if q:
                    shares.append(len(q & body) / len(q))
    except Exception as exc:                               # noqa: BLE001
        return {"skipped": f"no corpus open ({type(exc).__name__})"}
    if not shares:
        return {"skipped": "no gold thread resolved in the open corpus"}
    return {"query_tokens_present_in_gold": spread(shares),
            "sampled": len(shares)}


# ── the report ──────────────────────────────────────────────────────────────

def report(cases_path: Path, *, with_corpus: bool = False) -> dict[str, Any]:
    """Every panel for one case file, assembled. Panels that cannot run report a
    ``skipped`` note rather than raising, so a partial answer is still an answer."""
    cases = read_jsonl(cases_path)
    details = read_jsonl(detail_path_for(cases_path))
    runs = _ledger(cases_path.parent)
    out: dict[str, Any] = {
        "file": str(cases_path),
        "miners": sorted({str(c["miner"]) for c in cases if c.get("miner")}),
        "yield": yield_and_cost(cases, details, runs),
        "pool": pool_shape(cases),
        "queries": query_shape(cases),
        "coverage": coverage(cases),
    }
    if with_corpus:
        out["leakage"] = leakage(cases)
    return out


def _ledger(home: Path) -> list[dict]:
    """The mining ledger beside a case file, newest first. Read directly rather
    than through :mod:`search_lab.mine_runs` so this instrument keeps working on a
    corpus directory copied off the machine that mined it."""
    return list(reversed(read_jsonl(home / "mine-runs.jsonl")))


#: When an opening distribution reads as a template rather than a sample. Either
#: signal alone is enough: mass piled on one phrasing (``top_share``), or a
#: distribution flatter than a real one ever is (normalized ``entropy``). The
#: thresholds are deliberately loose — this flags a file for reading, it does not
#: score one, and a false positive costs a look where a false negative costs a
#: benchmark that measures its own prompt.
TEMPLATE_TOP_SHARE = 0.20
TEMPLATE_ENTROPY = 0.90


def _template_flag(openings: dict[str, Any]) -> str:
    if not openings or openings.get("total", 0) < 8:
        return ""     # too few to distinguish a template from a coincidence
    if (openings.get("top_share", 0) >= TEMPLATE_TOP_SHARE
            or openings.get("entropy", 1.0) <= TEMPLATE_ENTROPY):
        return "  ⚠ TEMPLATE"
    return ""


def _fmt_spread(s: dict[str, Any], unit: str = "") -> str:
    if not s:
        return "—"
    return (f"median {s['median']:g}{unit}  "
            f"[{s['min']:g}–{s['max']:g}]  p25/p75 {s['p25']:g}/{s['p75']:g}")


def text_report(data: dict[str, Any]) -> str:
    """The panel as a readable block. Every number carries what it is read against
    — a bare concentration figure means nothing without knowing that 1.0 is spread
    and 0 is a single value."""
    L: list[str] = [f"gold stats — {data['file']}"]
    if data.get("miners"):
        L.append(f"  miner: {', '.join(data['miners'])}")

    y = data.get("yield") or {}
    L += ["", "YIELD & COST"]
    L.append(f"  cases {y.get('cases', 0)}"
             + (f" from {y['units_drawn']} unit(s) drawn "
                f"({y['cases_per_unit']} case/unit, drop rate {y['drop_rate']})"
                if y.get("units_drawn") else ""))
    if y.get("cost_usd"):
        c = y["cost_usd"]
        L.append(f"  spent ${c['total']} — ${c['per_unit']}/unit, ${c['per_case']}/case")
    if y.get("outcomes"):
        L.append("  outcomes: " + ", ".join(f"{k} {v}" for k, v in y["outcomes"].items()))
    if y.get("funnel"):
        L.append(f"  funnel (newest run, {y.get('funnel_at', '?')}):")
        for row in y["funnel"]:
            why = ", ".join(f"{k} {v}" for k, v in (row.get("reasons") or {}).items()
                            if k != "ok")
            L.append(f"    {'$' if row.get('kind') == 'agent' else ' '} "
                     f"{row['stage']:<18} {row['in']:>5} → {row['out']:<5}"
                     + (f"  ({why})" if why else ""))
    else:
        L.append("  funnel: not recorded (miner declares no stages)")

    p = data.get("pool") or {}
    L += ["", "POOL"]
    L.append(f"  gold set:    {_fmt_spread(p.get('gold_set', {}))}")
    L.append(f"  graded pool: {_fmt_spread(p.get('graded_pool', {}))}")
    if p.get("single_gold") is not None:
        L.append(f"  single-gold cases: {p['single_gold']} ({p['single_gold_share']:.0%})"
                 " — recall@k and success@k coincide on these")
    if p.get("pool_of_one"):
        L.append(f"  ⚠ {p['pool_of_one']} case(s) grade a pool of one document — "
                 "nDCG over these is not measuring ordering")
    if p.get("grade_census"):
        L.append("  grades: " + ", ".join(f"{g}→{n}" for g, n in p["grade_census"].items()))
    if p.get("resolution"):
        L.append(f"  resolution: 1/n = {p['resolution']} — a smaller delta is a "
                 "rank shuffle, not a win")

    q = data.get("queries") or {}
    if q:
        L += ["", "QUERY SHAPE"]
        L.append(f"  length: {_fmt_spread(q.get('words', {}), ' words')}")
        op = q.get("openings") or {}
        if op:
            L.append(f"  openings: {op['distinct']} distinct in {op['total']}, "
                     f"entropy {op['entropy']} (1.0 = spread), "
                     f"top {op['top_share']:.0%} = {op['top']!r}{_template_flag(op)}")
        L.append(f"  carries a distinctive identifier: {q['carries_identifier']:.0%}"
                 f"   operators used: {q.get('uses_an_operator', 0)}")
        # Per tier, because a template lives in one rung: the whole-file number
        # averages a collapsed tier against two healthy ones and reads fine.
        for tier, t in (q.get("by_tier") or {}).items():
            top = t["openings"]
            L.append(f"    {tier:<12} n={t['n']:<4} "
                     f"median {t['words']['median']:g}w  "
                     f"ident {t['carries_identifier']:.0%}  "
                     f"openings {top.get('entropy', 0)} / top {top.get('top_share', 0):.0%}"
                     f" {top.get('top', '')!r}{_template_flag(top)}")

    cov = data.get("coverage") or {}
    if cov:
        L += ["", "COVERAGE"]
        for key in ("repo", "directory", "path", "target_thread"):
            c = cov.get(key)
            if c:
                L.append(f"  {key}: {c['distinct']} distinct, "
                         f"top {c['top_share']:.0%} ({c['top']})")
        if cov.get("protocols"):
            L.append("  protocol: " + ", ".join(f"{k} {v}" for k, v in cov["protocols"].items()))
        if len(cov.get("snapshot_ids") or []) > 1:
            L.append(f"  ⚠ {len(cov['snapshot_ids'])} snapshot ids in one file — "
                     "these cases are not all bound to the same corpus")
        if len(cov.get("template_shas") or []) > 1:
            L.append(f"  ⚠ {len(cov['template_shas'])} authoring templates in one "
                     "file — these cases are not one population")

    lk = data.get("leakage")
    if lk:
        L += ["", "LEAKAGE"]
        if lk.get("skipped"):
            L.append(f"  skipped: {lk['skipped']}")
        else:
            L.append(f"  query tokens present in the gold thread: "
                     f"{_fmt_spread(lk['query_tokens_present_in_gold'])} "
                     f"(n={lk['sampled']})")
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cases", type=Path, help="a mined case file (*cases*.jsonl)")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit the panel as JSON")
    ap.add_argument("--corpus", action="store_true",
                    help="also run the panels needing an open archive (leakage); "
                         "point THREAD_ARCHIVE_HOME at the snapshot the cases name")
    args = ap.parse_args(argv)

    path = args.cases.expanduser()
    if not path.exists():
        print(f"no case file at {path}", file=sys.stderr)
        return 2
    if args.corpus:
        from thread_archive import _api as api

        api.open_archive()
    data = report(path, with_corpus=args.corpus)
    print(json.dumps(data, indent=1) if args.as_json else text_report(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
