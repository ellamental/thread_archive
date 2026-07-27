"""Pooled gold labels — real queries, judged over a union of independent
retrievers.

The one protocol here whose queries are **observed rather than authored**. Every
other miner writes its queries from an artifact, which fixes circularity at the
cost of realism: authored queries run ~20 words of grammatical prose, where the
usage ledger's run ~4 words of keyword soup (``watcher ingest lock``). A weight
tuned on the first population is not obviously right for the second — density
normalizes matched terms against a fixed window, and the OR-fallback tier fires
when a strict pass comes up short, which is far likelier on a long query than a
three-token one. This miner takes the queries agents actually ran
(:func:`thread_archive._retrieval.usage.read_calls`) and labels them.

**Labeling a real query means retrieval helps build the pool, and this miner does
not pretend otherwise.** Nobody ever enumerated the answer set for
``watcher ingest lock``; the only record of it is what search returned and what
the agent opened, which is the censored click label. So the pool is assembled the
way TREC assembles one — from **several independent systems plus a random
sample** — and judged as a union:

- ``stack`` — the shipped fused pipeline.
- ``bm25`` — FTS5's ``bm25()`` alone (``search_lab.bm25_baseline``), its own query
  path and its own ordering, not a re-ranking of the stack's pool.
- ``deep`` — the stack at a far deeper ``pool_floor``. This is the load-bearing
  one: candidates that never enter the standard pool are unreachable by every
  other system here however they weigh what they got, so without it the union
  inherits one pool boundary.
- ``lexical`` — density and phrase alone, both arm-magnitude terms and fusion
  zeroed. A deliberately different ordering of a lexical pool.
- ``random`` — threads drawn from the corpus at random, so the judge sees
  documents no ranker nominated and the pool has an unbiased floor under it.

That does not make the gold retrieval-free, and :attr:`Miner.retrieval_free` is
``False`` here for that reason. What it does is move the bound: instead of "what
the incumbent missed is invisible", it is "what *no* pooled system retrieved is
invisible" — a bias that shrinks as systems are added, and one whose size can be
probed by asking how much each system contributed alone (recorded per case as
``pool_contrib``, and per run as the unique-contribution breakdown). Read a number
off this file as a measurement over real traffic with a known, shrinkable pool
bias; read the commit- and edit-linked files when the question needs gold that no
retrieval touched at all.

The judge's ``none-of-pool`` verdict is the recall alarm: a real query for which
five systems and a random draw turned up nothing worth grading is either an
unanswerable query or a hole under the whole pool, and the rate is recorded in the
mining ledger rather than left in a console line.

Resume is by query text *and by refusal*: a re-run skips queries already in the
case file, and queries the judge already refused, for as long as the judging prompt
is unchanged. That matters most for ``none-of-pool`` — a judge paid once to say the
pool holds no answer must not be paid to say it again — and the refusals file is
where that alarm accumulates into a rate rather than evaporating with the console
line that reported it.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from sqlalchemy import text as sa_text

from thread_archive._store import use_session

from . import _framework as fw
from ._agent import run_claude
from ._framework import MineContext, Miner, MineResult, now_iso

CASES_STEM = "pooled-cases"

#: Throwaway queries a bench or smoke test leaves in the ledger. Excluded by exact
#: text, matching the other ledger readers.
PROBE_QUERIES = ("x", "test", "warmup", "hello", "bogus")

#: Per-system depth, and the cap on the judged union. The pool has to be big
#: enough that "nothing here answers it" means something and small enough to fit
#: one judging pass; past ~40 the judge's attention is the bottleneck, not its
#: reading.
DEFAULT_DEPTH = 10
DEFAULT_POOL_CAP = 40
#: Random draws mixed in. Small on purpose — they are a floor under the pool, not
#: a sample of the corpus, and every one of them costs judge attention.
DEFAULT_RANDOM = 4

#: The deep system's pool depth. Well past the shipped ``pool_floor`` so it
#: reaches candidates the standard pool boundary cuts off.
DEEP_POOL_FLOOR = 1000

# A judging pass reads several candidates before grading, so it wants a real
# exploration loop — unlike the authoring miners, this agent is *supposed* to
# read the corpus.
JUDGE_MAX_TURNS = 40
JUDGE_TIMEOUT_S = 900

#: Above this share of the judged union graded 2, the verdict has stopped
#: discriminating: every ranker scores the same on it, so the case adds weight to
#: the denominator and no information. Generous — a genuinely easy query can have
#: several right answers — and it only catches the judge that said yes to nearly
#: everything.
MAX_GRADE2_SHARE = 0.8

_PROMPT = """You are grading search results for a conversation-archive relevance \
benchmark.

