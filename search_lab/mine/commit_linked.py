"""Commit-linked gold labels — findability cases whose gold is fixed by
provenance rather than by retrieval.

The session that answers a query is not *judged* to be the answer and not
*sampled* at random: it is the session that **provably produced a commit**. That
is what makes this a measurement rather than a description of the incumbent
ranker — a label established by searching the corpus can only mark what the
ranker already reaches, so a systematic blind spot never scores as a miss and the
number is an upper bound on itself by an unknown margin. Here no search runs
during labeling at all.

The input is a **linkage file** — one row per session that is externally linked to
the commits it authored (see :data:`LINKAGE_NAME`). A corpus that ships session ↔
commit provenance can produce one; ``search_lab/swechat_corpus.py`` writes it for the
SWE-chat dataset. Nothing here is specific to that dataset: the miner reads the
linkage shape, not a corpus.

Two further properties carry the protocol:

- **No vocabulary leakage.** The query is authored from the *commit* — message and
  diff — and the agent runs with :func:`~._agent.run_claude`'s
  ``corpus_access=False``, so it holds no tool that could reach the target thread
  and cannot echo the answer's own wording back as the query. A prompt asking it
  not to look would be a request; an empty allowlist is the guarantee the protocol
  needs. That is also how a person actually searches ("where did we change the
  retry backoff") — from the code they remember, not from the conversation they
  are trying to find.
- **Structural confounds, no tokens.** Sibling sessions in the same repository are
  the hard negatives — topically identical, wrong answer — and repo membership
  plus touched files grades them for free: the intended session is 2, a sibling
  touching overlapping files is 1 (plausibly partial), a sibling touching disjoint
  files is 0. A confound pool with no judge in it, and so no judge's reach to
  bound it.

**Read grades 1 and 0 as structural proxies, not verified labels.** Only the 2 is
a strong claim (provenance says so). A disjoint-file sibling *could* still answer a
vague query; file overlap is a heuristic for "related work", not a judgment. The
gold-2 recall numbers are the trustworthy signal, and nDCG over the proxy pool is
directional.

Resume is by gold thread *and by refusal*: a re-run skips sessions already
represented in the case file, and sessions a paid gate already rejected — re-buying
a verdict that cost money and cannot have changed is waste. A refusal binds only
while the gate that made it is unchanged, so a reworded audit re-opens everything
it turned down (see :func:`~._framework.refused_units`). Every refusal is written to
a ``-rejects.jsonl`` beside the cases: what a corpus is refused *for* is a finding
about that corpus, and at scale ``misattributed`` says a provenance join is wrong
while ``untargetable-commit`` says the commit hygiene is.
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

CASES_STEM = "commit-cases"

#: The linkage file's default name under the gold dir. Written by a corpus
#: builder, consumed here; ``--linkage`` overrides the path.
LINKAGE_NAME = "commit-linkage.jsonl"

DIFFICULTIES = ("literal", "functional", "intent")

# How much diff the authoring agent sees across a unit's commits — split among
# them by :func:`split_budget`, not spent front-to-back. Enough to characterize the
# change without turning one prompt into a whole file's worth of context.
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

# The authoring agent writes queries from a commit and holds no tools; one pass is
# all it has to make, and the couple of spare turns only cover a reply it wants to
# reformat.
GEN_MAX_TURNS = 4
GEN_TIMEOUT_S = 300

# The alignment auditor reads a whole session before it decides, so unlike the
# authoring agent it needs a real exploration loop and a wall clock to match.
AUDIT_MAX_TURNS = 20
AUDIT_TIMEOUT_S = 600

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
  "intent"     — how someone would describe the goal from memory weeks later: \
fuzzy and incomplete, the outcome rather than the mechanism. Phrase it however \
this particular change would come back to someone. Do NOT reach for a recall \
formula ("that time we…", "that session where…") — a benchmark of one opening \
with the change swapped out measures the template, not the query.

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


_ALIGNMENT_PROMPT = """You are auditing a benchmark's labels before it is built.

