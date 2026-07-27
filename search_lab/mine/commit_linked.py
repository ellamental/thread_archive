"""Commit-linked gold labels — findability cases whose gold is fixed by
provenance rather than by retrieval.

Every other miner establishes its labels by *searching the corpus with the engine
under test*: the rerank judge grades a pool production search returned, and the
query/topic labelers sweep with their own reformulations through the same stack.
That circularity bounds what they can ever measure — a systematic retrieval blind
spot is invisible to the labeler and the ranker alike, so it can never score as a
miss. Query-gen escapes it by fixing the gold first (a sampled thread), and this
miner escapes it the same way but with a stronger gold: the session is not
*sampled*, it is the session that **provably produced a commit**.

The input is a **linkage file** — one row per session that is externally linked to
the commits it authored (see :data:`LINKAGE_NAME`). A corpus that ships session ↔
commit provenance can produce one; ``search_lab/swechat_corpus.py`` writes it for the
SWE-chat dataset. Nothing here is specific to that dataset: the miner reads the
linkage shape, not a corpus.

Two properties earn this miner its place beside query-gen:

- **No vocabulary leakage.** The query is authored from the *commit* — message and
  diff — and the agent never reads the target thread. Query-gen's known bias is an
  agent that just read the thread echoing its wording; here the query is written
  from an artifact outside the corpus, which is also how a real user searches
  ("where did we change the retry backoff") — from the code they remember, not
  from the conversation they're trying to find.
- **Structural confounds, no tokens.** Sibling sessions in the same repository are
  the hard negatives — topically identical, wrong answer — and repo membership
  plus touched files grades them for free: the intended session is 2, a sibling
  touching overlapping files is 1 (plausibly partial), a sibling touching disjoint
  files is 0. That is the confound-ranking signal the topic miner spends agent
  sessions to synthesize.

**Read grades 1 and 0 as structural proxies, not verified labels.** Only the 2 is
a strong claim (provenance says so). A disjoint-file sibling *could* still answer a
vague query; file overlap is a heuristic for "related work", not a judgment. The
gold-2 recall numbers are the trustworthy signal, and nDCG over the proxy pool is
directional.

Resume is by gold thread: a re-run skips sessions already represented in the file.
"""

from __future__ import annotations

import concurrent.futures
import json
import random
from pathlib import Path

from sqlalchemy import text as sa_text

from thread_archive._store import use_session

from . import _framework as fw
from ._agent import run_claude
from ._framework import MineContext, Miner, MineResult, now_iso

CASES_STEM = "commit-cases"

#: The linkage file's default name under the gold dir. Written by a corpus
#: builder, consumed here; ``--linkage`` overrides the path.
LINKAGE_NAME = "commit-linkage.jsonl"

DIFFICULTIES = ("literal", "functional", "intent")

# How much of a diff the authoring agent sees. Enough to characterize the change
# without turning one prompt into a whole file's worth of context.
MAX_PATCH_CHARS = 6000

# Confound pool caps. The graded pool wants enough hard negatives to make ordering
# measurable without every case carrying a repo's entire session list; a repo with
# 870 sessions would otherwise dominate the file's scoring cost.
MAX_PARTIAL = 6
MAX_CONFOUND = 12

# Default stratification cap. Repo sizes are wildly skewed in a real multi-repo
# corpus (one repo can be 15% of the sessions), so an unstratified sample measures
# how well search works on that one codebase.
DEFAULT_PER_REPO = 3

GEN_MAX_TURNS = 6
GEN_TIMEOUT_S = 300

