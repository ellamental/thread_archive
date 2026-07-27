"""Rerank-judged gold labels — a retrieved pool, graded by an independent judge.

The cheapest agent-mined rung. For each sampled real query, production search
returns a deep pool (``--pool``, default 50); one ``claude`` judge grades that
pool 2/1/0 against the query's intent and flags when nothing in it answers. Gold
is the grade-2 set. One search + one judge per query — no multi-turn corpus
sweep — so it scores an order of magnitude more queries per token than the query
miner.

What it measures and what it can't: the pool is drawn from the system under
test, so the golds inherit the retriever's recall ceiling — a relevant thread
search never surfaced can't enter the pool and can't become gold. So these cases
score **ordering within what search retrieves** (nDCG is the sharp signal here),
and are blind to recall misses by construction. The judge's "nothing in the pool
answers" verdict is the one recall signal available: it can't name the missing
thread, but it turns the blind spot into a measured ``none-of-pool`` rate instead
of silent inflation. Pair with the query miner (deep sweep, recall-capable) or
the query-gen miner (findability) to cover what this rung cannot.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from pathlib import Path

from thread_archive import _api as api
from thread_archive._retrieval.read import resolve_thread_ref
from thread_archive._store import use_session

from . import _framework as fw
from . import query_mined
from ._agent import run_claude
from ._framework import MineContext, Miner, MineResult, now_iso, validate_gold

CASES_STEM = "rerank-cases"

# The judge is a single-pass grader, not a corpus explorer: a smaller turn/time
# budget than the deep-sweep miners keeps the "cheap rung" cheap.
JUDGE_MAX_TURNS = 25
JUDGE_TIMEOUT_S = 600

_PROMPT = """You are judging search-result relevance for a conversation-archive \
benchmark.

A user searched their conversation archive with:

  query: {query}

Production search returned this ranked pool of candidate threads (id, title, and \
a snippet). Your job is to grade how well each ANSWERS the query's intent — not \
merely whether it shares vocabulary:

{pool}

You may read any candidate in full to verify before grading (read-only; the \
corpus is a fixed snapshot):

  {tool} read <thread_id> [--mode ends|chat|user|full|last] [--offset N] [--max-chars N]

Grade EVERY thread in the pool, and grade ONLY threads from the pool (do not \
introduce ids that aren't listed):
  2 = answers the query's intent
  1 = related / partial — touches it but doesn't answer
  0 = irrelevant, or a vocabulary-only look-alike

If NOTHING in the pool actually answers the intent (search retrieved only \
near-misses), say so with "none_answer": true and grade accordingly — that verdict \
is a signal, not a failure.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"grades": {{"<thread_id>": 2, "<thread_id>": 0, ...}}, \
"none_answer": false, "rationale": "<one short clause>"}}"""


# ── pure logic (tested) ──────────────────────────────────────────────────────

def parse_rerank(text: str, pool_ids: set[str]) -> dict | None:
    """The judge's verdict, or None if unparseable. ``grades`` keeps only
    well-formed thread->0|1|2 entries whose id is actually in ``pool_ids`` (the
    judge grades the retrieved pool, not the corpus — an id it didn't retrieve is
    dropped, not trusted). ``none`` is the explicit "nothing in the pool answers"
    flag, the recall-failure signal."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        v = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(v, dict) or not isinstance(v.get("grades"), dict):
        return None
    grades: dict[str, int] = {}
    for tid, g in v["grades"].items():
        if str(tid) not in pool_ids:
            continue
        try:
            g = int(g)
        except (TypeError, ValueError):
            continue
        if g in (0, 1, 2):
            grades[str(tid)] = g
    return {"grades": grades, "none": bool(v.get("none_answer")),
            "rationale": v.get("rationale")}


def build_prompt(query: str, pool: list[dict], tool_cmd: str) -> str:
    """The judge's brief: the query and the retrieved pool inline (id, title,
    snippet), so the common case needs no tool calls at all — reads are offered
    only for verification."""
    lines = "\n".join(
        f"  {p['thread_id']}  {(p.get('title') or '(untitled)')[:120]}\n"
        f"      {(p.get('snippet') or '').strip()[:280]}"
        for p in pool
    ) or "  (search returned nothing)"
    return _PROMPT.format(query=query, pool=lines, tool=tool_cmd)


def retrieved_pool(query: str, sessions: set[str], pool_size: int) -> list[dict]:
    """The production-search pool for a query over the snapshot, minus the
    originating sessions (which quote the query verbatim), capped to ``pool_size``.
    One row per thread — the ranked shape search already returns."""
    hits = api.search(query, limit=pool_size + len(sessions), rerank=None)
    pool: list[dict] = []
    for h in hits:
        tid = h["thread_id"]
        if tid in sessions:
            continue
        pool.append({"thread_id": tid,
                     "title": h.get("thread_title") or "",
                     "snippet": h.get("snippet") or h.get("full_content") or ""})
        if len(pool) >= pool_size:
            break
    return pool


# ── the agent seam ───────────────────────────────────────────────────────────