A dataset claims this archived coding session produced this git commit. Your job \
is to check that claim, because a query authored from this commit will be graded \
on whether search returns this session — and if the two are not the same work, \
the label is simply wrong and the case is unanswerable by construction.

  repository: {repo}
  files the commit changed: {files}

  commit message:
{message}

  diff (truncated):
{patch}

Read the session in full:

  {tool_cmd} read {thread_id}

Answer two separate questions.

1. "aligned" — did THIS session do THIS work? You are looking for the change \
itself in the transcript: the files, the edits, the reasoning that produced them. \
A session that merely ran `git commit` on someone else's work is NOT aligned. \
Neither is one that touches the same area but not this change.

2. "targetable" — is there enough distinctive substance in the COMMIT (its \
message or its diff) for someone to write a search query that this session \
answers and its sibling sessions do not? A lazy or generic message ("wip", \
"fixes", "0.4.9", "Backpack") with a diff that could be anything is not \
targetable, even when the linkage is perfectly correct. This is a judgment about \
the commit as *material*, not about the session's quality.

The two are independent and a "no" on either is a useful answer, not a failure. \
Be strict: a case admitted here becomes a permanent label nobody re-reads.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"aligned": true|false, "targetable": true|false, \
"reason": "<one line: what you found in the session, or what is missing>"}}"""


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
    """The author agent's queries, or None if unparseable. A kept query needs a
    non-empty string ``query`` and a ``difficulty``
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


def split_budget(lengths: list[int], budget: int) -> list[int]:
    """How many characters each diff gets out of ``budget``, shortest served first
    so a patch that needs less than its share hands the surplus back. Every diff
    that fits is whole, and what has to be cut is cut evenly."""
    out = [0] * len(lengths)
    left = budget
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for n, i in enumerate(order):
        share = left // (len(order) - n)
        out[i] = min(lengths[i], share)
        left -= out[i]
    return out


def render_patches(commits: list[dict]) -> tuple[str, int]:
    """The prompt's diff block, and how many diffs had to be clipped to fit.

    The budget is split across the commits rather than spent front-to-back: a
    session's commits are concatenated in order, so a first diff at the cap leaves
    the rest of the body of work invisible while their *messages* still reach the
    agent — which is asking it to author queries for a change it cannot see. A
    clipped diff says so inline, so the agent knows it is reading part of one."""
    patches = [c.get("patch") or "" for c in commits]
    shares = split_budget([len(p) for p in patches], MAX_PATCH_CHARS)
    parts: list[str] = []
    clipped = 0
    for patch, share in zip(patches, shares):
        if not patch:
            continue
        if share < len(patch):
            clipped += 1
            parts.append(patch[:share] + "\n    … (diff truncated)")
        else:
            parts.append(patch)
    return "\n".join(parts), clipped


def build_prompt(unit: dict) -> str:
    """The author's brief: one unit's commits, rendered as message + diff. Multiple
    commits appear together — they are one session's output, so they describe one
    body of work — each with its own share of the diff budget."""
    messages = "\n".join(f"    {c['message'].strip()}" for c in unit["commits"])
    patch, _clipped = render_patches(unit["commits"])
    files = ", ".join(unit["files"][:20]) or "(not recorded)"
    return _PROMPT.format(repo=unit["repo"], files=files,
                          patch=patch or "    (no diff recorded)", message=messages)


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
                      max_turns=GEN_MAX_TURNS, timeout=GEN_TIMEOUT_S,
                      corpus_access=False)
    detail = {"thread_id": unit["thread_id"], "repo": unit["repo"],
              "session_id": unit.get("session_id", ""),
              "commit_shas": [c.get("sha") for c in unit["commits"]],
              "pool_size": len(grades), "agent": stats,
              # How much of the change the author could not see. A case whose
              # diffs were clipped was authored from partial material, and that
              # belongs on the record rather than in the size of a constant.
              "patch_clipped": render_patches(unit["commits"])[1]}
    if text is None:
        detail["outcome"] = "agent-failed"
        return [], detail
    parsed = parse_queries(text)
    if parsed is None:
        detail["outcome"] = "unparseable"
        return [], detail
    rows = cases_from_queries(unit, parsed["queries"], grades, snapshot_id)
    prov = {"gen_model": stats.get("model") or model,
            "prompt_sha": fw.prompt_sha(prompt),
            "template_sha": fw.template_sha(_PROMPT),
            "miner_commit": fw.miner_commit()}
    for row in rows:
        row.update(prov)
    detail.update({"outcome": "ok" if rows else "no-queries",
                   "queries": parsed["queries"], "note": parsed["note"]})
    return rows, detail


