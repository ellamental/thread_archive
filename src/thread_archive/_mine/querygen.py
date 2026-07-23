"""Query-generated gold labels — findability cases from known threads.

The mirror image of the rerank judge. There the gold is fixed by construction
(the sampled thread) and the query is generated, so it measures **findability /
recall** — the blind spot a pool-bounded judge structurally can't see. A random
thread is sampled, one ``claude`` agent reads it and authors queries a real user
might type to find it, and each query becomes a case whose single gold is that
thread: does search rank the thread the query was written to find?

Two design choices earn their keep:

- **A difficulty ladder, not one query.** Each thread yields up to three tiers —
  ``verbatim`` (a distinctive phrase from the thread), ``paraphrase`` (same
  meaning, deliberately different words), and ``vague`` (a from-memory
  description). The verbatim tier is a near-1.0 smoke test; the vague tier is
  where semantic recall actually lives, and the gap between tiers is itself a
  diagnostic. Each case carries its ``difficulty`` so the tiers can be scored
  apart. The tiers also steer the known leakage risk (an agent that just read the
  thread echoes its vocabulary): the paraphrase/vague tiers are instructed to
  diverge lexically, which a title-derived query never can.
- **Random thread sampling.** Trail queries skew toward what agents already
  search for; sampling threads at random gives corpus-representative coverage,
  including the dusty regions nobody queries — where silent recall rot would hide.

Resume is by thread: a re-run skips threads already represented in the file.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import random

from sqlalchemy import text as sa_text

from .._store import use_session
from . import _framework as fw
from ._agent import run_claude
from ._framework import Miner, MineContext, MineResult, now_iso

CASES_STEM = "findability-cases"

DIFFICULTIES = ("verbatim", "paraphrase", "vague")

# A generation pass reads one thread and writes a handful of queries; it doesn't
# need the deep-sweep budget, but it may read the thread in parts, so keep a
# moderate loop.
GEN_MAX_TURNS = 20
GEN_TIMEOUT_S = 600

_PROMPT = """You are generating search queries for a conversation-archive \
findability benchmark.

Below is one archived conversation thread. Read it, then write queries a real \
user who remembers this conversation might type to find it again. Each query you \
write becomes a test: search must rank THIS thread for it.

  thread_id: {thread_id}
  title: {title}

Read the thread (read-only; the corpus is a fixed snapshot):

  {tool} read {thread_id} [--mode ends|chat|user|full|last] [--offset N] [--max-chars N]

Write up to one query per difficulty tier — omit a tier if you can't write a fair \
query for it:

  "verbatim"   — a distinctive phrase or identifier that actually appears in the \
thread (the easy case; a smoke test).
  "paraphrase" — the same information need in DIFFERENT words: avoid the thread's \
distinctive vocabulary, so this tests meaning-matching, not string-matching.
  "vague"      — how someone would describe this from memory weeks later, fuzzy \
and incomplete ("that conversation where we figured out why the watcher kept \
restarting").

Only write a query if THIS thread is a genuinely strong answer to it — if a query \
would be satisfied just as well by many other conversations, it isn't \
discriminative, so drop it. Quality over quantity; a thread that yields only one \
good query should return only one.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"queries": [{{"query": "<what a user types>", \
"difficulty": "verbatim|paraphrase|vague"}}, ...], \
"note": "<optional: why you skipped a tier, or left it thin>"}}

Return an empty "queries" list if the thread is too thin or generic to target \
fairly."""


# ── pure logic (tested) ──────────────────────────────────────────────────────

def parse_queries(text: str) -> dict | None:
    """The generator's queries, or None if unparseable. Each kept query needs a
    non-empty string ``query`` and a ``difficulty`` in :data:`DIFFICULTIES`; a
    malformed entry is dropped, not guessed. Duplicate query strings collapse
    (first wins). An empty list is valid (the agent judged the thread untargetable)
    and yields no cases."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        v = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(v, dict) or not isinstance(v.get("queries"), list):
        return None
    out: list[dict] = []
    seen: set[str] = set()
    for q in v["queries"]:
        if not isinstance(q, dict):
            continue
        query = q.get("query")
        difficulty = q.get("difficulty")
        if not isinstance(query, str) or not query.strip():
            continue
        if difficulty not in DIFFICULTIES:
            continue
        query = query.strip()
        if query in seen:
            continue
        seen.add(query)
        out.append({"query": query, "difficulty": difficulty})
    return {"queries": out, "note": v.get("note")}


def build_prompt(thread_id: str, title: str, tool_cmd: str) -> str:
    """The generator's brief: the thread to read and the difficulty ladder to
    author against."""
    return _PROMPT.format(thread_id=thread_id, title=title or "(untitled)",
                          tool=tool_cmd)