_PROMPT = """You are writing search queries for a conversation-archive \
findability benchmark.

Below is a git commit. An AI coding session produced this change, and that \
session's transcript is archived. Your job: write the queries a developer who \
remembers *this change* would type to find *that conversation* again.

  repository: {repo}
  files touched: {files}

  commit message:
{message}

  diff (truncated):
{patch}

You do NOT have the conversation. Do not try to find or read it — write your \
queries from the change itself. That is the point: these queries must be phrased \
the way someone recalls the *work*, not the way the transcript happens to be \
worded.

Write up to one query per tier — omit a tier you can't write fairly:

  "literal"    — names identifiers, files, or symbols that appear in this diff \
(the easy case; a smoke test).
  "functional" — describes what the change DOES, using none of the diff's \
distinctive identifiers, so this tests meaning-matching rather than \
string-matching.
  "intent"     — how someone would describe the goal from memory weeks later, \
fuzzy and incomplete ("that session where we stopped the uploader retrying \
forever").

Never mention the commit SHA — it does not appear in the conversation.

Only write a query if the session behind THIS change is a genuinely strong \
answer to it. Other sessions in the same repository are competing answers, so a \
query that any of them would satisfy equally well is not discriminative — drop \
it. Quality over quantity.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"queries": [{{"query": "<what a developer types>", \
"difficulty": "literal|functional|intent"}}, ...], \
"note": "<optional: why you skipped a tier>"}}

Return an empty "queries" list if the change is too trivial or generic to target \
fairly (a version bump, a lockfile refresh, a pure formatting pass)."""


# ── linkage ─────────────────────────────────────────────────────────────────

def linkage_path() -> Path:
    """Default linkage file: ``<gold dir>/commit-linkage.jsonl``."""
    return fw.gold_dir() / LINKAGE_NAME


def load_linkage(path: Path) -> list[dict]:
    """Linkage rows that carry everything a case needs: a thread id, a repo, and
    at least one commit with a message. Malformed or incomplete rows are skipped
    rather than half-mined — a linkage file is machine-written, so a bad row is a
    builder bug, not something to guess around."""
    if not path.exists():
        raise SystemExit(
            f"no linkage file at {path}. The commit miner needs session ↔ commit "
            "provenance for the corpus; build it first (for SWE-chat: "
            "`python search_lab/swechat_corpus.py --linkage-only`), or pass --linkage.")
    out: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        commits = [c for c in (row.get("commits") or [])
                   if isinstance(c, dict) and (c.get("message") or "").strip()]
        if not (row.get("thread_id") and row.get("repo") and commits):
            continue
        out.append({"thread_id": str(row["thread_id"]), "repo": str(row["repo"]),
                    "session_id": str(row.get("session_id") or ""),
                    "files": [str(f) for f in (row.get("files") or [])],
                    "commits": commits})
    return out


def resolvable_ids(ids: set[str]) -> set[str]:
    """Which of ``ids`` are conversation threads present in the current snapshot.

    A linkage file outlives the corpus it describes: a session withdrawn upstream,
    or simply never ingested, leaves a row pointing at nothing. Filtering here means
    a stale row drops out of the sample instead of minting a case whose gold can
    never rank."""
    if not ids:
        return set()
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT id FROM threads WHERE thread_type = 'conversation' "
            "AND NOT exclude_from_search")).all()
    live = {r[0] for r in rows}
    return {i for i in ids if i in live}


def sample_units(rows: list[dict], n: int, seed: int, skip: set[str],
                 per_repo: int) -> list[dict]:
    """``n`` linked sessions to mine, stratified so no repository contributes more
    than ``per_repo``. Excludes ``skip`` (already mined). Deterministic in ``seed``.

    Stratification is the whole point: repo sizes in a real corpus are power-law
    skewed, and an unstratified draw would spend most of the budget inside the one
    or two largest codebases."""
    pool = [r for r in rows if r["thread_id"] not in skip]
    rng = random.Random(seed)
    rng.shuffle(pool)
    taken: dict[str, int] = {}
    out: list[dict] = []
    for row in pool:
        if len(out) >= n:
            break
        if taken.get(row["repo"], 0) >= per_repo:
            continue
        taken[row["repo"]] = taken.get(row["repo"], 0) + 1
        out.append(row)
    return out


def build_grades(unit: dict, siblings: list[dict], seed: int) -> dict[str, int]:
    """The graded pool for one unit: the linked session at 2, same-repo siblings
    sharing a touched file at 1, the rest of the repo at 0.

    Capped and deterministically sampled — a repo with hundreds of sessions would
    otherwise put its whole roster in every case. Grades 1 and 0 are structural
    proxies (see the module docstring); only the 2 is grounded in provenance."""
    gold = unit["thread_id"]
    files = {f for f in unit["files"] if f}
    partial: list[str] = []
    confound: list[str] = []
    for sib in siblings:
        tid = sib["thread_id"]
        if tid == gold:
            continue
        if files and files & {f for f in sib["files"] if f}:
            partial.append(tid)
        else:
            confound.append(tid)
    rng = random.Random(f"{seed}:{gold}")
    rng.shuffle(partial)
    rng.shuffle(confound)
    grades = {gold: 2}
    for tid in partial[:MAX_PARTIAL]:
        grades[tid] = 1
    for tid in confound[:MAX_CONFOUND]:
        grades[tid] = 0
    return grades


