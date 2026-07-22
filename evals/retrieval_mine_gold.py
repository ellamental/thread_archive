"""Agent-mined gold labels — corpus-grounded relevance cases, mined once, scored free.

The click labels (``retrieval_eval.py --from-log``) are incumbent-shaped:
they can only credit what production search already surfaced. This harness
spends real agent work to produce labels clicks can't: for each sampled real
query it runs one headless
``claude`` agent that reads the originating session for intent, sweeps the
corpus with its own reformulated searches, reads the strongest candidates,
and decides which thread(s) the searcher actually wanted. The output is a
case file in the eval's ``--cases`` format — so every eval run after the
one-time mining spend scores against corpus-grounded, intent-aware golds at
zero token cost.

Determinism by snapshot binding: mining runs against a frozen corpus snapshot
(``thread_archive snapshot``), not the live archive. The snapshot is a fixed
``THREAD_ARCHIVE_HOME`` whose content can't grow underneath the measurement, so
every agent search and every gold is drawn from the same corpus an eval will
later score against. Each case records the snapshot's ``snapshot_id`` (a content
fingerprint of the corpus); ``retrieval_eval.py --cases`` refuses to score cases
whose id does not match the snapshot it runs over, so a corpus that has changed
since mining can't be scored against stale golds — the cases invalidate rather
than silently drift. Mining is a cadence against one snapshot: new trail queries
accumulate; re-run (pointed at the same snapshot) to append fresh cases
(already-mined queries are skipped). When the corpus has moved on, take a new
snapshot and mine afresh — the old cases no longer match.

Two modes in one file:

- default: the orchestrator. Samples mined queries — from top-level
  conversation sessions only, since subagent retrieval fleets and collection
  sweeps search to collect a whole vein, which has no single rankable target —
  spawns one ``claude`` agent per query (Bash restricted to this script's tool
  mode), validates the verdict, appends cases (each carrying its gold plus the
  full graded candidate pool, so nDCG scores the whole ranking) + a detail sidecar.
- ``tool search`` / ``tool read``: the agent's corpus access — production
  search and thread reads over the snapshot the mining run is pointed at.

Costs real tokens (one multi-turn opus agent per query — minutes each;
``--sample`` bounds it) and requires the ``claude`` CLI. Read-only against
the archive. Case files quote real usage — keep them out of the repo; they
live beside the trend ledgers in ``~/.thread/archive/``.

    thread_archive snapshot ~/.thread/archive-snap
    export THREAD_ARCHIVE_HOME=~/.thread/archive-snap
    .venv/bin/python evals/retrieval_mine_gold.py --sample 5
    .venv/bin/python evals/retrieval_mine_gold.py --queries seed.jsonl --sample 5
    .venv/bin/python evals/retrieval_eval.py --cases ~/.thread/archive/judged-cases.jsonl
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import random
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_eval", Path(__file__).resolve().parent / "retrieval_eval.py")
retrieval_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("retrieval_eval", retrieval_eval)
_SPEC.loader.exec_module(retrieval_eval)

from sqlalchemy import text as sa_text  # noqa: E402

from thread_archive import _api as api  # noqa: E402
from thread_archive._retrieval.read import resolve_thread_ref  # noqa: E402
from thread_archive._store import use_session  # noqa: E402

# The claude CLI's model alias for the latest Opus — mining is judgment work,
# the capable tier is the point.
DEFAULT_MODEL = "opus"

# One agent run explores with dozens of tool calls; cap the loop and the wall
# clock so a wedged agent costs a bounded amount and is scored as a failure.
AGENT_MAX_TURNS = 60
AGENT_TIMEOUT_S = 1500

# Default output beside the trend ledgers; the detail sidecar derives from it.
DEFAULT_CASES = "~/.thread/archive/judged-cases.jsonl"

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


def validate_gold(gold: list[str], *, sessions: set[str], resolve) -> list[str]:
    """Golds that hold up: resolvable to a canonical thread id and not the
    originating session (which quotes the query verbatim). ``resolve(ref) ->
    canonical tid | None``. Existence needs no check — the agent searched the
    snapshot, so any gold it returns is already in the frozen corpus. Order
    preserved, dupes dropped."""
    out: list[str] = []
    for ref in gold:
        tid = resolve(ref)
        if tid is None or tid in sessions or tid in out:
            continue
        out.append(tid)
    return out


def build_prompt(case: dict, context: str, tool_cmd: str) -> str:
    """The mining agent's brief. ``case`` carries query/at/clicks/sessions and
    the originating ``session`` id; the session skips are baked into the search
    invocation shown so the agent never surfaces a session that quotes the query,
    and the session id is offered as a read handle so the agent can pull more of
    the pre-search lead-up when the window is thin. The corpus is the snapshot
    this run is pointed at — no per-case bound."""
    skip = ",".join(case["sessions"]) or "-"
    return _PROMPT.format(
        at=case["at"], query=case["query"],
        context=context.strip() or "(context unavailable)",
        clicks=", ".join(case["clicks"]) or "(none recorded)",
        tool=tool_cmd,
        session=case["session"],
        skip=f'--skip "{skip}"',
    )


def mined_queries(path: Path) -> set[str]:
    """Queries already in the case file — re-runs append only new ones."""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.add(json.loads(line)["query"])
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def read_query_list(path: Path) -> list[str]:
    """Query strings from a hand-picked list, in file order — one per line,
    either a JSON object with a ``query`` field (the seed-accepted format) or a
    bare query string. Blank lines skipped; duplicates dropped, first wins."""
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
    """query -> its latest search site in the trail: the originating session,
    the trail event id (the context anchor), and when it happened (``at`` — the
    intent-context timestamp the prompt shows the agent). ``mine_log_cases``
    merges occurrences per query; this recovers the where/when that merge drops.
    Scoped to ``thread_type='conversation'`` sessions, matching
    ``mine_log_cases`` — subagent retrieval fleets are excluded on both sides, so
    the join in ``sample_cases`` can't reintroduce them."""
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
        if retrieval_eval.classify_tool(p.get("tool_name")) != "search":
            continue
        q = (p.get("input") or {}).get("query")
        if isinstance(q, str) and q.strip():
            # Later rows overwrite: the latest occurrence of the query wins.
            sites[q.strip()] = {"session": sess, "event_id": eid, "at": str(at)}
    return sites