A real query was typed against an archive of AI-coding conversations. Below is a \
pool of candidate conversations, gathered by several different retrieval systems \
plus a random sample — so the pool is NOT one ranker's opinion, and its order here \
means nothing. Grade each candidate on how well it answers the query.

  query: {query}

Candidates:
{pool}

You can read any candidate in full:

  {tool_cmd} read <thread_id>

Read the ones whose grade is not obvious from the snippet. Do not grade on the \
snippet alone when the snippet is ambiguous.

Grades:
  2 — answers the query. Someone who typed this and opened this conversation \
found what they were looking for.
  1 — partially relevant. Touches the subject, discusses it in passing, or is \
adjacent work someone might reasonably want alongside the answer.
  0 — not relevant. Shares vocabulary, wrong subject.

Grade EVERY candidate listed. A pool where everything is 2 is as useless as one \
where everything is 0 — be discriminating, and be willing to say a well-ranked \
result is a 0.

If nothing in the pool answers the query at all, set "none_answer" to true and \
still grade the candidates (they will be 0s and 1s). That verdict is a signal \
about retrieval, not a failure on your part — say so honestly.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"grades": {{"<thread_id>": 2, "<thread_id>": 0, ...}}, \
"none_answer": false, "rationale": "<one line: what the query wanted and what \
answered it>"}}"""


# ── the query population ────────────────────────────────────────────────────

def ledger_queries(home=None, *, limit: int | None = None) -> list[str]:
    """Distinct query texts agents actually ran, newest first.

    Reads the live archive's usage ledger, which is deliberately *not* the frozen
    corpus the cases bind to: the snapshot is what gets searched, the ledger is
    only where the query population comes from. A query is kept once however many
    times it was paged through — the shapes are the sample, not the traffic
    volume."""
    from thread_archive._retrieval import usage

    seen: list[str] = []
    known: set[str] = set()
    for query, _kwargs in usage.read_calls(home, exclude=PROBE_QUERIES):
        text = (query or "").strip()
        if not text or text in known:
            continue
        known.add(text)
        seen.append(text)
        if limit and len(seen) >= limit:
            break
    return seen


def sample_queries(target: int, seed: int, skip: set[str], *, home=None) -> list[str]:
    """``target`` unmined queries, deterministic for a seed.

    Sampled rather than taken newest-first: the newest slice of a ledger is one
    session's preoccupations, and a case file built from it would measure search
    on whatever its author happened to be doing that week."""
    pool = [q for q in ledger_queries(home) if q not in skip]
    rng = random.Random(seed)
    rng.shuffle(pool)
    return pool[:target]


# ── the pool ────────────────────────────────────────────────────────────────

def _hits(search_fn, query: str, depth: int) -> list[str]:
    """Thread ids from one system, in its own order, deduped. A system that raises
    contributes nothing rather than failing the case: the pool's whole point is
    that it does not depend on any single retriever."""
    try:
        hits = search_fn(query, limit=depth)
    except Exception:
        return []
    out: list[str] = []
    for hit in hits or []:
        tid = str(hit.get("thread_id") or "")
        if tid and tid not in out:
            out.append(tid)
    return out


def system_pools(query: str, *, depth: int, n_random: int, seed: int) -> dict[str, list[str]]:
    """``{system: [thread_id, ...]}`` — what each independent retriever returned.

    Kept per system rather than pre-merged so the union's composition survives
    into the case: which systems found a given answer, and which answers only one
    system found, is the evidence for whether the pool is wide enough."""
    from thread_archive import _api as api
    from thread_archive._retrieval import SearchParams

    from ..bm25_baseline import bm25_search

    lexical_only = SearchParams(fusion_weight=0.0, semantic_weight=0.0,
                                bm25_score_weight=0.0, bm25_weight=0.0)
    deep = SearchParams(pool_floor=DEEP_POOL_FLOOR)
    return {
        "stack": _hits(api.search, query, depth),
        "bm25": _hits(bm25_search, query, depth),
        "deep": _hits(lambda q, limit: api.search(q, limit=limit, params=deep),
                      query, depth),
        "lexical": _hits(
            lambda q, limit: api.search(q, limit=limit, params=lexical_only),
            query, depth),
        "random": random_threads(n_random, seed=f"{seed}:{query}"),
    }


def random_threads(n: int, *, seed) -> list[str]:
    """``n`` conversation threads drawn at random — the unbiased floor under the
    pool. Without them every judged document was nominated by some ranker, and a
    pool that only holds nominees cannot show what the nominees are missing."""
    if n <= 0:
        return []
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT id FROM threads WHERE thread_type = 'conversation' "
            "AND NOT exclude_from_search")).all()
    ids = sorted(str(r[0]) for r in rows)
    if not ids:
        return []
    rng = random.Random(seed)
    rng.shuffle(ids)
    return ids[:n]


def merge_pool(pools: dict[str, list[str]], cap: int) -> tuple[list[str], dict[str, list[str]]]:
    """The judged union and, per thread, which systems nominated it.

    Round-robin across systems rather than concatenated, so the cap trims the
    *tail* of every system evenly instead of spending the whole budget on
    whichever one happens to be listed first."""
    contrib: dict[str, list[str]] = {}
    order: list[str] = []
    for rank in range(max((len(v) for v in pools.values()), default=0)):
        for system, hits in pools.items():
            if rank >= len(hits):
                continue
            tid = hits[rank]
            if tid not in contrib:
                contrib[tid] = []
                if len(order) < cap:
                    order.append(tid)
            if system not in contrib[tid]:
                contrib[tid].append(system)
    return order, {tid: contrib[tid] for tid in order}


def pool_rows(thread_ids: list[str]) -> list[dict]:
    """Title and a short snippet per candidate — what the judge sees before it
    decides whether to open one."""
    if not thread_ids:
        return []
    names = ", ".join(f":t{i}" for i in range(len(thread_ids)))
    params = {f"t{i}": tid for i, tid in enumerate(thread_ids)}
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT t.id, t.title, t.source, "
            "  (SELECT f.content FROM events_fts f WHERE f.thread_id = t.id "
            "   AND f.content_type = 'user' LIMIT 1) "
            f"FROM threads t WHERE t.id IN ({names})"), params).all()
    by_id = {str(tid): (title, source, snippet)
             for tid, title, source, snippet in rows}
    out = []
    for tid in thread_ids:
        title, source, snippet = by_id.get(tid, (None, None, None))
        out.append({"thread_id": tid, "title": title or "(untitled)",
                    "source": source or "?",
                    "snippet": (snippet or "")[:300].replace("\n", " ")})
    return out


def build_prompt(query: str, rows: list[dict], tool_cmd: str) -> str:
    """The judging prompt. Candidates are listed in the merged order, which is
    round-robin across systems and so carries no ranking signal — stated in the
    prompt so the judge does not read position as a hint."""
    pool = "\n".join(
        f"  [{r['thread_id']}] ({r['source']}) {r['title']}\n      {r['snippet']}"
        for r in rows) or "  (empty)"
    return _PROMPT.format(query=query, pool=pool, tool_cmd=tool_cmd)


def parse_grades(text: str, pool_ids: set[str]) -> dict | None:
    """The judge's verdict, or None if unparseable. Grades outside 0/1/2 and ids
    outside the pool are dropped — a judge that invents a thread id has graded
    something that was never offered, and keeping it would put a document in the
    qrels that no system could have returned."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("grades"), dict):
        return None
    grades: dict[str, int] = {}
    for tid, grade in raw["grades"].items():
        if str(tid) in pool_ids and isinstance(grade, int) and grade in (0, 1, 2):
            grades[str(tid)] = grade
    if not grades:
        return None
    rationale = raw.get("rationale")
    return {"grades": grades, "none": bool(raw.get("none_answer")),
            "rationale": rationale if isinstance(rationale, str) else None}


