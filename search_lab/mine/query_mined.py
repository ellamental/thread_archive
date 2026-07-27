"""Query-mined gold labels — corpus-grounded relevance cases from real queries.

The click labels (``retrieval_eval.py --from-log``) are incumbent-shaped: they
can only credit what production search already surfaced. This miner spends real
agent work to produce labels clicks can't: for each sampled real query it runs
one headless ``claude`` agent that reads the originating session for intent,
sweeps the frozen corpus snapshot with its own reformulated searches, reads the
strongest candidates, and decides which thread(s) the searcher actually wanted.
The output is a graded eval ``--cases`` file — so every eval run after the
one-time mining spend scores against corpus-grounded, intent-aware golds for free.

Measures both precision and recall: the agent sweeps deep and wide, so its gold
set can credit a relevant thread today's ranker buries past its top results —
the recall signal a pool-bounded judge (the rerank miner) structurally cannot see.

Sampling is from the trail's real ``thread_search`` queries, top-level
conversation sessions only (subagent retrieval fleets search to collect a whole
vein, which has no single rankable target). ``--queries`` mines a hand-picked
list instead, in file order — the vetted-seed path. Already-mined queries are
skipped, so a re-run against the same snapshot is an append cadence.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import random
from pathlib import Path

from sqlalchemy import text as sa_text

from thread_archive import _api as api
from thread_archive._retrieval.read import resolve_thread_ref
from thread_archive._store import use_session

from ..eval_core import classify_tool, mine_log_cases
from . import _framework as fw
from ._agent import run_claude
from ._framework import (  # re-exported for callers/tests that reach them here
    MineContext,
    Miner,
    MineResult,
    mined_queries,
    now_iso,
    validate_gold,
)

# Kept out of the class so the list view can read it and the tests can import it.
CASES_STEM = "judged-cases"

_PROMPT = """You are mining a gold relevance label for a conversation-archive \
search benchmark.

At {at}, an AI agent working in its own session searched its conversation \
archive with:

  query: {query}

Context from that session around the moment of the search — a ±3-turn window \
(not the whole session) showing what the agent was doing and, after the search, \
what it did with the results:

{context}

That window is only a slice of session thread {session}. If it doesn't make the \
searcher's intent clear, read the session yourself to see the lead-up — what \
they were working on before they searched — going as far back as you need:

  {tool} read {session} --mode chat [--offset N]

Read as much or as little of it as your judgment requires. (The session is \
never itself a gold candidate — it is where the query came from, and it is \
excluded from search results for the same reason.)

Historical hint: after this search the agent opened thread(s) {clicks}. That \
is one signal of intent — but it may be wrong (opened is not answered) or \
incomplete (the right thread may never have been surfaced).

Your job: determine which archived thread(s) the searcher most plausibly \
wanted — the grounded gold set — by exploring the archive yourself.

Tools (run via Bash; read-only; the corpus is a fixed snapshot — always pass \
the flags exactly as shown):

  {tool} search {skip} "<query>" [--limit N] [--rerank on|off|auto]
  {tool} read <thread_id> [--mode ends|chat|user|full|last] \
[--offset N] [--max-chars N]

Method:
1. Reformulate widely. Run many searches around the query — synonyms, code \
identifiers, structural variants, narrower and broader phrasings. Do not \
stop at the first plausible hit; the benchmark's value is finding what the \
original search may have missed. Search DEEP: `search` returns 50 by default — \
read down the whole band, not just the top few, so the gold isn't capped at \
today's ranker's top results.
2. Read the strongest candidates (--mode ends is a cheap first read) to \
verify they actually contain what the searcher wanted, not just matching \
vocabulary.
3. Decide the gold set: the thread id(s) that ANSWER the intent (grade 2). \
Also grade every other candidate you read: 1 = related/useful context, \
0 = irrelevant.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"gold": ["<thread_id>", ...], "grades": {{"<thread_id>": 2, ...}}, \
"confidence": "high|medium|low", "rationale": "<one short paragraph>"}}

"gold" may be empty if nothing in the corpus answers the intent."""


# ── pure logic (tested) ──────────────────────────────────────────────────────

def parse_verdict(text: str) -> dict | None:
    """The agent's final JSON verdict, or None. Tolerates prose around the
    object; rejects anything without a list-shaped ``gold``. Grades keep only
    well-formed thread->0|1|2 entries — a lost grade is a smaller error than a
    made-up one."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        v = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(v, dict) or not isinstance(v.get("gold"), list):
        return None
    grades = {}
    for tid, g in (v.get("grades") or {}).items() if isinstance(
            v.get("grades"), dict) else []:
        try:
            g = int(g)
        except (TypeError, ValueError):
            continue
        if g in (0, 1, 2):
            grades[str(tid)] = g
    return {"gold": [str(t) for t in v["gold"]], "grades": grades,
            "confidence": v.get("confidence"), "rationale": v.get("rationale")}


