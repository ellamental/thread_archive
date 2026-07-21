"""LLM-judged relevance for real queries — the quality claim clicks can't make.

The click-labeled eval (``retrieval_eval.py --from-log``) can only credit
re-finding what a past search surfaced and an agent opened: relevant siblings
score as misses, and results better than the historical click get no credit.
This harness closes that gap with explicit judgments: sample real mined
queries, run the production search, and have a headless ``claude`` grade each
top-k thread for relevance to the query. That yields graded precision — a
statement about what search returns *now*, not about re-finding old clicks —
plus a calibration of the click labels themselves (how often the clicked
thread is actually judged relevant) and the beyond-click rate (relevant
results the click protocol would have scored as noise).

Grades, per (query, thread): 2 = answers the query, 1 = related/useful
context, 0 = irrelevant. The judge sees the thread's title and the matched
snippet — the same evidence a searching agent triages on, so the judgment
approximates the searcher's first-pass relevance call, not a full read.

Costs real tokens (one ``claude -p`` call per query; ``--sample`` bounds it)
and requires the ``claude`` CLI. Read-only against the archive. Reports to
stdout; ``--trend-out`` appends one JSONL summary row (the judged twin of the
eval's trend ledger); ``--out`` dumps per-thread judgments. Judged output
quotes real usage — keep dumps out of the repo.

    .venv/bin/python evals/retrieval_judge.py --sample 20
    .venv/bin/python evals/retrieval_judge.py --sample 20 --mined-after 2026-07-01 \
        --trend-out ~/.thread/archive/retrieval-judge.jsonl
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_eval", Path(__file__).resolve().parent / "retrieval_eval.py")
retrieval_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("retrieval_eval", retrieval_eval)
_SPEC.loader.exec_module(retrieval_eval)

from thread_archive import _api as api  # noqa: E402

# The claude CLI's model alias for the latest Opus — the curation drains'
# default for the same reason: capable unattended judgment without pinning a
# dated snapshot id. Judging is cheap per call (no tools, one turn), so the
# capable tier costs little.
DEFAULT_MODEL = "opus"

# Hard wall-clock stop for one judgment call, seconds. A judge that hangs is
# skipped (scored as no judgment), never waited on.
CALL_TIMEOUT_S = 120

_PROMPT = """You are judging search-result relevance for a local conversation \
archive. The user (an AI agent mid-task) searched their own past sessions.

Query:
{query}

Candidate threads (title, then the snippet that matched):
{candidates}

Grade each candidate's relevance TO THE QUERY:
- 2 = likely answers it (this thread is what the searcher wanted)
- 1 = related; useful context but probably not the target
- 0 = irrelevant

Judge only from the evidence shown. Reply with ONLY a JSON array, one object
per candidate, in the order given: [{{"id": <n>, "grade": 0|1|2}}, ...]"""


def _judge_call(prompt: str, model: str) -> list[dict] | None:
    """One headless grading call. None on any failure — a lost judgment is a
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
        start, end = result.find("["), result.rfind("]")
        if start < 0 or end <= start:
            return None
        grades = json.loads(result[start:end + 1])
        return grades if isinstance(grades, list) else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def judge_query(query: str, hits: list[dict], model: str,
                call=None) -> dict[str, int] | None:
    """Grade the distinct threads in ``hits`` — {thread_id: grade} or None.

    ``call`` is the judgment seam — ``(prompt, model) -> grades | None`` —
    defaulting to the headless ``claude`` subprocess; tests inject a fake.
    """
    call = call or _judge_call
    threads: list[tuple[str, str, str]] = []  # (tid, title, snippet)
    seen: set[str] = set()
    for h in hits:
        tid = h["thread_id"]
        if tid in seen:
            continue
        seen.add(tid)
        threads.append((
            tid,
            (h.get("thread_title") or "(untitled)")[:200],
            (h.get("snippet") or h.get("full_content") or "")[:400],
        ))
    if not threads:
        return {}
    candidates = "\n".join(
        f"{i}. title: {title}\n   snippet: {snippet}"
        for i, (_, title, snippet) in enumerate(threads))
    grades = call(_PROMPT.format(query=query, candidates=candidates), model)
    if grades is None:
        return None
    out: dict[str, int] = {}
    for g in grades:
        try:
            idx, grade = int(g["id"]), int(g["grade"])
        except (TypeError, KeyError, ValueError):
            continue
        if 0 <= idx < len(threads) and grade in (0, 1, 2):
            out[threads[idx][0]] = grade
    return out


