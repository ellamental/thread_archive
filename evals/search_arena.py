"""search_arena — pairwise LLM-judged duels: challenger configs vs the shipped one.

The judged A/B tier of the quality ladder (docs/search-quality.md): where
``search_lab.py`` races configurations on the synthetic corpus
and ``retrieval_judge.py`` grades the shipped pipeline pointwise, this harness
answers the promotion question directly — *given real queries, does a judge
prefer the challenger's ranking to the incumbent's?* It samples real mined
queries, runs each through the baseline and through a challenger from
``evals/experiments/`` (contract in ``evals/experiments/README.md``), and shows the two
result lists — side order randomized per query, labels blind — to a headless
``claude`` that picks a winner or calls it a tie.

Identical rankings short-circuit to a tie without spending a judge call, so the
token cost scales with how much the configurations actually *disagree*; the
verdict rates are reported over the disagreements, plus a two-sided sign test
on wins vs losses so a 6–4 split doesn't masquerade as a result.

Costs real tokens (≤ one ``claude -p`` call per query per challenger;
``--sample`` bounds it) and requires the ``claude`` CLI. Read-only against the
archive. ``--out`` dumps per-query verdicts — judged output quotes real usage;
keep dumps out of the repo.

    .venv/bin/python evals/search_arena.py --experiment heavy_recency
    .venv/bin/python evals/search_arena.py --experiment no_phrase,flat_content_types \\
        --sample 30 --mined-after 2026-07-01

A challenger that wins here has real-usage evidence behind it — the strongest
offline signal the ladder has, and the bar to clear before changing the
defaults in ``_retrieval/params.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_module(path: Path, name: str):
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader, path
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def _lab():
    """The experiment bench (``evals/search_lab.py``) — discover() and the contract."""
    return _load_module(Path(__file__).resolve().parent / "search_lab.py", "search_lab")


# The claude CLI's model alias for the latest Opus — same choice, for the same
# reason, as retrieval_judge.py: capable unattended judgment, one cheap turn.
DEFAULT_MODEL = "opus"

# Hard wall-clock stop for one judgment call, seconds. A judge that hangs is
# skipped (scored as no verdict), never waited on.
CALL_TIMEOUT_S = 120

_PROMPT = """You are comparing two rankings from a search engine over a local \
conversation archive. The searcher (an AI agent mid-task) ran this query over \
its own past sessions:

Query:
{query}

Two engines ranked the results. Same archive, same query — only the ranking
differs. Each list shows its top threads in order (title, then the snippet
that matched):

List 1:
{list1}

List 2:
{list2}

Which list serves the query better overall? Weigh the top positions most —
the searcher reads from the top. Judge only from the evidence shown. If the
lists are equally good, say tie. Reply with ONLY a JSON object:
{{"winner": 1}} or {{"winner": 2}} or {{"winner": 0}} (0 = tie)."""


def _judge_call(prompt: str, model: str) -> dict | None:
    """One headless verdict call. None on any failure — a lost verdict is a
    smaller error than a made-up one."""
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json",
             "--model", model, "--max-turns", "1"],
            capture_output=True, text=True, timeout=CALL_TIMEOUT_S,
        )
        if proc.returncode != 0:
            return None
        result = json.loads(proc.stdout).get("result", "")
        start, end = result.find("{"), result.rfind("}")
        if start < 0 or end <= start:
            return None
        verdict = json.loads(result[start:end + 1])
        return verdict if isinstance(verdict, dict) else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def distinct_threads(hits: list[dict], k: int) -> list[tuple[str, str, str]]:
    """Collapse hits to the top-``k`` distinct threads — (id, title, snippet),
    the same evidence retrieval_judge shows: what a searching agent triages on."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for h in hits:
        tid = h["thread_id"]
        if tid in seen:
            continue
        seen.add(tid)
        out.append((
            tid,
            (h.get("thread_title") or "(untitled)")[:200],
            (h.get("snippet") or h.get("full_content") or "")[:400],
        ))
        if len(out) >= k:
            break
    return out


def _render(threads: list[tuple[str, str, str]]) -> str:
    if not threads:
        return "(no results)"
    return "\n".join(f"{i + 1}. title: {title}\n   snippet: {snippet}"
                     for i, (_, title, snippet) in enumerate(threads))


def duel(query: str, baseline: list[tuple[str, str, str]],
         challenger: list[tuple[str, str, str]], *,
         model: str, flip: bool, call=None) -> str | None:
    """One blind pairwise judgment. ``flip`` puts the challenger in the List 1
    slot (the caller randomizes it so neither side owns a position). Returns
    ``"baseline"`` / ``"challenger"`` / ``"tie"``, or None on judge failure.
    Identical rankings tie without a judge call."""
    if [t[0] for t in baseline] == [t[0] for t in challenger]:
        return "tie"
    first, second = (challenger, baseline) if flip else (baseline, challenger)
    verdict = (call or _judge_call)(
        _PROMPT.format(query=query, list1=_render(first), list2=_render(second)),
        model)
    if verdict is None:
        return None
    winner = verdict.get("winner")
    if winner == 0:
        return "tie"
    if winner in (1, 2):
        picked_first = winner == 1
        return "challenger" if picked_first == flip else "baseline"
    return None