def case_from_verdict(query: str, verdict: dict, contrib: dict[str, list[str]],
                      snapshot_id: str) -> dict | None:
    """One case, or None when the judge found no grade-2 in the pool.

    A case with no right answer scores every ranker identically at zero and adds
    nothing but weight to the file's denominator, so the ``none-of-pool`` verdict
    is recorded as a run outcome instead — that is where it means something."""
    gold = sorted(tid for tid, g in verdict["grades"].items() if g == 2)
    if not gold:
        return None
    return {
        "query": query,
        "gold": gold,
        "grades": verdict["grades"],
        "sessions": [],
        "snapshot_id": snapshot_id,
        "protocol": "pooled-judged",
        "n_gold": len(gold),
        "pool_size": len(verdict["grades"]),
        "pool_contrib": {tid: contrib.get(tid, []) for tid in gold},
        "rationale": verdict["rationale"],
        "template_sha": fw.template_sha(_PROMPT),
        "mined_at": now_iso(),
    }


def unique_contributions(cases: list[dict]) -> dict[str, int]:
    """Grade-2 answers each system was the *only* one to nominate — the pool's
    marginal value, and the number that says whether a system earns its slot."""
    out: dict[str, int] = {}
    for case in cases:
        for _tid, systems in (case.get("pool_contrib") or {}).items():
            if len(systems) == 1:
                out[systems[0]] = out.get(systems[0], 0) + 1
    return out