def judge_case(case: dict, model: str, tool_cmd: str, snapshot_id: str,
               pool_size: int, run=run_claude) -> tuple[dict | None, dict]:
    """Run one query end-to-end: retrieve the pool, judge it, validate gold.
    Returns (case_row | None, detail_row). The detail always records the outcome,
    including the ``none-of-pool`` recall-failure verdict. ``run`` is the agent
    seam."""
    sessions = set(case["sessions"])
    pool = retrieved_pool(case["query"], sessions, pool_size)
    detail = {"query": case["query"], "pool_size": len(pool), "sessions": case["sessions"]}
    if not pool:
        detail["outcome"] = "empty-pool"
        return None, detail
    pool_ids = {p["thread_id"] for p in pool}
    prompt = build_prompt(case["query"], pool, tool_cmd)
    text, stats = run(prompt, model, tool_cmd,
                      max_turns=JUDGE_MAX_TURNS, timeout=JUDGE_TIMEOUT_S)
    detail["agent"] = stats
    if text is None:
        detail["outcome"] = "agent-failed"
        return None, detail
    verdict = parse_rerank(text, pool_ids)
    if verdict is None:
        detail["outcome"] = "unparseable"
        return None, detail

    claimed_gold = [t for t, g in verdict["grades"].items() if g == 2]
    with use_session() as s:
        memo: dict[str, str | None] = {}

        def resolve(ref: str) -> str | None:
            if ref not in memo:
                memo[ref] = resolve_thread_ref(s, str(ref).strip())
            return memo[ref]

        gold = validate_gold(claimed_gold, sessions=sessions, resolve=resolve)
    outcome = "ok" if gold else ("none-of-pool" if verdict["none"] else "no-grade-2")
    detail.update({"outcome": outcome, "grades": verdict["grades"],
                   "none": verdict["none"], "rationale": verdict["rationale"]})
    if not gold:
        return None, detail
    row = {"query": case["query"], "gold": gold, "grades": verdict["grades"],
           "sessions": sorted(sessions), "snapshot_id": snapshot_id,
           "protocol": "rerank-judged", "mined_at": now_iso(),
           "judge_model": stats.get("model") or model,
           "prompt_sha": fw.prompt_sha(prompt), "miner_commit": fw.miner_commit()}
    return row, detail


# ── the miner ────────────────────────────────────────────────────────────────

class RerankJudgedMiner(Miner):
    name = "rerank"
    summary = "grade a retrieved pool with one judge (cheap; ordering, not recall)"
    measures = "precision (in-pool)"
    unit = "query"
    cost = "1 search + 1 judge / query (single-pass, cheap)"
    target_kind = "per-case"
    target_help = "queries to judge, one pool each"
    default_target = 5
    cases_stem = CASES_STEM

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--pool", type=int, default=50, metavar="N",
                            help="results per query the judge grades (default 50)")
        parser.add_argument(
            "--queries", type=Path, default=None, metavar="PATH",
            help="judge a hand-picked query list (JSONL rows with a \"query\" "
            "field, or bare query lines) instead of random trail sampling")
        parser.add_argument("--mined-after", metavar="ISO", default=None,
                            help="only sample queries from trail events after this date")

    def run(self, ctx: MineContext) -> MineResult:
        args = ctx.args
        cases_path, detail_path = fw.open_output(
            args.out, fw.default_cases_path(self.cases_stem))
        already = fw.mined_queries(cases_path)
        if args.queries:
            wanted = query_mined.read_query_list(args.queries.expanduser())
            cases = [c for c in query_mined.cases_for_queries(wanted, args.mined_after)
                     if c["query"] not in already][:ctx.target]
        else:
            cases = query_mined.sample_cases(ctx.target, args.seed,
                                             args.mined_after, already)
        if not cases:
            raise SystemExit("no unmined queries to judge")
        print(f"judging {len(cases)} queries (pool={args.pool}) with {ctx.model} "
              f"judges (jobs={ctx.jobs}) against snapshot {ctx.snapshot_id} -> {cases_path}")

        agent = ctx.agent_run or run_claude
        writer = fw.CaseWriter(self.name, cases_path, detail_path)
        ok = failed = 0
        outcomes: dict[str, int] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as ex:
            futures = {ex.submit(judge_case, c, ctx.model, ctx.tool_cmd,
                                 ctx.snapshot_id, args.pool, agent): c for c in cases}
            for fut in concurrent.futures.as_completed(futures):
                row, detail = fut.result()
                writer.write_detail(detail)
                outcomes[detail["outcome"]] = outcomes.get(detail["outcome"], 0) + 1
                if row is None:
                    failed += 1
                    print(f"  ✗ {detail['query'][:60]!r}: {detail['outcome']}")
                    continue
                writer.write_case(row)
                ok += 1
                print(f"  ✓ {row['query'][:60]!r}: {len(row['gold'])} gold "
                      f"of {detail['pool_size']} pooled")
        notes = []
        none_of_pool = outcomes.get("none-of-pool", 0)
        if none_of_pool:
            # The one recall signal an in-pool judge has — persisted as a rate on
            # the mining ledger, not just noted here (see _ops.mine_runs).
            notes.append(f"{none_of_pool}/{len(cases)} query(s) had no answer in the "
                         "pool (recall-failure signal — search retrieved only near-misses)")
        return MineResult(written=ok, failed=failed, cases_path=cases_path,
                          detail_path=detail_path, notes=notes,
                          attempted=len(cases), outcomes=outcomes)


MINER = RerankJudgedMiner()