# ── the stages ──────────────────────────────────────────────────────────────
#
# Three gates, cheapest first, and the ordering is the whole design: the trail
# check costs nothing and refuses a unit outright, so the paid gate is only ever
# asked about units that already survived it, and the authoring agent is only ever
# asked about units two gates have admitted.
#
# The two paid drops are deliberately separate verdicts. A unit refused as
# *misattributed* is a broken label leaving the file — the benchmark gets more
# correct. A unit refused as *untargetable* is a hard case leaving the file — the
# benchmark gets easier. They are both right to drop and they move the number in
# opposite directions, so they are counted apart and the run says so.


def commit_files(unit: dict) -> list[str]:
    """The repo-relative paths a unit's commits changed.

    SWE-chat stores ``files_changed`` as ``git diff --name-status`` output, so a
    row is ``M\\tpath`` and the status letter has to come off before anything can
    match a path against it."""
    out: list[str] = []
    for commit in unit["commits"]:
        for raw in commit.get("files") or []:
            path = raw.strip()
            if "\t" in path:
                path = path.split("\t")[-1].strip()
            if path and path not in out:
                out.append(path)
    return out


def trail_edits(thread_id: str) -> set[str]:
    """Absolute paths a session edited, from the code projection."""
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT DISTINCT path FROM event_paths WHERE thread_id = :t "
            "AND op IN ('edit', 'write', 'delete')"), {"t": thread_id}).all()
    return {str(r[0]) for r in rows}


def trail_available() -> bool:
    """Whether this corpus has a folded code projection at all.

    Without one every unit would fail the provenance gate and the run would report
    a corpus of pure misattribution, which is a spectacular way to be wrong. An
    empty projection means *unmeasured*, not *disproven*, so the gate stands down
    and the run says it did."""
    with use_session() as s:
        return bool(s.execute(sa_text(
            "SELECT 1 FROM event_paths LIMIT 1")).first())


def stage_provenance(unit: dict, ctx: MineContext) -> fw.Verdict:
    """Free gate: did the linked session actually edit the files its commit
    changed?

    The dataset's attribution is a join over checkpoints; this is the tool-use
    trail, which is a record of what the session *did*. Where they disagree the
    trail wins, and it costs nothing to ask. On the SWE-chat linkage this refuses
    about a tenth of rows before any agent sees them."""
    if not getattr(ctx.args, "provenance_gate", True) or not trail_available():
        return fw.Verdict(keep=unit, reason="ok")
    files = commit_files(unit)
    if not files:
        return fw.Verdict(keep=unit, reason="ok",
                          detail={"note": "commit lists no files; gate skipped"})
    edited = trail_edits(unit["thread_id"])
    if not edited:
        return fw.Verdict(reason="no-trail",
                          detail={"note": "session edited nothing in this corpus"})
    hits = [f for f in files if any(p.endswith("/" + f) or p == f for p in edited)]
    if not hits:
        return fw.Verdict(reason="no-file-overlap",
                          detail={"commit_files": len(files), "overlap": 0})
    unit["_overlap"] = len(hits) / len(files)
    return fw.Verdict(keep=unit, reason="ok",
                      detail={"commit_files": len(files), "overlap": len(hits)})