def build_prompt(case: dict, context: str, tool_cmd: str) -> str:
    """The mining agent's brief. ``case`` carries query/at/clicks/sessions and the
    originating ``session`` id; the session skips are baked into the search
    invocation shown so the agent never surfaces a session that quotes the query,
    and the session id is offered as a read handle so the agent can pull more of
    the pre-search lead-up when the window is thin. The corpus is the snapshot this
    run is pointed at — no per-case bound."""
    skip = ",".join(case["sessions"]) or "-"
    return _PROMPT.format(
        at=case["at"], query=case["query"],
        context=context.strip() or "(context unavailable)",
        clicks=", ".join(case["clicks"]) or "(none recorded)",
        tool=tool_cmd,
        session=case["session"],
        skip=f'--skip "{skip}"',
    )


def read_query_list(path) -> list[str]:
    """Query strings from a hand-picked list, in file order — one per line, either
    a JSON object with a ``query`` field (the seed-accepted format) or a bare query
    string. Blank lines skipped; duplicates dropped, first wins."""
    out: list[str] = []
    seen: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            q = row["query"] if isinstance(row, dict) else str(row)
        except (json.JSONDecodeError, KeyError, TypeError):
            q = line
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


# ── trail mining ─────────────────────────────────────────────────────────────

def query_sites(s, after: str | None = None) -> dict[str, dict]:
    """query -> its latest search site in the trail: the originating session, the
    trail event id (the context anchor), and when it happened (``at``). Scoped to
    ``thread_type='conversation'`` sessions, matching ``mine_log_cases`` — subagent
    retrieval fleets are excluded on both sides, so the join in
    :func:`sample_cases` can't reintroduce them."""
    sql = (
        "SELECT e.thread_id, e.id, e.occurred_at, e.payload FROM events e "
        "JOIN threads t ON t.id = e.thread_id "
        "WHERE e.event_type = 'tool_use_complete' "
        "AND e.payload LIKE '%thread_search%' "
        "AND t.thread_type = 'conversation' ")
    params: dict[str, str] = {}
    if after:
        sql += "AND e.occurred_at >= :after "
        params["after"] = after
    sql += "ORDER BY e.occurred_at, e.id"
    sites: dict[str, dict] = {}
    for sess, eid, at, payload in s.execute(sa_text(sql), params).all():
        p = payload if isinstance(payload, dict) else json.loads(payload)
        if classify_tool(p.get("tool_name")) != "search":
            continue
        q = (p.get("input") or {}).get("query")
        if isinstance(q, str) and q.strip():
            sites[q.strip()] = {"session": sess, "event_id": eid, "at": str(at)}
    return sites


def sample_cases(n: int, seed: int, after: str | None,
                 skip_queries: set[str]) -> list[dict]:
    """Mined click cases joined with their search sites, minus already-mined
    queries, sampled to ``n``."""
    cases = mine_log_cases(10**6, seed, after)
    with use_session() as s:
        sites = query_sites(s, after)
    joined = []
    for c in cases:
        site = sites.get(c["query"])
        if site is None or c["query"] in skip_queries:
            continue
        joined.append({"query": c["query"], "clicks": c["gold"],
                       "sessions": c["sessions"], **site})
    random.Random(seed).shuffle(joined)
    return joined[:n]


def cases_for_queries(queries: list[str], after: str | None = None) -> list[dict]:
    """Case dicts for an explicit, hand-picked query list — the vetted-seed path.
    Same shape :func:`sample_cases` produces (query / clicks / sessions + the trail
    search site), but in the given order with no random sampling. A query with no
    conversation-session search site is dropped (nothing to date-bound or anchor
    the agent on)."""
    catalog = {c["query"]: c for c in mine_log_cases(10**6, 0, after)}
    with use_session() as s:
        sites = query_sites(s, after)
    out: list[dict] = []
    for q in queries:
        site = sites.get(q)
        if site is None:
            continue
        c = catalog.get(q)
        out.append({"query": q,
                    "clicks": c["gold"] if c else [],
                    "sessions": c["sessions"] if c else [site["session"]],
                    **site})
    return out


# ── the agent seam ───────────────────────────────────────────────────────────

def _session_context(case: dict) -> str:
    """The originating session around the search call — the opening intent evidence
    the agent gets for free: a ±3-turn window in chat view. Best-effort: mining
    proceeds without it rather than dying on one odd thread."""
    try:
        return api.read_thread(case["session"], around_event=case["event_id"],
                               context_turns=3, mode="chat")
    except Exception:
        return ""