def sample_cases(n: int, seed: int, after: str | None,
                 skip_queries: set[str]) -> list[dict]:
    """Mined click cases joined with their search sites, minus already-mined
    queries, sampled to ``n``."""
    cases = retrieval_eval.mine_log_cases(10**6, seed, after)
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
    """Case dicts for an explicit, hand-picked query list — the vetted-seed
    path. Same shape :func:`sample_cases` produces (query / clicks / sessions +
    the trail search site), but in the given order with no random sampling, so
    a reviewed seed file mines in the sequence it was curated. A query with no
    conversation-session search site is dropped (nothing to date-bound or anchor
    the agent on); its click gold and full skip-session set come from
    ``mine_log_cases`` when present, else fall back to the site's own session."""
    catalog = {c["query"]: c
               for c in retrieval_eval.mine_log_cases(10**6, 0, after)}
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
    """The originating session around the search call — the opening intent
    evidence the agent gets for free: a ±3-turn window in chat view, bounded
    only by ``read_thread``'s own size budget (no fixed cap). It is deliberately
    just the neighborhood of the search; the prompt hands the agent the session
    id so it can read the rest of the lead-up itself when this window is too
    thin. Best-effort: mining proceeds without it rather than dying on one odd
    thread."""
    try:
        return api.read_thread(case["session"], around_event=case["event_id"],
                               context_turns=3, mode="chat")
    except Exception:
        return ""


