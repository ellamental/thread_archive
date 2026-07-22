"""Topic-mined gold labels — a curated topic's confounds turned into graded cases.

The query miner (``retrieval_mine_gold.py``) starts from queries agents *really
ran*; this one starts from a curated **topic** — a subject dense with near-misses
(a large, confound-rich topic like "suicide": lived experience vs. bot-death
grief vs. AI right-to-die vs. self-deprecation, all sharing vocabulary). A topic
that hard is where ranking earns or loses its keep, and the trail rarely supplies
enough real queries against it. So two stages of agents manufacture the benchmark:

1. **Survey** (one agent): searches the topic to understand it, decides the
   *angles* worth testing — as many as the topic genuinely warrants, its own
   call, no target count — and authors one query per angle. For each angle it
   records the intent, the confound subjects to exclude, and the candidate
   threads its own searches found.
2. **Label** (one agent per angle): takes the survey's candidates for that angle
   as a starting pool, verifies and *expands* them with its own searches to find
   everything relevant the survey missed, and grades a comprehensive pool —
   2 = answers the intent, 1 = partial / related, 0 = a confound. Gold is the
   grade-2 set. The labeler builds on the survey's work rather than rediscovering
   blind: the goal is the most complete gold set, and the survey's findings are
   signal (the labeler isn't the search system under test, so nothing leaks).

Snapshot binding, same as the query miner: run against a frozen corpus snapshot
(``thread_archive snapshot``; point ``THREAD_ARCHIVE_HOME`` at it), and the
topic must exist in it. Each case records the snapshot's ``snapshot_id``;
``retrieval_eval.py --cases`` scores over that same snapshot and refuses cases
once the id no longer matches (the corpus moved; re-mine). Output is the eval's
``--cases`` format with a graded pool for nDCG, plus a detail sidecar carrying
the facet map and per-angle intent/reasons.

Costs real tokens (one survey agent + one labeler per angle — minutes each) and
requires the ``claude`` CLI. Read-only against the archive. Case files quote
real usage — keep them out of the repo; they live beside the trend ledgers in
``~/.thread/archive/``.

    thread_archive snapshot ~/.thread/archive-snap
    export THREAD_ARCHIVE_HOME=~/.thread/archive-snap
    .venv/bin/python evals/topic_mine_gold.py --topic suicide
    .venv/bin/python evals/retrieval_eval.py --cases ~/.thread/archive/topic-cases-suicide.jsonl
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

# Reuse the query miner's headless-agent runner and its snapshot-bound corpus
# tools (the agents shell into `retrieval_mine_gold.py tool search|read`), so
# there is one implementation of "how an agent reaches the frozen corpus".
_SPEC = importlib.util.spec_from_file_location(
    "retrieval_mine_gold", Path(__file__).resolve().parent / "retrieval_mine_gold.py")
mine_gold = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("retrieval_mine_gold", mine_gold)
_SPEC.loader.exec_module(mine_gold)

from thread_archive import _api as api  # noqa: E402
from thread_archive._knowledge import topic_get  # noqa: E402
from thread_archive._ops.snapshot import read_snapshot_id  # noqa: E402

DEFAULT_MODEL = mine_gold.DEFAULT_MODEL

# How many of the topic's members to hand the survey agent inline (id + title),
# highest-cited first; it has search/read to reach the rest. Enough to ground the
# survey without pasting a thousand-member topic into the prompt.
SURVEY_MEMBER_SAMPLE = 80

# Runaway guard: never spawn more than this many labeler agents from one survey,
# whatever it authors. A cap on cost, not a target — the survey decides how many
# angles the topic actually warrants, below this bound.
MAX_ANGLES = 20

# Cap on the survey's candidate threads shown to a labeler per angle — bounds the
# label prompt; the labeler expands past them with its own searches anyway.
LABEL_CANDIDATE_CAP = 30

DEFAULT_CASES_TEMPLATE = "~/.thread/archive/topic-cases-{slug}.jsonl"

_METHOD = (
    "topic-driven: one survey agent searched the topic, decided the angles it "
    "warrants (as many as are genuinely distinct), and authored one query per "
    "angle with the candidate threads it found; then one labeler agent per query "
    "built on the survey's candidates — verifying and expanding them with its own "
    "searches — and graded a comprehensive pool (2=intended, 1=partial, 0=confound)."
)

_SURVEY_PROMPT = """You are designing a search-quality benchmark from one curated \
topic in a conversation archive.

Topic: {title}
{description}

This topic has {n_members} member conversations (threads cited to it). Here are \
the {n_shown} most-cited, as `thread_id  title`:

{members}