def _agent_call(prompt: str, model: str, tool_cmd: str,
                run=run_claude) -> tuple[dict | None, dict]:
    """One headless mining agent. Returns (verdict, stats) — the verdict parse over
    the agent's reply; a lost case is a smaller error than an unvalidated one.
    ``run`` is the agent seam (default :func:`run_claude`)."""
    text, stats = run(prompt, model, tool_cmd)
    if text is None:
        return None, stats
    return parse_verdict(text), stats


def mine_case(case: dict, model: str, tool_cmd: str, snapshot_id: str,
              run=run_claude) -> tuple[dict | None, dict]:
    """Run one case end-to-end: context, agent, verdict validation. Returns
    (case_row | None, detail_row). ``snapshot_id`` binds the case to the corpus it
    was mined against; ``run`` is the agent seam."""
    prompt = build_prompt(case, _session_context(case), tool_cmd)
    verdict, stats = _agent_call(prompt, model, tool_cmd, run)
    detail = {"query": case["query"], "at": case["at"],
              "session": case["session"], "clicks": case["clicks"],
              "agent": stats}
    if verdict is None:
        detail["outcome"] = "agent-failed"
        return None, detail
    with use_session() as s:
        memo: dict[str, str | None] = {}

        def resolve(ref: str) -> str | None:
            if ref not in memo:
                memo[ref] = resolve_thread_ref(s, str(ref).strip())
            return memo[ref]

        gold = validate_gold(verdict["gold"], sessions=set(case["sessions"]),
                             resolve=resolve)
    detail.update({"outcome": "ok" if gold else "no-valid-gold",
                   "gold_claimed": verdict["gold"], "gold": gold,
                   "grades": verdict["grades"],
                   "confidence": verdict["confidence"],
                   "rationale": verdict["rationale"]})
    if not gold:
        return None, detail
    # The graded candidate pool travels with the case, not just the grade-2 gold:
    # nDCG scores the whole ranked pool the agent built, so the paid-for judgment
    # isn't collapsed to one right answer.
    row = {"query": case["query"], "gold": gold, "grades": verdict["grades"],
           "sessions": sorted(case["sessions"]), "snapshot_id": snapshot_id,
           "protocol": "agent-mined", "click_gold": case["clicks"],
           "confidence": verdict["confidence"], "mined_at": now_iso()}
    return row, detail


# ── the miner ────────────────────────────────────────────────────────────────

class QueryMinedMiner(Miner):
    name = "query"
    summary = "corpus-grounded golds from real trail queries (deep sweep)"
    measures = "precision + recall (grounded)"
    unit = "query"
    cost = "1 opus agent / query (multi-turn, ~min each)"
    target_kind = "per-case"
    target_help = "queries to mine, one agent each"
    default_target = 5
    cases_stem = CASES_STEM

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--queries", type=Path, default=None, metavar="PATH",
            help="mine a hand-picked query list (JSONL rows with a \"query\" "
            "field, or bare query lines) in file order — the vetted-seed path, "
            "instead of random trail sampling")
        parser.add_argument("--mined-after", metavar="ISO", default=None,
                            help="only mine queries from trail events after this date")

    def run(self, ctx: MineContext) -> MineResult:
        args = ctx.args
        cases_path, detail_path = fw.open_output(
            args.out, fw.default_cases_path(self.cases_stem))
        already = mined_queries(cases_path)
        if args.queries:
            wanted = read_query_list(args.queries.expanduser())
            cases = [c for c in cases_for_queries(wanted, args.mined_after)
                     if c["query"] not in already][:ctx.target]
        else:
            cases = sample_cases(ctx.target, args.seed, args.mined_after, already)
        if not cases:
            raise SystemExit("no unmined queries to mine")
        print(f"mining {len(cases)} queries with {ctx.model} agents "
              f"(jobs={ctx.jobs}) against snapshot {ctx.snapshot_id} -> {cases_path}")

        agent = ctx.agent_run or run_claude
        writer = fw.CaseWriter(self.name, cases_path, detail_path)
        ok = failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as ex:
            futures = {ex.submit(mine_case, c, ctx.model, ctx.tool_cmd,
                                 ctx.snapshot_id, agent): c for c in cases}
            for fut in concurrent.futures.as_completed(futures):
                row, detail = fut.result()
                writer.write_detail(detail)
                if row is None:
                    failed += 1
                    print(f"  ✗ {detail['query'][:60]!r}: {detail['outcome']}")
                    continue
                writer.write_case(row)
                ok += 1
                print(f"  ✓ {row['query'][:60]!r}: {len(row['gold'])} gold "
                      f"(confidence: {row['confidence']})")
        return MineResult(written=ok, failed=failed, cases_path=cases_path,
                          detail_path=detail_path)


MINER = QueryMinedMiner()