# ── the stages ──────────────────────────────────────────────────────────────

def stage_pool(query: str, ctx: MineContext) -> fw.Verdict:
    """Free stage: assemble the multi-system union this query will be judged over.

    Free in tokens, not in time — it runs five retrievers — but it spends no agent,
    which is what decides where a ``--plan`` run stops. A query no system returns
    anything for is dropped here rather than sent to a judge with an empty page."""
    args = ctx.args
    pools = system_pools(query, depth=args.depth, n_random=args.n_random,
                         seed=args.seed)
    ids, contrib = merge_pool(pools, args.pool_cap)
    per_system = {k: len(v) for k, v in pools.items()}
    if not ids:
        return fw.Verdict(reason="empty-pool", detail={"per_system": per_system})
    return fw.Verdict(keep={"query": query, "ids": ids, "contrib": contrib},
                      reason="ok",
                      detail={"pool_size": len(ids), "per_system": per_system})


def stage_judge(unit: dict, ctx: MineContext) -> fw.Verdict:
    """Paid stage: one judge grades the whole union 2/1/0.

    ``none-of-pool`` is the recall alarm and stays its own disposition: a real
    query for which five systems and a random draw turned up nothing worth grading
    is either unanswerable or a hole under the entire pool, and collapsing it into
    a generic failure would lose the only signal it carries."""
    prompt = build_prompt(unit["query"], pool_rows(unit["ids"]), ctx.tool_cmd)
    run = ctx.agent_run or run_claude
    reply, stats = run(prompt, ctx.model, ctx.tool_cmd, max_turns=JUDGE_MAX_TURNS,
                       timeout=JUDGE_TIMEOUT_S, corpus_access=True)
    cost = float((stats or {}).get("cost_usd") or 0.0)
    detail = {"agent": stats, "prompt_sha": fw.prompt_sha(prompt),
              "template_sha": fw.template_sha(_PROMPT)}
    if reply is None:
        return fw.Verdict(reason="agent-failed", cost_usd=cost, detail=detail)
    verdict = parse_grades(reply, set(unit["ids"]))
    if verdict is None:
        return fw.Verdict(reason="unparseable", cost_usd=cost, detail=detail)
    detail.update({"none_answer": verdict["none"], "rationale": verdict["rationale"],
                   "graded": len(verdict["grades"])})
    case = case_from_verdict(unit["query"], verdict, unit["contrib"],
                             ctx.snapshot_id)
    if case is None:
        return fw.Verdict(reason="none-of-pool", cost_usd=cost, detail=detail)
    unit["case"] = case
    unit["verdict"] = verdict
    return fw.Verdict(keep=unit, reason="ok", cost_usd=cost, detail=detail)