# ── pure logic (tested) ─────────────────────────────────────────────────────

def parse_queries(text: str) -> dict | None:
    """The author agent's queries, or None if unparseable. Mirrors query-gen's
    contract: a kept query needs a non-empty string ``query`` and a ``difficulty``
    in :data:`DIFFICULTIES`, duplicates collapse, and at most one query per tier
    survives so a chatty run can't outweigh the single-case targets when the file
    is scored. An empty list is valid — the agent judged the change untargetable."""
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
    seen_tiers: set[str] = set()
    for q in v["queries"]:
        if not isinstance(q, dict):
            continue
        query, difficulty = q.get("query"), q.get("difficulty")
        if not isinstance(query, str) or not query.strip():
            continue
        if difficulty not in DIFFICULTIES or difficulty in seen_tiers:
            continue
        query = query.strip()
        if query in seen:
            continue
        seen.add(query)
        seen_tiers.add(difficulty)
        out.append({"query": query, "difficulty": difficulty})
    return {"queries": out, "note": v.get("note")}


def build_prompt(unit: dict) -> str:
    """The author's brief: one unit's commits, rendered as message + truncated
    diff. Multiple commits concatenate — they are one session's output, so they
    describe one body of work."""
    messages = "\n".join(f"    {c['message'].strip()}" for c in unit["commits"])
    patches = "\n".join(c.get("patch") or "" for c in unit["commits"])
    patch = patches[:MAX_PATCH_CHARS] or "    (no diff recorded)"
    files = ", ".join(unit["files"][:20]) or "(not recorded)"
    return _PROMPT.format(repo=unit["repo"], files=files, message=messages,
                          patch=patch)


def cases_from_queries(unit: dict, queries: list[dict], grades: dict[str, int],
                       snapshot_id: str) -> list[dict]:
    """One eval ``--cases`` row per authored query. Gold is the linked session;
    ``grades`` carries the structural pool so nDCG scores ordering against real
    same-repo confounds. ``sessions`` is empty — the query was authored from a
    commit, so no session in the corpus quotes it."""
    stamp = now_iso()
    gold = unit["thread_id"]
    return [{"query": q["query"], "gold": [gold], "grades": dict(grades),
             "sessions": [], "snapshot_id": snapshot_id, "protocol": "commit-linked",
             "difficulty": q["difficulty"], "target_thread": gold,
             "repo": unit["repo"],
             "commit_shas": [c.get("sha") for c in unit["commits"] if c.get("sha")],
             "mined_at": stamp}
            for q in queries]


# ── the agent seam ──────────────────────────────────────────────────────────

def generate_case(unit: dict, grades: dict[str, int], model: str, tool_cmd: str,
                  snapshot_id: str, run=run_claude) -> tuple[list[dict], dict]:
    """Author the cases for one linked session. Returns (rows, detail_row); rows
    may be empty (agent failed, or judged the change untargetable), which the
    caller counts as no cases written but not a hard failure."""
    prompt = build_prompt(unit)
    text, stats = run(prompt, model, tool_cmd,
                      max_turns=GEN_MAX_TURNS, timeout=GEN_TIMEOUT_S)
    detail = {"thread_id": unit["thread_id"], "repo": unit["repo"],
              "session_id": unit.get("session_id", ""),
              "commit_shas": [c.get("sha") for c in unit["commits"]],
              "pool_size": len(grades), "agent": stats}
    if text is None:
        detail["outcome"] = "agent-failed"
        return [], detail
    parsed = parse_queries(text)
    if parsed is None:
        detail["outcome"] = "unparseable"
        return [], detail
    rows = cases_from_queries(unit, parsed["queries"], grades, snapshot_id)
    prov = {"gen_model": stats.get("model") or model,
            "prompt_sha": fw.prompt_sha(prompt), "miner_commit": fw.miner_commit()}
    for row in rows:
        row.update(prov)
    detail.update({"outcome": "ok" if rows else "no-queries",
                   "queries": parsed["queries"], "note": parsed["note"]})
    return rows, detail


