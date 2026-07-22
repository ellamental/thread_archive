"""Topic-mined gold labels — a curated topic's confounds turned into graded cases.

The query miner (``retrieval_mine_gold.py``) starts from queries agents *really
ran*; this one starts from a curated **topic** — a subject dense with near-misses
(a large, confound-rich topic like "suicide": lived experience vs. bot-death
grief vs. AI right-to-die vs. self-deprecation, all sharing vocabulary). A topic
that hard is where ranking earns or loses its keep, and the trail rarely supplies
enough real queries against it. So two agents manufacture the benchmark:

1. **Survey** (one agent): reads the topic's members, maps its facets, and
   authors queries — each tagged with the single facet it *intends* and the
   confound facets a lazy ranker would drag in instead. The queries carry
   intent, not answers.
2. **Label** (one agent per query, independent and **blind** to the survey
   agent's thread ids): sweeps the frozen corpus with its own searches, reads
   candidates, and grades a pool — 2 = answers the intended facet, 1 = partial /
   related, 0 = a confound. Gold is the grade-2 set. Blindness is the point: the
   survey agent's picks never leak into the labels, so a case isn't just "did
   search find what agent A already had in mind."

Snapshot binding, same as the query miner: run against a frozen corpus snapshot
(``thread_archive snapshot``; point ``THREAD_ARCHIVE_HOME`` at it), and the
topic must exist in it. Each case records the snapshot's ``snapshot_id``;
``retrieval_eval.py --cases`` scores over that same snapshot and refuses cases
once the id no longer matches (the corpus moved; re-mine). Output is the eval's
``--cases`` format with a graded pool for nDCG, plus a detail sidecar carrying
the facet map and per-query intent/reasons.

Costs real tokens (one survey agent + one labeler per query — minutes each) and
requires the ``claude`` CLI. Read-only against the archive. Case files quote
real usage — keep them out of the repo; they live beside the trend ledgers in
``~/.thread/archive/``.

    thread_archive snapshot ~/.thread/archive-snap
    export THREAD_ARCHIVE_HOME=~/.thread/archive-snap
    .venv/bin/python evals/topic_mine_gold.py --topic suicide --sample 8
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
# highest-cited first; it has search/read to reach the rest. Enough to ground a
# facet map without pasting a thousand-member topic into the prompt.
SURVEY_MEMBER_SAMPLE = 80

DEFAULT_CASES_TEMPLATE = "~/.thread/archive/topic-cases-{slug}.jsonl"

_METHOD = (
    "topic-driven: one survey agent mapped corpus facets and authored queries "
    "from intent; one independent labeler agent per query swept the corpus and "
    "graded a candidate pool (2=intended, 1=partial, 0=confound). Labelers were "
    "not shown the survey agent's thread ids."
)

_SURVEY_PROMPT = """You are designing a search-quality benchmark from one curated \
topic in a conversation archive.

Topic: {title}
{description}

This topic has {n_members} member conversations (threads cited to it). Here are \
the {n_shown} most-cited, as `thread_id  title`:

{members}

You can explore the whole frozen corpus (not just this topic) to understand the \
subject and its look-alikes (run via Bash; read-only):

  {tool} search "<query>" [--limit N] [--rerank on|off|auto]
  {tool} read <thread_id> [--mode ends|chat|user|full|last] [--offset N] [--max-chars N]

Your job, in two parts:

1. Map the topic's FACETS — the distinct sub-subjects it contains, and the \
neighboring subjects that share its vocabulary but are NOT it (its confounds). \
Read enough members and search widely enough to find the confounds; the whole \
point of this topic is that near-misses look relevant.

2. Author QUERIES a real user might type. For each, name the ONE facet it \
intends and the confound facets a careless ranker would surface instead. Write \
queries that genuinely discriminate — a query whose intended facet and confounds \
are easy to tell apart teaches nothing. Aim for {n_queries} queries spread \
across the facets.

Reply with ONLY this JSON object as your final message (no prose around it):

{{"facets": [{{"facet": "<short name>", "rough_volume": "small|medium|large", \
"example_thread_ids": ["<id>", ...]}}, ...], \
"queries": [{{"query": "<what a user types>", "intended_facet": "<facet name>", \
"confounds": ["<facet name>", ...]}}, ...]}}"""

_LABEL_PROMPT = """You are grading search results for one benchmark query against \
a frozen conversation archive.

Query: {query}
Intended meaning: {intended_facet}
Nearby subjects that share this query's vocabulary but do NOT answer it \
(confounds — grade these 0 even though they look relevant): {confounds}

Explore the corpus yourself and decide, for each thread you inspect, whether it \
answers the INTENDED meaning (run via Bash; read-only):

  {tool} search "<query>" [--limit N] [--rerank on|off|auto]
  {tool} read <thread_id> [--mode ends|chat|user|full|last] [--offset N] [--max-chars N]

Method:
1. Reformulate widely — synonyms, the intended facet's own vocabulary, the \
confounds' vocabulary. Build a candidate pool from what the searches surface.
2. Read the strongest candidates (--mode ends is a cheap first read) to judge \
what they actually contain, not just matching words.
3. Grade every thread you inspected: 2 = answers the intended meaning, \
1 = partial or related-but-not-answering, 0 = a confound or irrelevant. Include \
the confounds you found at grade 0 — a benchmark needs the hard negatives, not \
only the positives.

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


