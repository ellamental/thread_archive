"""Edit-linked gold labels — multi-answer cases whose gold is fixed by the
tool-use trail rather than by retrieval.

The unit is a **file path**, and the question is the one an agent actually asks
an archive: *which conversations worked on this?* The answer is not judged and
not sampled — it is enumerated. Every ``Edit`` / ``Write`` / ``apply_patch`` the
archive ever ingested is projected into ``event_paths`` (see
:mod:`thread_archive._retrieval.code`), so "the sessions that changed this file"
is a ``GROUP BY`` over a record of what happened, complete by construction. A
thread that search cannot surface is still in the gold, which is exactly what a
retrieval-labeled pool can never manage.

**This is the multi-answer rung.** A commit-linked case has one right answer, so
it measures findability and nothing about completeness — whether the window an
agent reads holds the *several* conversations that bear on a subject. A path
edited across six sessions has six grade-2 answers, and the fraction of them that
rank is a completeness measurement whose denominator nobody had to guess.

The graded pool costs no tokens, because the trail already separates the tiers:

- **2 — edited it.** ``edit`` / ``write`` / ``delete`` on this path.
- **1 — touched it without changing it.** ``read`` / ``search`` / ``run`` and no
  write. A session that grepped the file is a plausibly-partial answer to "who
  worked on this", not a wrong one.
- **0 — worked next door.** Edited a *sibling* path in the same directory and
  never this one. Topically adjacent, sharing module names and vocabulary, and
  wrong — the hard negative, established structurally.

Two things bound what this can claim.

**The query is authored, not observed.** As with commit-linked gold, an agent
writes the queries, so this fixes circularity and not query realism (see
``docs/search-quality.md`` → "What is not measured"). What it does not do is let
the agent read the conversations: it is handed the path and a sample of the
*edit payloads* — the diff-shaped record of what changed — and run with
:func:`~._agent.run_claude`'s ``corpus_access=False``, so it holds no tool that
could reach a thread.

**The residual leak is narrower than reading a thread, and it is not nil.** Those
edit payloads live inside the target conversations as tool events, and a tool
*call* is indexed (its ``file_path`` and arguments are searchable, though tool
*output* is not). So query vocabulary drawn from a payload can echo indexed text
in the very threads that are the answer. The ``functional`` and ``intent`` tiers
are instructed to diverge lexically for that reason, and the ``literal`` tier is
frankly a smoke test — it names the path, and the path is indexed.

Resume is by path: a re-run skips paths already represented in the file.
"""

from __future__ import annotations

import concurrent.futures
import json
import posixpath
import random

from sqlalchemy import text as sa_text

from thread_archive._store import use_session

from . import _framework as fw
from ._agent import run_claude
from ._framework import MineContext, Miner, MineResult, now_iso

CASES_STEM = "edited-cases"

DIFFICULTIES = ("literal", "functional", "intent")

#: Ops that count as changing a file — the grade-2 rule.
WRITE_OPS = ("edit", "write", "delete")
#: Ops that count as touching without changing — the grade-1 rule.
TOUCH_OPS = ("read", "search", "run")

#: A path needs at least this many editing sessions to be worth a case. Below it
#: the case is single-gold, which the commit miner already does better (its label
#: is provenance rather than a projection, and its query never sees the target).
#: The whole reason to mine a path is that several conversations answer it.
DEFAULT_MIN_SESSIONS = 2

#: And at most this many. A path edited by 200 sessions (a lockfile, a changelog,
#: an `__init__.py`) has a gold set no window could hold, so recall@k measures the
#: size of the set rather than the ranking; it is also rarely a coherent subject.
DEFAULT_MAX_SESSIONS = 12

#: Confound pool cap. Enough hard negatives for ordering to be measurable without
#: a busy directory carrying its whole file list into every case.
MAX_CONFOUND = 12
#: Grade-1 cap, same reasoning.
MAX_PARTIAL = 8

#: Sampled paths per directory. Edit counts cluster hard by module, so an
#: unstratified sample measures how well search works on one subsystem.
DEFAULT_PER_DIR = 2