def cases_from_queries(thread_id: str, queries: list[dict],
                       snapshot_id: str) -> list[dict]:
    """One eval ``--cases`` row per generated query — gold is the single sampled
    thread, grades bind it at 2 (binary: findability is a single-target recall
    question), and ``difficulty`` travels so the tiers can be scored apart.
    ``sessions`` is empty: the query is authored, not drawn from a session."""
    stamp = now_iso()
    return [{"query": q["query"], "gold": [thread_id], "grades": {thread_id: 2},
             "sessions": [], "snapshot_id": snapshot_id, "protocol": "query-gen",
             "difficulty": q["difficulty"], "target_thread": thread_id,
             "mined_at": stamp}
            for q in queries]


def sample_threads(n: int, seed: int, skip: set[str]) -> list[dict]:
    """Random conversation threads with enough content to target — titled and with
    at least a few user turns, so the generator has real material to read. Excludes
    ``skip`` (threads already mined). ``[{thread_id, title}]``, shuffled by seed."""
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT t.id, t.title FROM threads t "
            "WHERE t.thread_type = 'conversation' AND NOT t.exclude_from_search "
            "AND t.title IS NOT NULL AND length(t.title) >= 12 "
            "AND (SELECT count(*) FROM events_fts f WHERE f.thread_id = t.id "
            "     AND f.content_type = 'user') >= 3"
        )).all()
    pool = [{"thread_id": tid, "title": title}
            for tid, title in rows if tid not in skip]
    random.Random(seed).shuffle(pool)
    return pool[:n]


# ── the agent seam ───────────────────────────────────────────────────────────

def generate_case(thread: dict, model: str, tool_cmd: str, snapshot_id: str,
                  run=run_claude) -> tuple[list[dict], dict]:
    """Generate the cases for one thread. Returns (rows, detail_row) — rows may be
    empty (agent failed, or judged the thread untargetable), which the caller
    counts as no cases written but not a hard failure. ``run`` is the agent seam."""
    prompt = build_prompt(thread["thread_id"], thread["title"], tool_cmd)
    text, stats = run(prompt, model, tool_cmd,
                      max_turns=GEN_MAX_TURNS, timeout=GEN_TIMEOUT_S)
    detail = {"thread_id": thread["thread_id"], "title": thread["title"],
              "agent": stats}
    if text is None:
        detail["outcome"] = "agent-failed"
        return [], detail
    parsed = parse_queries(text)
    if parsed is None:
        detail["outcome"] = "unparseable"
        return [], detail
    rows = cases_from_queries(thread["thread_id"], parsed["queries"], snapshot_id)
    detail.update({"outcome": "ok" if rows else "no-queries",
                   "queries": parsed["queries"], "note": parsed["note"]})
    return rows, detail


# ── the miner ────────────────────────────────────────────────────────────────

class QueryGenMiner(Miner):
    name = "querygen"
    summary = "generate queries for a random thread; test it ranks (findability)"
    measures = "recall (findability)"
    unit = "thread"
    cost = "1 agent / thread (reads it, writes queries)"
    target_kind = "per-case"
    target_help = "threads to sample, one agent each (each yields 1+ cases)"
    default_target = 5
    cases_stem = CASES_STEM

    def run(self, ctx: MineContext) -> MineResult:
        args = ctx.args
        cases_path, detail_path = fw.open_output(
            args.out, fw.default_cases_path(self.cases_stem))
        skip = fw.mined_gold_ids(cases_path)
        threads = sample_threads(ctx.target, args.seed, skip)
        if not threads:
            raise SystemExit("no unmined threads to generate from")
        print(f"generating queries for {len(threads)} threads with {ctx.model} "
              f"agents (jobs={ctx.jobs}) against snapshot {ctx.snapshot_id} -> {cases_path}")

        agent = ctx.agent_run or run_claude
        writer = fw.CaseWriter(self.name, cases_path, detail_path)
        written = empty = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as ex:
            futures = {ex.submit(generate_case, t, ctx.model, ctx.tool_cmd,
                                 ctx.snapshot_id, agent): t for t in threads}
            for fut in concurrent.futures.as_completed(futures):
                rows, detail = fut.result()
                writer.write_detail(detail)
                if not rows:
                    empty += 1
                    print(f"  ✗ {detail['thread_id']}: {detail['outcome']}")
                    continue
                for row in rows:
                    writer.write_case(row)
                written += len(rows)
                tiers = ", ".join(r["difficulty"] for r in rows)
                print(f"  ✓ {detail['thread_id']}: {len(rows)} queries ({tiers})")
        return MineResult(written=written, failed=empty, cases_path=cases_path,
                          detail_path=detail_path)


MINER = QueryGenMiner()