def parse_survey(text: str) -> dict | None:
    """The survey agent's facet map + queries, or None. Keeps only well-formed
    queries (a string ``query`` and ``intended_facet``); a query with neither is
    dropped rather than mined blind. ``confounds`` normalized to a list of
    strings; ``facets`` passed through as authored (detail-only)."""
    v = _extract_json(text)
    if v is None or not isinstance(v.get("queries"), list):
        return None
    queries = []
    for q in v["queries"]:
        if not isinstance(q, dict):
            continue
        query = q.get("query")
        facet = q.get("intended_facet")
        if not isinstance(query, str) or not query.strip() or not isinstance(facet, str):
            continue
        confounds = [str(c) for c in q.get("confounds", []) if isinstance(c, (str, int))]
        queries.append({"query": query.strip(), "intended_facet": facet,
                        "confounds": confounds})
    if not queries:
        return None
    facets = v["facets"] if isinstance(v.get("facets"), list) else []
    return {"facets": facets, "queries": queries}


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


def build_survey_prompt(topic: dict, tool_cmd: str, n_queries: int) -> str:
    """The survey agent's brief, seeded with the topic and a capped, highest-cited
    slice of its members (it has search/read to reach the rest)."""
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
        tool=tool_cmd, n_queries=n_queries,
    )


def build_label_prompt(query: dict, tool_cmd: str) -> str:
    """One labeler's brief — the query and its intent only; never the survey
    agent's gold ids (the labeler rediscovers blind)."""
    return _LABEL_PROMPT.format(
        query=query["query"], intended_facet=query["intended_facet"],
        confounds=", ".join(query["confounds"]) or "(none named)", tool=tool_cmd,
    )


# ── the agent seam ───────────────────────────────────────────────────────────

def run_survey(topic: dict, model: str, tool_cmd: str, n_queries: int
               ) -> tuple[dict | None, dict]:
    """Run the survey agent. Returns (survey | None, stats)."""
    text, stats = mine_gold.run_claude(
        build_survey_prompt(topic, tool_cmd, n_queries), model, tool_cmd)
    return (parse_survey(text) if text is not None else None), stats


def case_from_labels(query: dict, labels: dict, snapshot_id: str) -> dict | None:
    """The eval ``--cases`` row for a labeled query, or None when the label pool
    has no grade-2 gold (nothing to rank). Carries the graded pool for nDCG,
    ``sessions: []`` (topic queries are authored, not from a session), and the
    ``snapshot_id`` binding."""
    if not labels["gold"]:
        return None
    return {"query": query["query"], "gold": labels["gold"], "grades": labels["grades"],
            "sessions": [], "snapshot_id": snapshot_id, "protocol": "topic-mined",
            "topic": query.get("_topic"), "intended_facet": query["intended_facet"],
            "mined_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def label_query(query: dict, model: str, tool_cmd: str,
                snapshot_id: str) -> tuple[dict | None, dict]:
    """Label one query end-to-end. Returns (case_row | None, detail_row). A case
    with no grade-2 gold is dropped (nothing to rank), but its detail is kept."""
    text, stats = mine_gold.run_claude(build_label_prompt(query, tool_cmd), model, tool_cmd)
    detail = {"query": query["query"], "intent": query, "agent": stats}
    if text is None:
        detail["outcome"] = "agent-failed"
        return None, detail
    labels = parse_labels(text)
    if labels is None:
        detail["outcome"] = "unparseable"
        return None, detail
    detail.update({"outcome": "ok" if labels["gold"] else "no-gold",
                   "grades": labels["grades"], "reasons": labels["reasons"]})
    return case_from_labels(query, labels, snapshot_id), detail


# ── entry ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--topic", required=True,
                    help="topic id, or a name/title (case-insensitive, unique)")
    ap.add_argument("--sample", type=int, default=8,
                    help="max queries to author + label this run (one agent each)")
    ap.add_argument("--seed", type=int, default=7)  # accepted for parity; survey is the sampler
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
    survey, survey_stats = run_survey(topic, args.model, tool_cmd, args.sample)
    if survey is None:
        raise SystemExit("survey agent produced no usable queries")

    queries = [q for q in survey["queries"] if q["query"] not in already][:args.sample]
    for q in queries:
        q["_topic"] = topic.get("title") or slug
    if not queries:
        raise SystemExit("survey authored no unmined queries")

    cases_path.parent.mkdir(parents=True, exist_ok=True)
    with detail_path.open("a") as f:
        f.write(json.dumps({
            "topic": topic.get("title"), "topic_id": topic["id"],
            "snapshot_id": snapshot_id,
            "mined": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "method": _METHOD, "survey_agent": survey_stats,
            "facet_map": survey["facets"],
        }) + "\n")

    print(f"labeling {len(queries)} queries with {args.model} agents (jobs={args.jobs}) "
          f"-> {cases_path}")
    ok = failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futures = {ex.submit(label_query, q, args.model, tool_cmd, snapshot_id): q
                   for q in queries}
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