def sign_test_p(wins: int, losses: int) -> float | None:
    """Two-sided exact sign test on the decided duels: P(a split at least this
    lopsided | the configs are truly equal). None when nothing was decided."""
    n = wins + losses
    if n == 0:
        return None
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def run_duels(cases: list[dict], challenger, baseline_search, *,
              k: int = 8, model: str = DEFAULT_MODEL, rerank=None,
              rng: random.Random | None = None, call=None) -> dict:
    """Duel one challenger against the baseline over ``cases`` (mined-case
    dicts: ``query``, optional ``sessions`` to exclude as self-hits). Returns
    the summary plus per-query detail rows."""
    rng = rng or random.Random(0)
    tallies = {"challenger": 0, "baseline": 0, "tie": 0}
    identical = failures = 0
    detail: list[dict] = []
    for case in cases:
        skip = set(case.get("sessions", []))

        def top(search) -> list[tuple[str, str, str]]:
            hits = [h for h in search(case["query"], limit=k + len(skip), rerank=rerank)
                    if h["thread_id"] not in skip]
            return distinct_threads(hits, k)

        base, chal = top(baseline_search), top(challenger.search)
        if [t[0] for t in base] == [t[0] for t in chal]:
            identical += 1
            tallies["tie"] += 1
            detail.append({"query": case["query"], "verdict": "tie", "identical": True})
            continue
        verdict = duel(case["query"], base, chal, model=model,
                       flip=rng.random() < 0.5, call=call)
        if verdict is None:
            failures += 1
            continue
        tallies[verdict] += 1
        detail.append({"query": case["query"], "verdict": verdict, "identical": False,
                       "baseline": [t[0] for t in base], "challenger": [t[0] for t in chal]})
    wins, losses = tallies["challenger"], tallies["baseline"]
    decided = wins + losses
    return {
        "challenger": challenger.name,
        "hypothesis": challenger.hypothesis,
        "n": len(cases),
        "identical": identical,
        "judge_failures": failures,
        "wins": wins,
        "losses": losses,
        "ties": tallies["tie"],
        "win_rate": wins / decided if decided else None,
        "sign_test_p": sign_test_p(wins, losses),
        "detail": detail,
    }


def _print_report(rep: dict) -> None:
    print(f"{rep['challenger']} vs baseline — {rep['n']} queries "
          f"({rep['identical']} identical rankings, {rep['judge_failures']} judge failures)")
    print(f"  challenger wins {rep['wins']}  losses {rep['losses']}  ties {rep['ties']}")
    if rep["win_rate"] is not None:
        print(f"  win rate over decided duels: {rep['win_rate']:.3f}"
              f"   sign test p = {rep['sign_test_p']:.3f}")
    else:
        print("  no decided duels — the configurations agree on every sampled query")
    print(f"  hypothesis: {rep['hypothesis']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--experiment", required=True,
                    help="challenger name(s) from evals/experiments/, comma-separated")
    ap.add_argument("--experiments", type=Path, default=Path(__file__).resolve().parent / "experiments",
                    help="experiments directory (default: evals/experiments/)")
    ap.add_argument("--sample", type=int, default=20,
                    help="queries per duel (each costs ≤ one claude call)")
    ap.add_argument("--k", type=int, default=8, help="threads shown per list")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--mined-after", metavar="ISO", default=None,
                    help="only mine queries from trail events after this date")
    ap.add_argument("--rerank", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--out", type=Path, default=None,
                    help="dump per-query verdicts as JSONL (real usage — "
                    "keep it out of the repo)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if shutil.which("claude") is None:
        raise SystemExit("search_arena needs the `claude` CLI on PATH")

    lab = _lab()
    experiments = {e.name: e for e in lab.discover(args.experiments)}
    wanted = [n.strip() for n in args.experiment.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in experiments]
    if unknown:
        raise SystemExit(f"unknown experiment(s): {', '.join(unknown)}")

    from thread_archive import _api as api

    retrieval_eval = _load_module(Path(__file__).resolve().parent / "retrieval_eval.py", "retrieval_eval")
    api.open_archive()
    cases = retrieval_eval.mine_log_cases(10**6, args.seed, args.mined_after)
    if not cases:
        raise SystemExit("no mined queries to duel over")
    random.Random(args.seed).shuffle(cases)
    cases = cases[:args.sample]

    rerank = None if args.rerank == "auto" else (args.rerank == "on")
    reports = []
    for name in wanted:
        rep = run_duels(cases, experiments[name], api.search, k=args.k,
                        model=args.model, rerank=rerank,
                        rng=random.Random(args.seed))
        reports.append(rep)
        if not args.json:
            _print_report(rep)
            print()

    if args.out:
        out = args.out.expanduser()
        out.write_text("".join(
            json.dumps({"challenger": r["challenger"], **d}) + "\n"
            for r in reports for d in r["detail"]))
    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if k != "detail"}
                          for r in reports], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