def stage_alignment(unit: dict, ctx: MineContext) -> fw.Verdict:
    """Paid gate: an agent reads the commit *and* the session and confirms they are
    the same work, and that the commit is distinctive enough to author from.

    **This agent reads the thread and the authoring agent must never see what it
    found.** That is what keeps the protocol intact: the verdict is two booleans
    and a sentence, it lands in the detail sidecar, and nothing from it is
    interpolated into the authoring prompt — which is built from ``unit`` alone,
    from material that was already there before this stage ran. A validator that
    handed its reasoning forward would be a vocabulary leak wearing a QA badge."""
    prompt = _ALIGNMENT_PROMPT.format(
        repo=unit["repo"], files=", ".join(commit_files(unit)[:20]) or "(not recorded)",
        message="\n".join(f"    {c['message'].strip()}" for c in unit["commits"]),
        patch=render_patches(unit["commits"])[0] or "    (no diff recorded)",
        tool_cmd=ctx.tool_cmd, thread_id=unit["thread_id"])
    run = ctx.agent_run or run_claude
    # Explicit, though it is the default: which of these two agents may read the
    # corpus is the entire protocol, so neither call leaves it to a default.
    text, stats = run(prompt, ctx.model, ctx.tool_cmd,
                      max_turns=AUDIT_MAX_TURNS, timeout=AUDIT_TIMEOUT_S,
                      corpus_access=True)
    cost = float((stats or {}).get("cost_usd") or 0.0)
    if text is None:
        return fw.Verdict(reason="audit-failed", cost_usd=cost, detail={"agent": stats})
    verdict = parse_alignment(text)
    if verdict is None:
        return fw.Verdict(reason="audit-unparseable", cost_usd=cost,
                          detail={"agent": stats})
    detail = {"agent": stats, **verdict,
              "template_sha": fw.template_sha(_ALIGNMENT_PROMPT)}
    if not verdict["aligned"]:
        return fw.Verdict(reason="misattributed", cost_usd=cost, detail=detail)
    if not verdict["targetable"]:
        return fw.Verdict(reason="untargetable-commit", cost_usd=cost, detail=detail)
    return fw.Verdict(keep=unit, reason="ok", cost_usd=cost, detail=detail)