def run_claude(prompt: str, model: str, tool_cmd: str) -> tuple[str | None, dict]:
    """One headless agent turn-loop. Returns (final message text | None, stats).
    Bash is allowed only for ``tool_cmd`` (this harness's snapshot-bound corpus
    access); any failure is (None, stats). The reusable seam both this miner and
    the topic miner drive — neither interprets the reply here, so the caller
    parses whatever shape it asked the agent for."""
    stats: dict = {}
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json",
             "--model", model, "--max-turns", str(AGENT_MAX_TURNS),
             "--allowedTools", f"Bash({tool_cmd}:*)"],
            capture_output=True, text=True, timeout=AGENT_TIMEOUT_S,
            cwd=str(_REPO),
        )
        if proc.returncode != 0:
            stats["error"] = (proc.stderr or "").strip()[-500:]
            return None, stats
        out = json.loads(proc.stdout)
        stats = {"num_turns": out.get("num_turns"),
                 "cost_usd": out.get("total_cost_usd"),
                 "duration_ms": out.get("duration_ms")}
        return (out.get("result") or ""), stats
    except subprocess.TimeoutExpired:
        stats["error"] = "timeout"
        return None, stats
    except (json.JSONDecodeError, OSError) as e:
        stats["error"] = str(e)[:200]
        return None, stats


def _agent_call(prompt: str, model: str, tool_cmd: str) -> tuple[dict | None, dict]:
    """One headless mining agent. Returns (verdict, stats) — the query-mining
    verdict parse over :func:`run_claude`'s reply; a lost case is a smaller error
    than an unvalidated one."""
    text, stats = run_claude(prompt, model, tool_cmd)
    if text is None:
        return None, stats
    return parse_verdict(text), stats