# ── the miner ───────────────────────────────────────────────────────────────

class CommitMiner(Miner):
    name = "commit"
    summary = ("author queries from a commit; test the session that produced it "
               "ranks (provenance gold)")
    measures = "recall (provenance gold)"
    unit = "linked session"
    cost = "1 agent / session (reads a diff, writes queries)"
    target_kind = "per-case"
    target_help = "commit-linked sessions to sample, one agent each"
    default_target = 5
    cases_stem = CASES_STEM
    # Needs a linkage file the corpus must supply, which a plain archive has no
    # reason to carry — `mine all` would fail on every ordinary home.
    runnable_in_all = False

    def add_arguments(self, parser) -> None:
        parser.add_argument("--linkage", type=Path, default=None, metavar="PATH",
                            help=f"session↔commit linkage file (default: "
                            f"~/.thread/archive/{LINKAGE_NAME})")
        parser.add_argument("--per-repo", type=int, default=DEFAULT_PER_REPO,
                            metavar="N",
                            help="max sampled sessions per repository "
                            f"(default {DEFAULT_PER_REPO}; stratifies a skewed corpus)")

    def run(self, ctx: MineContext) -> MineResult:
        args = ctx.args
        cases_path, detail_path = fw.open_output(
            args.out, fw.default_cases_path(self.cases_stem))
        rows = load_linkage(args.linkage.expanduser() if args.linkage
                            else linkage_path())

        # Drop rows whose thread is absent from the snapshot before sampling, so a
        # stale linkage file spends the budget on minable units instead of misses.
        live = resolvable_ids({r["thread_id"] for r in rows})
        stale = len(rows) - len(live)
        rows = [r for r in rows if r["thread_id"] in live]
        if not rows:
            raise SystemExit(
                "no linkage row resolves to a thread in this snapshot — is the "
                "linkage file built against a different corpus home?")

        by_repo: dict[str, list[dict]] = {}
        for row in rows:
            by_repo.setdefault(row["repo"], []).append(row)

        skip = fw.mined_gold_ids(cases_path)
        units = sample_units(rows, ctx.target, args.seed, skip, args.per_repo)
        if not units:
            raise SystemExit("no unmined commit-linked sessions to author from")
        print(f"authoring queries from {len(units)} commit-linked session(s) with "
              f"{ctx.model} agents (jobs={ctx.jobs}) against snapshot "
              f"{ctx.snapshot_id} -> {cases_path}")

        agent = ctx.agent_run or run_claude
        writer = fw.CaseWriter(self.name, cases_path, detail_path)
        written = empty = 0
        outcomes: dict[str, int] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as ex:
            futures = {
                ex.submit(generate_case, u,
                          build_grades(u, by_repo.get(u["repo"], []), args.seed),
                          ctx.model, ctx.tool_cmd, ctx.snapshot_id, agent): u
                for u in units}
            for fut in concurrent.futures.as_completed(futures):
                rows_out, detail = fut.result()
                writer.write_detail(detail)
                outcomes[detail["outcome"]] = outcomes.get(detail["outcome"], 0) + 1
                if not rows_out:
                    empty += 1
                    print(f"  ✗ {detail['thread_id']}: {detail['outcome']}")
                    continue
                for row in rows_out:
                    writer.write_case(row)
                written += len(rows_out)
                tiers = ", ".join(r["difficulty"] for r in rows_out)
                print(f"  ✓ {detail['thread_id']} ({detail['repo']}): "
                      f"{len(rows_out)} queries ({tiers}), pool {detail['pool_size']}")
        notes = []
        if empty:
            notes.append(f"{empty}/{len(units)} session(s) yielded no fair query "
                         "(change judged untargetable, or the agent failed)")
        if stale:
            notes.append(f"{stale} linkage row(s) name a thread absent from this "
                         "snapshot and were skipped")
        return MineResult(written=written, failed=empty, cases_path=cases_path,
                          detail_path=detail_path, notes=notes,
                          attempted=len(units), outcomes=outcomes)


MINER = CommitMiner()