#: Edit excerpts shown to the authoring agent, and how much of each. Enough to
#: characterize what changed without turning one prompt into a file's worth of
#: context.
MAX_EXCERPTS = 6
MAX_EXCERPT_CHARS = 700

# The authoring agent writes queries from a diff and holds no tools; it needs no
# exploration loop at all.
GEN_MAX_TURNS = 4
GEN_TIMEOUT_S = 300

_PROMPT = """You are writing search queries for a conversation-archive \
findability benchmark.

Below is one source file and a sample of the edits that were made to it. Several \
archived AI-coding conversations changed this file. Your job: write the queries \
someone who remembers *that work* would type to find *those conversations* again.

  path: {path}
  file: {basename}
  conversations that edited it: {n_sessions}
  first edit: {first_at}
  last edit: {last_at}

  sample of the edits made to this file:
{excerpts}

You have NO tools. You cannot read the conversations, and that is deliberate — \
these queries must be phrased the way someone recalls the *work*, not the way \
some transcript happens to be worded.

Write up to one query per tier — omit a tier you can't write fairly:

  "literal"    — names the file, its path, or symbols visible in these edits \
(the easy case; a smoke test).
  "functional" — describes what this file DOES or what these edits CHANGED, \
using none of the distinctive identifiers above, so this tests meaning-matching \
rather than string-matching.
  "intent"     — how someone would describe this work from memory weeks later: \
fuzzy and incomplete, the subject rather than the mechanism. Phrase it however \
this particular file's work would come back to someone. Do NOT reach for a recall \
formula ("that time we…", "the sessions where…") — a benchmark of one opening with \
the subject swapped out measures the template, not the query.

The answer is EVERY conversation that edited this file, so write queries about \
the file's subject rather than about one specific change to it. A query that only \
one of several editing sessions could answer is a worse case, not a better one.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"queries": [{{"query": "<what someone types>", \
"difficulty": "literal|functional|intent"}}, ...], \
"note": "<optional: why you skipped a tier>"}}

Return an empty "queries" list if this file is too generic to target fairly (a \
lockfile, a changelog, an empty __init__, a pure formatting pass)."""


# ── sampling: paths, their editors, and their structural pool ────────────────

def _rows(sql: str, params: dict) -> list:
    with use_session() as s:
        return list(s.execute(sa_text(sql), params).all())


def candidate_paths(*, min_sessions: int, max_sessions: int) -> list[dict]:
    """Paths edited by between ``min_sessions`` and ``max_sessions`` distinct
    conversations, newest activity first.

    Subagent threads (``system``) and search-excluded threads are dropped here
    rather than at grading time: they are not conversations anyone revisits, so
    counting them would let a path qualify on editors that never belong in a gold
    set."""
    write_ops = ", ".join(f"'{op}'" for op in WRITE_OPS)
    return [
        {"path": path, "n_sessions": int(n),
         "first_at": str(first_at or ""), "last_at": str(last_at or "")}
        for path, n, first_at, last_at in _rows(
            "SELECT p.path, count(DISTINCT p.thread_id) AS n, "
            "       min(p.occurred_at), max(p.occurred_at) "
            "FROM event_paths p JOIN threads t ON t.id = p.thread_id "
            f"WHERE p.op IN ({write_ops}) AND t.thread_type != 'system' "
            "  AND NOT t.exclude_from_search "
            "GROUP BY p.path HAVING n >= :lo AND n <= :hi "
            "ORDER BY max(p.occurred_at) DESC",
            {"lo": min_sessions, "hi": max_sessions})
    ]


def sample_paths(target: int, seed: int, skip: set[str], *, min_sessions: int,
                 max_sessions: int, per_dir: int) -> list[dict]:
    """``target`` paths, at most ``per_dir`` from any one directory, deterministic
    for a seed. Already-mined paths are dropped before sampling, so a re-run
    extends the file instead of re-spending on it."""
    pool = [p for p in candidate_paths(min_sessions=min_sessions,
                                       max_sessions=max_sessions)
            if p["path"] not in skip]
    rng = random.Random(seed)
    rng.shuffle(pool)
    out: list[dict] = []
    per_dir_count: dict[str, int] = {}
    for row in pool:
        d = posixpath.dirname(row["path"]) or "."
        if per_dir_count.get(d, 0) >= per_dir:
            continue
        per_dir_count[d] = per_dir_count.get(d, 0) + 1
        out.append(row)
        if len(out) >= target:
            break
    return out