def mine_case(case: dict, model: str, tool_cmd: str,
              snapshot_id: str) -> tuple[dict | None, dict]:
    """Run one case end-to-end: context, agent, verdict validation. Returns
    (case_row | None, detail_row). ``snapshot_id`` binds the case to the corpus
    it was mined against."""
    prompt = build_prompt(case, _session_context(case), tool_cmd)
    verdict, stats = _agent_call(prompt, model, tool_cmd)
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
    # The graded candidate pool travels with the case, not just the grade-2
    # gold: nDCG (retrieval_eval.py --cases) scores the whole ranked pool the
    # agent built, so the paid-for judgment isn't collapsed to one right answer.
    row = {"query": case["query"], "gold": gold, "grades": verdict["grades"],
           "sessions": sorted(case["sessions"]), "snapshot_id": snapshot_id,
           "protocol": "agent-mined", "click_gold": case["clicks"],
           "confidence": verdict["confidence"],
           "mined_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return row, detail


# ── tool mode: the agent's corpus access (over the snapshot) ─────────────────

def _tool_search(args) -> None:
    api.open_archive()
    skip = {s for s in (args.skip or "").split(",") if s and s != "-"}
    rerank = None if args.rerank == "auto" else (args.rerank == "on")
    hits = api.search(args.query, limit=args.limit + len(skip), rerank=rerank)
    shown = 0
    for h in hits:
        if h["thread_id"] in skip:
            continue
        shown += 1
        if shown > args.limit:
            break
        print(json.dumps({
            "thread_id": h["thread_id"],
            "title": (h.get("thread_title") or "(untitled)")[:200],
            "snippet": (h.get("snippet") or h.get("full_content") or "")[:400],
        }))
    if not shown:
        print("(no results)")


def _tool_read(args) -> None:
    api.open_archive()
    with use_session() as s:
        tid = resolve_thread_ref(s, str(args.thread_id).strip())
        if tid is None:
            raise SystemExit(f"unknown thread: {args.thread_id}")
    print(api.read_thread(tid, mode=args.mode, offset=args.offset,
                          max_chars=args.max_chars))


# ── entry ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")

    tool = sub.add_parser("tool", help="agent corpus access (date-bounded)")
    tsub = tool.add_subparsers(dest="tool_cmd", required=True)
    ts = tsub.add_parser("search")
    ts.add_argument("query")
    ts.add_argument("--skip", default="")
    # Deep by default: mining is recall-bound, so a shallow limit bakes the
    # incumbent ranker's blind spots into the gold — a relevant thread ranked
    # past the cap never enters the pool, and the eval can then never credit a
    # change that surfaces it. Snippets are cheap; reads stay agent-controlled.
    ts.add_argument("--limit", type=int, default=50)
    ts.add_argument("--rerank", choices=["auto", "on", "off"], default="auto")
    tr = tsub.add_parser("read")
    tr.add_argument("thread_id")
    tr.add_argument("--mode", default="ends",
                    choices=["ends", "chat", "user", "full", "last"])
    tr.add_argument("--offset", type=int, default=0)
    tr.add_argument("--max-chars", type=int, default=12000)

    ap.add_argument("--sample", type=int, default=5,
                    help="queries to mine (each is one multi-turn agent run)")
    ap.add_argument("--queries", type=Path, default=None, metavar="PATH",
                    help="mine a hand-picked query list (JSONL rows with a "
                    "\"query\" field, or bare query lines) in file order — the "
                    "vetted-seed path, instead of random trail sampling")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--mined-after", metavar="ISO", default=None,
                    help="only mine queries from trail events after this date")
    ap.add_argument("--jobs", type=int, default=2,
                    help="concurrent mining agents")
    ap.add_argument("--cases-out", type=Path, default=Path(DEFAULT_CASES),
                    help="case file to append to (real usage — keep out of "
                    "the repo); detail sidecar lands beside it")
    args = ap.parse_args()

    if args.cmd == "tool":
        {"search": _tool_search, "read": _tool_read}[args.tool_cmd](args)
        return

    if shutil.which("claude") is None:
        raise SystemExit("retrieval_mine_gold needs the `claude` CLI on PATH")

    api.open_archive()
    from thread_archive._ops.snapshot import read_snapshot_id

    snapshot_id = read_snapshot_id()
    if snapshot_id is None:
        raise SystemExit(
            "mining must run against a corpus snapshot, not the live archive: "
            "`thread_archive snapshot <dir>`, then point THREAD_ARCHIVE_HOME at "
            "it. Cases carry the snapshot's id so the eval can reject them once "
            "the corpus has moved on."
        )
    cases_path = args.cases_out.expanduser()
    detail_path = cases_path.with_name(cases_path.stem + "-detail.jsonl")
    already = mined_queries(cases_path)
    if args.queries:
        wanted = read_query_list(args.queries.expanduser())
        cases = [c for c in cases_for_queries(wanted, args.mined_after)
                 if c["query"] not in already][:args.sample]
    else:
        cases = sample_cases(args.sample, args.seed, args.mined_after, already)
    if not cases:
        raise SystemExit("no unmined queries to mine")
    print(f"mining {len(cases)} queries with {args.model} agents "
          f"(jobs={args.jobs}) against snapshot {snapshot_id} -> {cases_path}")

    tool_cmd = f"{sys.executable} {Path(__file__).resolve()} tool"
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futures = {ex.submit(mine_case, c, args.model, tool_cmd, snapshot_id): c
                   for c in cases}
        for fut in concurrent.futures.as_completed(futures):
            row, detail = fut.result()
            with detail_path.open("a") as f:
                f.write(json.dumps(detail) + "\n")
            if row is None:
                failed += 1
                print(f"  ✗ {detail['query'][:60]!r}: {detail['outcome']}")
                continue
            with cases_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            ok += 1
            print(f"  ✓ {row['query'][:60]!r}: {len(row['gold'])} gold "
                  f"(confidence: {row['confidence']})")
    print(f"done: {ok} cases written, {failed} failed "
          f"(detail: {detail_path})")


if __name__ == "__main__":
    main()