def stage_verify(unit: dict, ctx: MineContext) -> fw.Verdict:
    """Free QA over the judgment: did it actually discriminate?

    The judging prompt says a pool where everything is 2 is as useless as one where
    everything is 0, and nothing checked that the judge listened. A verdict that
    grades nearly the whole union relevant scores every ranker identically — it
    cannot separate a good ordering from a bad one, so it adds a case to the
    denominator and no information to the file. Dropped as ``undiscriminating``,
    which is a judgment quality event and not a retrieval one, and so is never
    confused with ``none-of-pool``."""
    grades = unit["verdict"]["grades"]
    if not grades:
        return fw.Verdict(reason="undiscriminating", detail={"graded": 0})
    top = sum(1 for g in grades.values() if g == 2) / len(grades)
    detail = {"graded": len(grades), "grade2_share": round(top, 3)}
    if top >= MAX_GRADE2_SHARE:
        return fw.Verdict(reason="undiscriminating", detail=detail)
    return fw.Verdict(keep=unit, reason="ok", detail=detail)


# ── the miner ───────────────────────────────────────────────────────────────

class PooledMiner(Miner):
    name = "pooled"
    summary = ("judge a multi-system pool for a real ledger query (observed "
               "queries, pooled labels)")
    measures = "relevance over real traffic"
    unit = "query"
    cost = "1 agent / query (reads candidates, grades a pool)"
    target_kind = "per-case"
    target_help = "ledger queries to judge, one agent each"
    default_target = 5
    cases_stem = CASES_STEM
    gold_source = ("multi-system pooled judgment — the union of stack / bm25 / "
                   "deep-pool / lexical retrievers plus a random sample, graded "
                   "by one judge; bias is bounded at what no pooled system finds")
    retrieval_free = False
    # Its query population comes from a live archive's usage ledger, which a bare
    # corpus does not have, so it is a deliberate invocation.
    runnable_in_all = False

    def add_arguments(self, parser) -> None:
        parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH, metavar="N",
                            help=f"hits taken from each system (default {DEFAULT_DEPTH})")
        parser.add_argument("--pool-cap", type=int, default=DEFAULT_POOL_CAP,
                            metavar="N",
                            help="most candidates in one judged pool "
                                 f"(default {DEFAULT_POOL_CAP})")
        parser.add_argument("--random", type=int, default=DEFAULT_RANDOM, metavar="N",
                            dest="n_random",
                            help="random threads mixed into the pool "
                                 f"(default {DEFAULT_RANDOM})")
        parser.add_argument("--ledger-home", type=Path, default=None, metavar="PATH",
                            help="archive home to read the query population from "
                                 "(default: the snapshot being mined, which usually "
                                 "carries no traffic — point this at the live archive "
                                 "for a real query population). The corpus searched "
                                 "is always the snapshot, never this.")

    def stages(self, args) -> list[fw.Stage]:
        """Pool (free — five retrievers, no agent) → judge (paid) → verify (free).

        The pool stage being free is what makes ``--plan`` worth running here: it
        answers "does this corpus even return anything for the queries agents
        asked" without spending a judge on the answer."""
        return [
            fw.Stage(name="pool", fn=stage_pool, kind="free",
                     summary="the union of stack / bm25 / deep / lexical / random"),
            fw.Stage(name="judge", fn=stage_judge, kind="agent",
                     summary="one judge grades the whole union 2/1/0",
                     gate_sha=fw.template_sha(_PROMPT)),
            fw.Stage(name="verify", fn=stage_verify, kind="free",
                     summary="the judgment discriminates rather than grading "
                             "everything relevant"),
        ]

    def run(self, ctx: MineContext) -> MineResult:
        args = ctx.args
        cases_path, detail_path = fw.open_output(
            args.out, fw.default_cases_path(CASES_STEM))
        funnel = fw.Funnel()
        stages = self.stages(args)
        rejects_path = fw.rejects_path_for(cases_path)
        # `none-of-pool` is the shape this most matters for: a judge already paid to
        # say the pool holds no answer must not be paid to say it again.
        already = fw.mined_queries(cases_path) | fw.refused_units(
            rejects_path, fw.paid_gates(stages))
        home = args.ledger_home
        population = ledger_queries(home)
        unmined = [q for q in population if q not in already]
        queries = sample_queries(ctx.target, args.seed, already, home=home)
        funnel.note("ledger", n_in=len(population), n_out=len(unmined),
                    reasons={"already-mined-or-abstained":
                             len(population) - len(unmined)})
        funnel.note("sample", n_in=len(unmined), n_out=len(queries),
                    reasons={"not-drawn": len(unmined) - len(queries)})
        if not queries:
            raise SystemExit(
                "no unmined queries in the usage ledger. This miner's population "
                "is what agents actually searched for, so a ledger with no "
                "searches (a young archive, or --ledger-home pointing somewhere "
                "empty) leaves it nothing to mine.")

        print(f"mining {len(queries)} observed query/queries through "
              f"{len(stages)} stage(s) with {ctx.model} agents (jobs={ctx.jobs}) "
              f"against snapshot {ctx.snapshot_id} -> {cases_path}")

        details: dict[str, dict] = {
            q: {"query": q, "at": now_iso(), "stages": {}} for q in queries}

        writer = fw.CaseWriter(self.name, cases_path, detail_path, rejects_path)

        def observe(stage: fw.Stage, unit, verdict: fw.Verdict) -> None:
            query = unit if isinstance(unit, str) else unit["query"]
            detail = details[query]
            detail["stages"][stage.name] = {"reason": verdict.reason,
                                            **(verdict.detail or {})}
            detail["outcome"] = verdict.reason if not verdict.kept else "ok"
            if not verdict.kept and not ctx.plan:
                writer.write_reject({
                    "unit": query, "stage": stage.name, "reason": verdict.reason,
                    "gate_sha": stage.gate_sha, "kind": stage.kind,
                    "snapshot_id": ctx.snapshot_id, "detail": verdict.detail or {}})

        surviving = fw.run_pipeline(stages, queries, ctx, funnel=funnel,
                                    on_verdict=observe)
        if ctx.plan:
            return fw.plan_result(funnel, stages, surviving, cases_path)
        minted: list[dict] = []
        for unit in surviving:
            case = unit["case"]
            writer.write_case(case)
            minted.append(case)
            details[unit["query"]]["pool_contrib"] = case["pool_contrib"]
            print(f"  ✓ {unit['query'][:60]}: {case['n_gold']} gold "
                  f"of {case['pool_size']} judged")
        for detail in details.values():
            writer.write_detail(detail)

        outcomes: dict[str, int] = {}
        for detail in details.values():
            outcomes[detail["outcome"]] = outcomes.get(detail["outcome"], 0) + 1
        notes = [f"funnel:\n{funnel.text(indent='    ')}"]
        none_rate = outcomes.get("none-of-pool", 0)
        if none_rate:
            notes.append(f"none-of-pool on {none_rate}/{len(queries)} — pool holds "
                         "no answer for those queries")
        unique = unique_contributions(minted)
        if unique:
            notes.append("sole-finder gold by system: "
                         + ", ".join(f"{k} {v}" for k, v in sorted(unique.items())))
        if funnel.cost_usd:
            notes.append(f"spent ${funnel.cost_usd:.2f}")
        return MineResult(written=len(minted), failed=len(queries) - len(minted),
                          cases_path=cases_path, detail_path=detail_path,
                          attempted=len(queries), outcomes=outcomes, notes=notes,
                          funnel=funnel)


MINER = PooledMiner()