def build_grades(path: str, seed: int) -> dict[str, int]:
    """The graded pool for one path, entirely from the trail: editors 2, other
    touchers 1, sibling-directory editors 0.

    Deterministic per path so a re-mine of the same path yields the same pool.
    Caps are applied after sorting by a seeded shuffle rather than by recency, so
    the confounds are not all drawn from one week."""
    write_ops = ", ".join(f"'{op}'" for op in WRITE_OPS)
    editors = [str(t) for (t,) in _rows(
        "SELECT DISTINCT p.thread_id FROM event_paths p "
        "JOIN threads t ON t.id = p.thread_id "
        f"WHERE p.path = :path AND p.op IN ({write_ops}) "
        "  AND t.thread_type != 'system' AND NOT t.exclude_from_search",
        {"path": path})]
    if not editors:
        return {}
    grades: dict[str, int] = {tid: 2 for tid in editors}

    touchers = [str(t) for (t,) in _rows(
        "SELECT DISTINCT p.thread_id FROM event_paths p "
        "JOIN threads t ON t.id = p.thread_id "
        "WHERE p.path = :path AND t.thread_type != 'system' "
        "  AND NOT t.exclude_from_search",
        {"path": path}) if str(t) not in grades]

    directory = posixpath.dirname(path)
    siblings: list[str] = []
    if directory:
        siblings = [str(t) for (t,) in _rows(
            "SELECT DISTINCT p.thread_id FROM event_paths p "
            "JOIN threads t ON t.id = p.thread_id "
            f"WHERE p.op IN ({write_ops}) AND p.path LIKE :dir AND p.path != :path "
            "  AND t.thread_type != 'system' AND NOT t.exclude_from_search",
            {"dir": directory + "/%", "path": path})
            if str(t) not in grades and str(t) not in touchers]

    rng = random.Random(f"{seed}:{path}")
    for pool, cap, grade in ((touchers, MAX_PARTIAL, 1),
                             (siblings, MAX_CONFOUND, 0)):
        chosen = sorted(pool)
        rng.shuffle(chosen)
        for tid in chosen[:cap]:
            grades[tid] = grade
    return grades


def edit_excerpts(path: str, *, limit: int = MAX_EXCERPTS) -> list[str]:
    """Short, readable excerpts of what the edits to ``path`` actually did.

    Read out of the tool-call payloads the path projection points at. Only the
    change itself is taken — never the surrounding conversation — so the agent
    characterizes the file from its diffs the way the commit miner works from a
    patch."""
    write_ops = ", ".join(f"'{op}'" for op in WRITE_OPS)
    rows = _rows(
        "SELECT e.payload FROM event_paths p JOIN events e ON e.id = p.event_id "
        f"WHERE p.path = :path AND p.op IN ({write_ops}) "
        "ORDER BY p.occurred_at LIMIT :lim",
        {"path": path, "lim": limit * 3})
    out: list[str] = []
    for (payload,) in rows:
        p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
        text = _excerpt_from_payload(p)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _excerpt_from_payload(payload: dict) -> str:
    """The changed text inside one tool-call payload, capped. Empty when the
    payload carries no recognizable edit body — a ``delete``, or a tool shape this
    does not model — which simply contributes no excerpt."""
    args = payload.get("input")
    if not isinstance(args, dict):
        return ""
    for key in ("new_string", "content", "patch", "new_str"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:MAX_EXCERPT_CHARS]
    return ""


# ── the authoring agent ─────────────────────────────────────────────────────