def summarize(rows: list[dict]) -> dict:
    """Aggregate per-query judgment rows into the report."""
    n = len(rows)
    if not n:
        return {"n": 0}

    def frac(pred) -> float:
        return sum(1 for r in rows if pred(r)) / n

    def mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    return {
        "n": n,
        # Graded precision over the distinct top threads shown to the judge.
        "precision_rel_at5": mean([r["rel_at5"] for r in rows]),
        "precision_ans_at5": mean([r["ans_at5"] for r in rows]),
        # Did search surface at least one useful / answering thread?
        "any_relevant_at5": frac(lambda r: r["rel_at5"] > 0),
        "any_answer_at5": frac(lambda r: r["ans_at5"] > 0),
        "any_answer_at10": frac(lambda r: r["n_answers"] > 0),
        # Click-label calibration: of queries whose historical click ranked,
        # how often is that click judged relevant at all?
        "clicks_judged": sum(1 for r in rows if r["click_grade"] is not None),
        "click_judged_relevant": mean(
            [1.0 if r["click_grade"] >= 1 else 0.0
             for r in rows if r["click_grade"] is not None]),
        # Beyond-click credit: answering threads the click protocol would
        # score as misses.
        "beyond_click_answers": sum(r["beyond_click"] for r in rows),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", type=int, default=20,
                    help="queries to judge (each is one claude call)")
    ap.add_argument("--k", type=int, default=10, help="results judged per query")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--mined-after", metavar="ISO", default=None,
                    help="only mine queries from trail events after this date")
    ap.add_argument("--rerank", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--out", type=Path, default=None,
                    help="dump per-thread judgments as JSONL (real usage — "
                    "keep it out of the repo)")
    ap.add_argument("--trend-out", type=Path, default=None,
                    help="append the summary as one JSONL row (~ ok)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if shutil.which("claude") is None:
        raise SystemExit("retrieval_judge needs the `claude` CLI on PATH")

    api.open_archive()
    cases = retrieval_eval.mine_log_cases(10**6, args.seed, args.mined_after)
    if not cases:
        raise SystemExit("no mined queries to judge")
    random.Random(args.seed).shuffle(cases)
    cases = cases[:args.sample]

    rerank = None if args.rerank == "auto" else (args.rerank == "on")
    rows: list[dict] = []
    detail: list[dict] = []
    failures = 0
    for case in cases:
        skip = set(case.get("sessions", []))
        hits = [h for h in api.search(case["query"], limit=args.k + len(skip),
                                      rerank=rerank)
                if h["thread_id"] not in skip][:args.k]
        graded = judge_query(case["query"], hits, args.model)
        if graded is None:
            failures += 1
            continue
        order: list[str] = []
        for h in hits:
            if h["thread_id"] not in order:
                order.append(h["thread_id"])
        top5 = order[:5]
        gold = set(case["gold"])
        click_grades = [graded[t] for t in gold if t in graded]
        rows.append({
            "rel_at5": (sum(1 for t in top5 if graded.get(t, 0) >= 1) / len(top5))
            if top5 else 0.0,
            "ans_at5": (sum(1 for t in top5 if graded.get(t, 0) == 2) / len(top5))
            if top5 else 0.0,
            "n_answers": sum(1 for g in graded.values() if g == 2),
            "click_grade": max(click_grades) if click_grades else None,
            "beyond_click": sum(
                1 for t, g in graded.items() if g == 2 and t not in gold),
        })
        detail.append({"query": case["query"], "gold": sorted(gold),
                       "grades": graded})

    report = summarize(rows)
    report["judge_failures"] = failures
    report["model"] = args.model
    report["k"] = args.k
    report["rerank"] = args.rerank
    report["mined_after"] = args.mined_after

    if args.out:
        out = args.out.expanduser()
        out.write_text("".join(json.dumps(d) + "\n" for d in detail))
    if args.trend_out:
        from datetime import datetime, timezone

        path = args.trend_out.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "protocol": "llm-judge", **report}) + "\n")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"queries judged: {report['n']} (failures: {failures})")
        if report["n"]:
            print(f"top-5 graded precision: relevant {report['precision_rel_at5']:.3f}"
                  f"   answers {report['precision_ans_at5']:.3f}")
            print(f"queries with an answer in top-5: {report['any_answer_at5']:.3f}"
                  f"   in top-{args.k}: {report['any_answer_at10']:.3f}")
            print(f"click labels judged relevant: {report['click_judged_relevant']:.3f}"
                  f" (of {report['clicks_judged']} ranked clicks)")
            print(f"beyond-click answers: {report['beyond_click_answers']}")


if __name__ == "__main__":
    main()