def parse_alignment(text: str) -> dict | None:
    """The auditor's verdict, or None if unparseable. Both booleans are required —
    a reply that answers one question is not a verdict, and defaulting the missing
    one either way would silently turn an audit into a rubber stamp."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    if not isinstance(raw.get("aligned"), bool) or not isinstance(raw.get("targetable"), bool):
        return None
    reason = raw.get("reason")
    return {"aligned": raw["aligned"], "targetable": raw["targetable"],
            "reason": reason if isinstance(reason, str) else None}


def stage_author(unit: dict, ctx: MineContext) -> fw.Verdict:
    """Paid stage: the blind authoring agent, holding no tools, writes the queries.

    Reads ``unit`` and nothing else — in particular nothing the alignment gate
    learned. See :func:`stage_alignment`."""
    rows, detail = generate_case(unit, unit["_grades"], ctx.model, ctx.tool_cmd,
                                 ctx.snapshot_id, ctx.agent_run or run_claude)
    cost = float((detail.get("agent") or {}).get("cost_usd") or 0.0)
    if not rows:
        return fw.Verdict(reason=detail["outcome"], cost_usd=cost, detail=detail)
    unit["_rows"] = rows
    return fw.Verdict(keep=unit, reason="ok", cost_usd=cost, detail=detail)


def stage_verify(unit: dict, ctx: MineContext) -> fw.Verdict:
    """Free QA over what was produced: does each query honour the tier it claims,
    and is it a new phrasing rather than the file's house sentence again?

    A query failing either is dropped from the unit, not the unit from the run — a
    session that yielded a good ``literal`` and a templated ``intent`` should
    contribute the one that holds. A unit left with nothing drops as
    ``all-queries-rejected``, which is a different event from the author declining
    to write any and is counted apart."""
    material = " ".join(
        (c.get("message") or "") + " " + (c.get("patch") or "")
        for c in unit["commits"])
    kept, rejected = [], []
    for row in unit["_rows"]:
        why = fw.tier_violation(row["query"], material, row["difficulty"])
        if why is None and fw.repeats_an_opening(row["query"], ctx.seen_openings):
            why = "repeats-an-opening"
        if why:
            rejected.append({"query": row["query"], "tier": row["difficulty"],
                             "why": why})
            continue
        ctx.seen_openings.add(fw.opening_of(row["query"]))
        kept.append(row)
    detail = {"kept": len(kept), "rejected": rejected}
    if not kept:
        return fw.Verdict(reason="all-queries-rejected", detail=detail)
    unit["_rows"] = kept
    return fw.Verdict(keep=unit, reason="ok", detail=detail)


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
    gold_source = ("commit provenance — the session a linkage file says authored "
                   "the commit; no search runs during labeling")
    retrieval_free = True
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
        parser.add_argument("--no-provenance-gate", dest="provenance_gate",
                            action="store_false",
                            help="skip the free trail check (does the session "
                                 "actually edit the files its commit changed)")
        parser.add_argument("--no-alignment", dest="alignment", action="store_false",
                            help="skip the paid audit that reads commit and session "
                                 "together before anything is authored. Halves the "
                                 "spend and admits units nothing has checked")

    def stages(self, args) -> list[fw.Stage]:
        """The declared funnel. Free gate first so the paid one is asked about
        less, and the authoring agent last so it is asked about least."""
        out = [fw.Stage(name="provenance", fn=stage_provenance, kind="free",
                        summary="session's trail edits the commit's files")]
        if getattr(args, "alignment", True):
            out.append(fw.Stage(name="alignment", fn=stage_alignment, kind="agent",
                                summary="commit and session are the same work, "
                                        "and the commit is targetable",
                                gate_sha=fw.template_sha(_ALIGNMENT_PROMPT)))
        out.append(fw.Stage(name="author", fn=stage_author, kind="agent",
                            summary="blind authoring of the queries",
                            gate_sha=fw.template_sha(_PROMPT)))
        out.append(fw.Stage(name="verify", fn=stage_verify, kind="free",
                            summary="each query honours its tier and is not the "
                                    "file's house sentence again"))
        return out

    def run(self, ctx: MineContext) -> MineResult:
        args = ctx.args
        cases_path, detail_path = fw.open_output(
            args.out, fw.default_cases_path(self.cases_stem))
        funnel = fw.Funnel()

        # ── supply: what the corpus offers, before anything is drawn ──────────
        rows = load_linkage(args.linkage.expanduser() if args.linkage
                            else linkage_path())
        live = resolvable_ids({r["thread_id"] for r in rows})
        resolved = [r for r in rows if r["thread_id"] in live]
        funnel.note("linkage", n_in=len(rows), n_out=len(resolved),
                    reasons={"absent-from-snapshot": len(rows) - len(resolved)})
        if not resolved:
            raise SystemExit(
                "no linkage row resolves to a thread in this snapshot — is the "
                "linkage file built against a different corpus home?")

        by_repo: dict[str, list[dict]] = {}
        for row in resolved:
            by_repo.setdefault(row["repo"], []).append(row)

        stages = self.stages(args)
        rejects_path = fw.rejects_path_for(cases_path)
        mined = fw.mined_gold_ids(cases_path)
        # A paid gate's refusal is worth money and does not change on its own, so a
        # unit it already rejected is not re-bought — unless the gate itself moved,
        # which re-opens it.
        refused = fw.refused_units(rejects_path, fw.paid_gates(stages))
        skip = mined | refused
        unmined = [r for r in resolved if r["thread_id"] not in skip]
        units = sample_units(resolved, ctx.target, args.seed, skip, args.per_repo)
        funnel.note("sample", n_in=len(resolved), n_out=len(units),
                    reasons={"already-mined": len(resolved) - len(unmined) - len(
                                 [r for r in resolved if r["thread_id"] in refused]),
                             "already-refused": len(
                                 [r for r in resolved if r["thread_id"] in refused]),
                             "not-drawn": len(unmined) - len(units)})
        if not units:
            raise SystemExit("no unmined commit-linked sessions to author from")
        for unit in units:
            unit["_grades"] = build_grades(unit, by_repo.get(unit["repo"], []),
                                           args.seed)

        print(f"mining {len(units)} commit-linked session(s) through "
              f"{len(stages)} stage(s) with {ctx.model} agents (jobs={ctx.jobs}) "
              f"against snapshot {ctx.snapshot_id} -> {cases_path}")
        if not trail_available() and getattr(args, "provenance_gate", True):
            print("  note: this corpus has no folded code projection, so the "
                  "provenance gate stands down (unmeasured, not disproven)")

        # One detail row per unit, accumulating each stage's verdict, so the
        # sidecar answers "what happened to this session" in one place rather than
        # one line per stage that a reader has to join by hand.
        details: dict[str, dict] = {
            u["thread_id"]: {"thread_id": u["thread_id"], "repo": u["repo"],
                             "session_id": u.get("session_id", ""),
                             "commit_shas": [c.get("sha") for c in u["commits"]],
                             "pool_size": len(u["_grades"]), "at": now_iso(),
                             "stages": {}}
            for u in units}

        writer = fw.CaseWriter(self.name, cases_path, detail_path, rejects_path)

        def observe(stage: fw.Stage, unit: dict, verdict: fw.Verdict) -> None:
            row = details[unit["thread_id"]]
            row["stages"][stage.name] = {"reason": verdict.reason,
                                         **(verdict.detail or {})}
            row["outcome"] = verdict.reason if not verdict.kept else "ok"
            # A kept unit can still have had queries thrown out, and those are
            # negative results as much as a refused unit is — the record of what an
            # authoring agent produced that failed its own tier contract is exactly
            # what a prompt gets tuned against.
            tossed = (verdict.detail or {}).get("rejected")
            if tossed and verdict.kept and not ctx.plan:
                writer.write_reject({
                    "unit": unit["thread_id"], "stage": stage.name,
                    "reason": "queries-rejected", "gate_sha": stage.gate_sha,
                    "kind": stage.kind, "snapshot_id": ctx.snapshot_id,
                    "detail": {"rejected": tossed}})
            if not verdict.kept and not ctx.plan:
                writer.write_reject({
                    "unit": unit["thread_id"], "stage": stage.name,
                    "reason": verdict.reason, "gate_sha": stage.gate_sha,
                    "kind": stage.kind, "snapshot_id": ctx.snapshot_id,
                    "repo": unit["repo"],
                    "commit_shas": [c.get("sha") for c in unit["commits"]],
                    "detail": verdict.detail or {}})

        ctx.seen_openings |= fw.mined_openings(cases_path)
        surviving = fw.run_pipeline(stages, units, ctx, funnel=funnel,
                                    on_verdict=observe)
        if ctx.plan:
            return fw.plan_result(funnel, stages, surviving, cases_path)
        written = 0
        for unit in surviving:
            for row in unit["_rows"]:
                writer.write_case(row)
            written += len(unit["_rows"])
            tiers = ", ".join(r["difficulty"] for r in unit["_rows"])
            print(f"  ✓ {unit['thread_id']} ({unit['repo']}): "
                  f"{len(unit['_rows'])} queries ({tiers})")
        for row in details.values():
            writer.write_detail(row)

        outcomes: dict[str, int] = {}
        for row in details.values():
            outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
        notes = [f"funnel:\n{funnel.text(indent='    ')}"]
        if funnel.cost_usd:
            notes.append(f"spent ${funnel.cost_usd:.2f} "
                         f"(${funnel.cost_usd / max(written, 1):.3f}/case)")
        return MineResult(written=written, failed=len(units) - len(surviving),
                          cases_path=cases_path, detail_path=detail_path,
                          notes=notes, attempted=len(units), outcomes=outcomes,
                          funnel=funnel)


MINER = CommitMiner()