def build_prompt(row: dict, excerpts: list[str]) -> str:
    """The authoring prompt for one path. Carries the path and its edits; carries
    nothing from any conversation."""
    body = "\n".join(f"    --- edit {i + 1} ---\n    "
                     + text.replace("\n", "\n    ")
                     for i, text in enumerate(excerpts)) or "    (no edit text recorded)"
    return _PROMPT.format(
        path=row["path"], basename=posixpath.basename(row["path"]),
        n_sessions=row["n_sessions"], first_at=row["first_at"] or "unknown",
        last_at=row["last_at"] or "unknown", excerpts=body)


def parse_queries(text: str) -> dict | None:
    """The agent's queries, or None if unparseable. A kept query needs a non-empty
    string ``query`` and a ``difficulty`` in :data:`DIFFICULTIES`, duplicates
    collapse, and at most one query per tier survives so a chatty run cannot
    outweigh a terse one when the file is scored. An empty list is valid — the
    agent judged the file untargetable."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("queries"), list):
        return None
    kept: list[dict] = []
    seen_text: set[str] = set()
    seen_tiers: set[str] = set()
    for item in raw["queries"]:
        if not isinstance(item, dict):
            continue
        query = item.get("query")
        difficulty = item.get("difficulty")
        if not isinstance(query, str) or not query.strip():
            continue
        if difficulty not in DIFFICULTIES or difficulty in seen_tiers:
            continue
        if query.strip() in seen_text:
            continue
        seen_text.add(query.strip())
        seen_tiers.add(difficulty)
        kept.append({"query": query.strip(), "difficulty": difficulty})
    note = raw.get("note")
    return {"queries": kept, "note": note if isinstance(note, str) else None}


def cases_from_queries(row: dict, grades: dict[str, int], queries: list[dict],
                       snapshot_id: str) -> list[dict]:
    """One case per query. Gold is every grade-2 thread — the whole editing set,
    not a representative of it, which is what makes recall on these cases mean
    completeness."""
    gold = sorted(tid for tid, g in grades.items() if g == 2)
    return [{
        "query": q["query"],
        "gold": gold,
        "grades": grades,
        "sessions": [],
        "snapshot_id": snapshot_id,
        "protocol": "edit-linked",
        "difficulty": q["difficulty"],
        "path": row["path"],
        "n_gold": len(gold),
        "template_sha": fw.template_sha(_PROMPT),
        "mined_at": now_iso(),
    } for q in queries]


# ── the miner ───────────────────────────────────────────────────────────────

class EditedMiner(Miner):
    name = "edited"
    summary = ("author queries from a file's edits; test that every conversation "
               "which changed it ranks (trail-enumerated gold)")
    measures = "completeness (multi-answer)"
    unit = "path"
    cost = "1 agent / path (reads diffs, writes queries; no tools)"
    target_kind = "per-case"
    target_help = "edited paths to sample, one agent each"
    default_target = 5
    cases_stem = CASES_STEM
    gold_source = ("the tool-use trail's edit records — every conversation that "
                   "changed this path, enumerated from event_paths; no search "
                   "runs during labeling")
    retrieval_free = True
    # Needs the code projection, which any real archive has and a bare corpus may
    # not, so it stays a deliberate invocation rather than part of a blind sweep.
    runnable_in_all = False

    def add_arguments(self, parser) -> None:
        parser.add_argument("--min-sessions", type=int, default=DEFAULT_MIN_SESSIONS,
                            metavar="N",
                            help="fewest editing conversations a path needs "
                                 f"(default {DEFAULT_MIN_SESSIONS}; below this the "
                                 "case is single-gold and measures no completeness)")
        parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS,
                            metavar="N",
                            help="most editing conversations a path may have "
                                 f"(default {DEFAULT_MAX_SESSIONS}; past it recall@k "
                                 "measures gold-set size, not ranking)")
        parser.add_argument("--per-dir", type=int, default=DEFAULT_PER_DIR,
                            metavar="N",
                            help="most paths sampled from any one directory "
                                 f"(default {DEFAULT_PER_DIR})")

    def run(self, ctx: MineContext) -> MineResult:
        args = ctx.args
        cases_path, detail_path = fw.open_output(
            args.out, fw.default_cases_path(CASES_STEM))
        already = mined_paths(cases_path)
        rows = sample_paths(ctx.target, args.seed, already,
                            min_sessions=args.min_sessions,
                            max_sessions=args.max_sessions,
                            per_dir=args.per_dir)
        if not rows:
            raise SystemExit(
                "no unmined paths with "
                f"{args.min_sessions}-{args.max_sessions} editing conversations. "
                "If this corpus has never folded its code projection, the "
                "event_paths table is empty and no path can qualify. A live "
                "archive's watcher folds it; a lab-built corpus has no watcher, so "
                "fold it once with `python -c \"from thread_archive import _api; "
                "print(_api.code_index())\"` and try again.")

        writer = fw.CaseWriter(self.name, cases_path, detail_path)
        agent = ctx.agent_run or run_claude
        outcomes: dict[str, int] = {}
        written = failed = 0

        def mine_one(row: dict) -> tuple[dict, dict]:
            grades = build_grades(row["path"], args.seed)
            excerpts = edit_excerpts(row["path"])
            prompt = build_prompt(row, excerpts)
            reply, stats = agent(prompt, ctx.model, ctx.tool_cmd,
                                 max_turns=GEN_MAX_TURNS, timeout=GEN_TIMEOUT_S,
                                 corpus_access=False)
            detail = {"path": row["path"], "n_sessions": row["n_sessions"],
                      "n_excerpts": len(excerpts), "stats": stats,
                      "prompt_sha": fw.prompt_sha(prompt), "at": now_iso()}
            if reply is None:
                detail["outcome"] = "agent-failed"
                return detail, {}
            parsed = parse_queries(reply)
            if parsed is None:
                detail["outcome"] = "unparseable"
                return detail, {}
            detail.update({"note": parsed["note"],
                           "queries": [q["query"] for q in parsed["queries"]]})
            if not parsed["queries"]:
                detail["outcome"] = "no-queries"
                return detail, {}
            if len([g for g in grades.values() if g == 2]) < args.min_sessions:
                # The pool can shrink under the count that qualified the path when
                # a thread is excluded from search between sampling and grading.
                detail["outcome"] = "too-few-gold"
                return detail, {}
            detail["outcome"] = "ok"
            return detail, {"rows": cases_from_queries(
                row, grades, parsed["queries"], ctx.snapshot_id)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as pool:
            for detail, result in pool.map(mine_one, rows):
                outcomes[detail["outcome"]] = outcomes.get(detail["outcome"], 0) + 1
                writer.write_detail(detail)
                if not result:
                    failed += 1
                    print(f"  ✗ {detail['path']}: {detail['outcome']}")
                    continue
                for case in result["rows"]:
                    writer.write_case(case)
                written += len(result["rows"])
                print(f"  ✓ {detail['path']}: {len(result['rows'])} case(s), "
                      f"{result['rows'][0]['n_gold']} gold")

        notes = []
        if written:
            notes.append(f"gold sets: {_gold_spread(cases_path)}")
        return MineResult(written=written, failed=failed, cases_path=cases_path,
                          detail_path=detail_path, attempted=len(rows),
                          outcomes=outcomes, notes=notes)


def mined_paths(path) -> set[str]:
    """Paths already represented in a case file — the resume key. This miner's
    unit is a path, so neither the query dedupe nor the gold-thread dedupe the
    framework offers is the right one."""
    out: set[str] = set()
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("path"):
            out.add(str(row["path"]))
    return out


def _gold_spread(cases_path) -> str:
    """``min-max (median N)`` over the file's gold-set sizes — the number that
    says whether this file can measure completeness at all. A file of 2-gold cases
    resolves recall in halves."""
    sizes = sorted(
        len(json.loads(line).get("gold", []))
        for line in cases_path.read_text().splitlines() if line.strip())
    if not sizes:
        return "none"
    return f"{sizes[0]}-{sizes[-1]} (median {sizes[len(sizes) // 2]})"


MINER = EditedMiner()