Explore the whole frozen corpus (not just this topic) to understand the subject \
and its look-alikes (run via Bash; read-only):

  {tool} search "<query>" [--limit N] [--rerank on|off|auto]
  {tool} read <thread_id> [--mode ends|chat|user|full|last] [--offset N] [--max-chars N]

Your job:

1. Search widely around this topic to understand it — its distinct sub-subjects \
and the neighboring subjects that share its vocabulary but are NOT it (near-misses \
look relevant; that is the point of testing on this topic). Search DEEP too: \
`search` returns 50 by default — read down the whole band, not just the top few, \
so you catch the look-alikes today's ranker buries.

2. Decide the ANGLES worth testing — each a distinct way a real user would come \
at this topic, and author ONE search query per angle. Use as many angles as the \
topic genuinely warrants: a broad, confound-rich topic supports more, a narrow \
one fewer. Don't pad with near-duplicate queries; don't collapse real \
distinctions. (You are the one who decides how many — there is no target count.)

3. For each angle, RECORD the threads you found that are relevant to it — their \
ids plus a short note. These become the starting pool for the deeper per-angle \
labeling pass, so capture what your searches surfaced, not just the query.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"facets": [{{"facet": "<short name>", "rough_volume": "small|medium|large", \
"example_thread_ids": ["<id>", ...]}}, ...], \
"angles": [{{"query": "<what a user types>", \
"intent": "<the one meaning this angle seeks>", \
"confounds": ["<look-alike subject to exclude>", ...], \
"candidates": [{{"thread_id": "<id>", "note": "<why relevant>"}}, ...]}}, ...]}}"""

_LABEL_PROMPT = """You are building the gold relevance set for one benchmark query \
against a frozen conversation archive.

Query: {query}
Intended meaning: {intent}
Nearby subjects that share this query's vocabulary but do NOT answer it \
(grade these 0 even though they look relevant): {confounds}

A survey pass already searched this topic and found these candidate threads for \
this angle — your STARTING POOL, to verify and build on (not to trust blindly, \
and not exhaustive):

{candidates}

Explore the corpus yourself (run via Bash; read-only):

  {tool} search "<query>" [--limit N] [--rerank on|off|auto]
  {tool} read <thread_id> [--mode ends|chat|user|full|last] [--offset N] [--max-chars N]

Method:
1. Start from the survey's candidates — read them (--mode ends is a cheap first \
read) and confirm or downgrade each; they are leads, not answers.
2. Expand past them: reformulate widely — synonyms, the intended meaning's own \
vocabulary, the confounds' vocabulary — to find relevant threads the survey \
missed. The benchmark's value is a COMPLETE gold set, so dig. `search` returns \
50 by default — read down the whole band; a thread that answers the intent \
belongs in the pool even when today's ranker buries it past the top few.
3. Grade every thread you inspected, ranked by how well it answers the intended \
meaning: 2 = answers it, 1 = partial or related-but-not-answering, 0 = a confound \
or irrelevant. Include the confounds you found at grade 0 — a benchmark needs the \
hard negatives, not only the positives.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"grades": {{"<thread_id>": 2, "<thread_id>": 0, ...}}, \
"reasons": {{"<thread_id>": "<one short clause>", ...}}}}

At least one grade-2 thread is expected; if truly nothing answers the intent, \
return grades with no 2s and say so in a reason."""


# ── pure logic (tested) ──────────────────────────────────────────────────────

def _extract_json(text: str) -> dict | None:
    """The outermost JSON object in an agent reply, tolerant of surrounding prose."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        v = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def _clean_candidates(raw) -> list[dict]:
    """Normalize a survey angle's candidate threads to ``[{thread_id, note}]``,
    dropping anything without a thread id. Tolerates a bare id string or a
    ``{thread_id, note}`` object."""
    out = []
    for c in raw if isinstance(raw, list) else []:
        if isinstance(c, str) and c.strip():
            out.append({"thread_id": c.strip(), "note": ""})
        elif isinstance(c, dict) and c.get("thread_id"):
            out.append({"thread_id": str(c["thread_id"]).strip(),
                        "note": str(c.get("note", ""))})
    return out


def parse_survey(text: str) -> dict | None:
    """The survey agent's angles (+ optional facet overview), or None. Each angle
    keeps a string ``query`` and ``intent``; one lacking either is dropped rather
    than mined half-formed. ``confounds`` normalized to strings; ``candidates``
    (the threads the survey found for the angle — the labeler's starting pool) to
    ``[{thread_id, note}]``; ``facets`` passed through as authored (detail-only)."""
    v = _extract_json(text)
    if v is None or not isinstance(v.get("angles"), list):
        return None
    angles = []
    for a in v["angles"]:
        if not isinstance(a, dict):
            continue
        query = a.get("query")
        intent = a.get("intent")
        if not isinstance(query, str) or not query.strip() or not isinstance(intent, str):
            continue
        confounds = [str(c) for c in a.get("confounds", []) if isinstance(c, (str, int))]
        angles.append({"query": query.strip(), "intent": intent,
                       "confounds": confounds,
                       "candidates": _clean_candidates(a.get("candidates"))})
    if not angles:
        return None
    facets = v["facets"] if isinstance(v.get("facets"), list) else []
    return {"facets": facets, "angles": angles}


def parse_labels(text: str) -> dict | None:
    """The labeler's graded pool, or None. ``grades`` keeps only well-formed
    thread->0|1|2 entries (a lost grade beats an invented one); ``reasons`` keeps
    only string values. Gold is derived, not trusted from the agent: the grade-2
    threads, in a stable order."""
    v = _extract_json(text)
    if v is None or not isinstance(v.get("grades"), dict):
        return None
    grades: dict[str, int] = {}
    for tid, g in v["grades"].items():
        try:
            g = int(g)
        except (TypeError, ValueError):
            continue
        if g in (0, 1, 2):
            grades[str(tid)] = g
    reasons = {str(t): str(r) for t, r in v.get("reasons", {}).items()
               if isinstance(r, str)} if isinstance(v.get("reasons"), dict) else {}
    gold = sorted(t for t, g in grades.items() if g == 2)
    return {"grades": grades, "gold": gold, "reasons": reasons}


def slugify(name: str) -> str:
    """A filesystem-safe slug for the output filename (lowercase, non-alphanumerics
    to hyphens, collapsed)."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "topic"


def resolve_topic(ref: str) -> dict:
    """Resolve a topic by id or (case-insensitive) name/title to its ``topic_get``
    detail. Exact name/title match wins; else a unique substring match; a missing
    or ambiguous name raises ``SystemExit`` naming the candidates."""
    from sqlalchemy import select

    from thread_archive._store import Thread, use_session

    with use_session() as s:
        t = s.get(Thread, ref)
        if t is not None and t.thread_type == "topic":
            return topic_get(t.id, session=s)
        rows = s.execute(
            select(Thread.id, Thread.title, Thread.name)
            .where(Thread.thread_type == "topic", Thread.archived.is_(False))
        ).all()
    needle = ref.strip().lower()
    def label(r) -> str:
        return (r.title or r.name or "").strip()
    exact = [r for r in rows if label(r).lower() == needle]
    hits = exact or [r for r in rows if needle in label(r).lower()]
    if not hits:
        raise SystemExit(f"no live topic matches {ref!r}")
    if len(hits) > 1:
        names = ", ".join(f"{label(r)!r} ({r.id})" for r in hits[:10])
        raise SystemExit(f"{ref!r} matches {len(hits)} topics — be specific: {names}")
    with use_session() as s:
        return topic_get(hits[0].id, session=s)


def build_survey_prompt(topic: dict, tool_cmd: str) -> str:
    """The survey agent's brief, seeded with the topic and a capped, highest-cited
    slice of its members (it has search/read to reach the rest). The agent decides
    how many angles the topic warrants — no target count is passed."""
    members = topic.get("member_threads", [])
    shown = members[:SURVEY_MEMBER_SAMPLE]
    member_lines = "\n".join(
        f"  {m['thread_id']}  {(m.get('title') or '(untitled)')[:120]}" for m in shown
    ) or "  (no cited members — explore via search)"
    desc = (topic.get("description") or "").strip()
    return _SURVEY_PROMPT.format(
        title=topic.get("title") or topic.get("id"),
        description=f"Description: {desc}" if desc else "(no description)",
        n_members=len(members), n_shown=len(shown), members=member_lines,
        tool=tool_cmd,
    )


def build_label_prompt(angle: dict, tool_cmd: str) -> str:
    """One labeler's brief — the query, its intent and confounds, and the survey's
    candidate threads for this angle as the starting pool (verify + expand, not
    blind rediscovery)."""
    cands = angle.get("candidates", [])[:LABEL_CANDIDATE_CAP]
    cand_lines = "\n".join(
        f"  {c['thread_id']}  {c.get('note', '')[:140]}".rstrip() for c in cands
    ) or "  (the survey recorded no candidates for this angle — discover them yourself)"
    return _LABEL_PROMPT.format(
        query=angle["query"], intent=angle["intent"],
        confounds=", ".join(angle["confounds"]) or "(none named)",
        candidates=cand_lines, tool=tool_cmd,
    )


# ── the agent seam ───────────────────────────────────────────────────────────

def run_survey(topic: dict, model: str, tool_cmd: str) -> tuple[dict | None, dict]:
    """Run the survey agent. Returns (survey | None, stats). The survey decides
    how many angles the topic warrants."""
    text, stats = mine_gold.run_claude(build_survey_prompt(topic, tool_cmd), model, tool_cmd)
    return (parse_survey(text) if text is not None else None), stats


def case_from_labels(angle: dict, labels: dict, snapshot_id: str) -> dict | None:
    """The eval ``--cases`` row for a labeled angle, or None when the label pool
    has no grade-2 gold (nothing to rank). Carries the graded pool for nDCG,
    ``sessions: []`` (topic queries are authored, not from a session), and the
    ``snapshot_id`` binding."""
    if not labels["gold"]:
        return None
    return {"query": angle["query"], "gold": labels["gold"], "grades": labels["grades"],
            "sessions": [], "snapshot_id": snapshot_id, "protocol": "topic-mined",
            "topic": angle.get("_topic"), "intent": angle["intent"],
            "mined_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def label_query(angle: dict, model: str, tool_cmd: str,
                snapshot_id: str) -> tuple[dict | None, dict]:
    """Label one angle end-to-end. Returns (case_row | None, detail_row). A case
    with no grade-2 gold is dropped (nothing to rank), but its detail is kept."""
    text, stats = mine_gold.run_claude(build_label_prompt(angle, tool_cmd), model, tool_cmd)
    detail = {"query": angle["query"], "intent": angle, "agent": stats}
    if text is None:
        detail["outcome"] = "agent-failed"
        return None, detail
    labels = parse_labels(text)
    if labels is None:
        detail["outcome"] = "unparseable"
        return None, detail
    detail.update({"outcome": "ok" if labels["gold"] else "no-gold",
                   "grades": labels["grades"], "reasons": labels["reasons"]})
    return case_from_labels(angle, labels, snapshot_id), detail


# ── entry ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--topic", required=True,
                    help="topic id, or a name/title (case-insensitive, unique)")
    ap.add_argument("--max-queries", type=int, default=MAX_ANGLES,
                    help=f"safety cap on angles labeled this run (default {MAX_ANGLES}); "
                    "the survey agent decides how many the topic warrants, below this")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--jobs", type=int, default=2, help="concurrent labeler agents")
    ap.add_argument("--cases-out", type=Path, default=None,
                    help="case file to append to (default: "
                    "~/.thread/archive/topic-cases-<slug>.jsonl); detail sidecar lands beside it")
    args = ap.parse_args()

    if shutil.which("claude") is None:
        raise SystemExit("topic_mine_gold needs the `claude` CLI on PATH")

    api.open_archive()
    snapshot_id = read_snapshot_id()
    if snapshot_id is None:
        raise SystemExit(
            "mining must run against a corpus snapshot, not the live archive: "
            "`thread_archive snapshot <dir>`, then point THREAD_ARCHIVE_HOME at it."
        )
    topic = resolve_topic(args.topic)
    slug = slugify(topic.get("title") or args.topic)
    cases_path = (args.cases_out.expanduser() if args.cases_out
                  else Path(DEFAULT_CASES_TEMPLATE.format(slug=slug)).expanduser())
    detail_path = cases_path.with_name(cases_path.stem + ".detail.jsonl")
    already = mine_gold.mined_queries(cases_path)

    tool_cmd = f"{sys.executable} {mine_gold.__file__} tool"
    print(f"surveying topic {topic.get('title')!r} ({topic['id']}, "
          f"{topic.get('citation_count', 0)} citations) against snapshot {snapshot_id}")
    survey, survey_stats = run_survey(topic, args.model, tool_cmd)
    if survey is None:
        raise SystemExit("survey agent produced no usable angles")

    angles = [a for a in survey["angles"] if a["query"] not in already]
    if len(angles) > args.max_queries:
        print(f"survey authored {len(angles)} angles; capping to --max-queries={args.max_queries}")
        angles = angles[:args.max_queries]
    for a in angles:
        a["_topic"] = topic.get("title") or slug
    if not angles:
        raise SystemExit("survey authored no unmined angles")

    cases_path.parent.mkdir(parents=True, exist_ok=True)
    with detail_path.open("a") as f:
        f.write(json.dumps({
            "topic": topic.get("title"), "topic_id": topic["id"],
            "snapshot_id": snapshot_id,
            "mined": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "method": _METHOD, "survey_agent": survey_stats,
            "facet_map": survey["facets"],
        }) + "\n")

    print(f"labeling {len(angles)} angles with {args.model} agents (jobs={args.jobs}) "
          f"-> {cases_path}")
    ok = failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futures = {ex.submit(label_query, a, args.model, tool_cmd, snapshot_id): a
                   for a in angles}
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
            print(f"  ✓ {row['query'][:60]!r}: {len(row['gold'])} gold, "
                  f"{len(row['grades'])} graded")
    print(f"done: {ok} cases written, {failed} failed (detail: {detail_path})")


if __name__ == "__main__":
    main()
