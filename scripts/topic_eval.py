"""Knowledge-graph usage metrics, mined from the trail — does the graph serve work?

The graph's delivery path into working sessions is the **subjects lens**
(:mod:`thread_archive._retrieval.subjects`): every ``thread_search`` result
set is annotated with the subjects its hits cluster under, each openable via
``thread_read(topic_id)``. It annotates, never ranks — so its value shows up
not in MRR but in whether agents *take the pivot*. That is the headline
metric here: **subject uptake** — of the ``thread_search`` calls in the
trail, how often does a topic read follow before the agent searches again?
Topic reads with no prior search in the session ("cold" reads — the tree,
a remembered topic) are counted alongside, split by searcher (``system``
curation/subagent sessions versus ``conversation`` working sessions). A
lens nobody pivots through is a terrarium, however well curated; uptake is
the number that says which it is.

Secondary, and strictly **curation ergonomics** (not KG value to work): the
librarian's ``topic_search`` dedup tool gets the click protocol —
per search: ``found`` (a topic-ref tool acted on a topic it surfaced),
``created`` (``topic_create`` before any open — the searcher concluded no
topic fit; with hundreds of singleton topics in the graph, some of those are
duplicate births the substring match failed to prevent), or ``nothing`` —
plus re-findability of the acted-on topics under today's title search.

Read-only. Run against the live archive:

    .venv/bin/python scripts/topic_eval.py
    .venv/bin/python scripts/topic_eval.py --json --trend-out \
        ~/.thread/archive/retrieval-trend.jsonl
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_eval", Path(__file__).resolve().parent / "retrieval_eval.py")
retrieval_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("retrieval_eval", retrieval_eval)
_SPEC.loader.exec_module(retrieval_eval)

from sqlalchemy import text as sa_text  # noqa: E402

from thread_archive import _api as api  # noqa: E402
from thread_archive._knowledge import write as kg_write  # noqa: E402
from thread_archive._retrieval.read import resolve_thread_ref  # noqa: E402
from thread_archive._store import use_session  # noqa: E402

RECALL_KS = (1, 5, 10, 20)

# Tool families whose topic ids live in this archive's id space: the librarian
# MCP server (standalone and plugin-hosted names both end in
# `archive-librarian__topic_*`) and the legacy thread-commands server. Bare
# unnamespaced `topic_*` names belong to other systems — excluded.
_TOOL_RE = re.compile(
    r"(?:archive[-_]librarian|thread[-_]commands)[_:]+topic_([a-z_]+)$")

# Input fields that carry a topic ref, across the librarian tools and their
# legacy names: topic_id (get/members/cite/rename/archive/update/read),
# from_id/into_id (merge), source_id/target_id (link/unlink), id (legacy).
_REF_FIELDS = ("topic_id", "from_id", "into_id", "source_id", "target_id", "id")

# topic_* verbs that act on an existing topic — the click set. `search` and
# `create` classify separately; anything else (unknown/future verbs without a
# ref field) is ignored rather than guessed at.
_OPEN_VERBS = {
    "get", "members", "read", "cite", "uncite", "link", "unlink", "merge",
    "rename", "archive", "update", "timeline",
}


def subject_uptake(events: list[tuple[object, str, object]],
                   thread_types: dict[object, str]) -> dict:
    """Subject-lens uptake from the archive-tool trail — pure.

    ``events``: (session, 'search'|'read', value) as
    ``retrieval_eval._trail_events`` produces (read refs already canonical).
    ``thread_types``: thread id → thread_type; a read whose target maps to
    ``'topic'`` is a topic read. Per search, uptake means a topic read follows
    in the same session before the next search. Topic reads with no prior
    search in the session count as ``cold`` (tree navigation, a remembered
    topic) — engagement, but not the lens's doing.
    """
    per_session: dict[object, list[tuple[str, object]]] = {}
    for sess, kind, value in events:
        per_session.setdefault(sess, []).append((kind, value))

    n_searches = 0
    searches_with_topic_read = 0
    topic_reads_after_search = 0
    topic_reads_cold = 0
    sessions_with_uptake: set[object] = set()
    for sess, evs in per_session.items():
        pending: bool | None = None  # open search's "topic read yet?" flag
        searched = False
        for kind, value in evs:
            if kind == "search":
                n_searches += 1
                searched = True
                pending = False
            elif thread_types.get(value) == "topic":
                if searched:
                    topic_reads_after_search += 1
                    sessions_with_uptake.add(sess)
                    if pending is False:
                        searches_with_topic_read += 1
                        pending = True
                else:
                    topic_reads_cold += 1
    return {
        "n_searches": n_searches,
        "searches_with_topic_read": searches_with_topic_read,
        "uptake_rate": (searches_with_topic_read / n_searches
                        if n_searches else 0.0),
        "topic_reads_after_search": topic_reads_after_search,
        "topic_reads_cold": topic_reads_cold,
        "sessions_with_uptake": len(sessions_with_uptake),
    }


def classify_tool(name: str | None) -> str | None:
    """'search' / 'create' / 'open' for a family topic tool name, else None."""
    m = _TOOL_RE.search(name or "")
    if not m:
        return None
    verb = m.group(1)
    if verb in ("search", "create"):
        return verb
    return "open" if verb in _OPEN_VERBS else None


def extract_refs(inp: dict) -> list[object]:
    """Topic refs carried by one tool call's input, in field order."""
    refs = []
    for field in _REF_FIELDS:
        v = inp.get(field)
        if isinstance(v, (int, str)) and str(v).strip():
            refs.append(v)
    return refs


def pair_topic_events(events: list[tuple[object, str, object]]) -> list[dict]:
    """Per-search outcomes from one trail of (session, kind, value) events.

    Kinds: 'search' (value: query), 'open' (value: canonical topic id),
    'create' (value: ignored). Attribution mirrors the thread protocol: an
    open labels the most recent prior search in its session; topics the agent
    had already touched before the search don't count. A 'create' before any
    open marks the search 'created' (the searcher concluded no topic fit).

    Returns one row per search: {query, session, outcome, gold} where outcome
    is 'found' (gold: sorted opened topic ids), 'created', or 'nothing'.
    """
    per_session: dict[object, list[tuple[str, object]]] = {}
    for sess, kind, value in events:
        per_session.setdefault(sess, []).append((kind, value))

    rows: list[dict] = []
    for sess, evs in per_session.items():
        seen: set[object] = set()
        current: dict | None = None

        def flush() -> None:
            if current is None:
                return
            if current["gold"]:
                current["outcome"] = "found"
                current["gold"] = sorted(current["gold"])
            else:
                current["gold"] = []
            rows.append(current)

        for kind, value in evs:
            if kind == "search":
                flush()
                current = {"query": value, "session": sess,
                           "outcome": "nothing", "gold": set()}
            elif kind == "create":
                if current is not None and not current["gold"]:
                    current["outcome"] = "created"
            else:  # open
                if (current is not None and value not in seen
                        and current["outcome"] != "created"):
                    current["gold"].add(value)
                seen.add(value)
        flush()
    return rows


def mine(after: str | None = None) -> tuple[list[dict], dict[object, str]]:
    """All per-search outcome rows from the trail, plus {session: thread_type}.

    Golds are canonicalized through the production resolver and filtered to
    live (unarchived) topics — a click on a topic that has since been merged
    away or archived can't be expected to rank.
    """
    with use_session() as s:
        sql = (
            "SELECT thread_id, payload FROM events "
            "WHERE event_type = 'tool_use_complete' AND payload LIKE '%topic_%' "
        )
        params: dict[str, str] = {}
        if after:
            sql += "AND occurred_at >= :after "
            params["after"] = after
        sql += "ORDER BY thread_id, id"
        db_rows = s.execute(sa_text(sql), params).all()

        events: list[tuple[object, str, object]] = []
        for sess, payload in db_rows:
            p = payload if isinstance(payload, dict) else json.loads(payload)
            kind = classify_tool(p.get("tool_name"))
            if kind is None:
                continue
            inp = p.get("input") or {}
            if kind == "search":
                q = inp.get("query")
                if isinstance(q, str) and q.strip():
                    events.append((sess, "search", q.strip()))
            elif kind == "create":
                events.append((sess, "create", None))
            else:
                for ref in extract_refs(inp):
                    events.append((sess, "open", ref))

        memo: dict[str, str | None] = {}

        def _resolve(ref: object) -> str | None:
            key = str(ref).strip()
            if key not in memo:
                memo[key] = resolve_thread_ref(s, key)
            return memo[key]

        resolved: list[tuple[object, str, object]] = []
        for sess, kind, value in events:
            if kind == "open":
                value = _resolve(value)
                if value is None:
                    continue
            resolved.append((sess, kind, value))

        rows = pair_topic_events(resolved)

        live_topics = {
            tid for (tid,) in s.execute(sa_text(
                "SELECT id FROM threads WHERE thread_type = 'topic' "
                "AND (archived IS NULL OR NOT archived)")).all()
        }
        for r in rows:
            if r["outcome"] == "found":
                r["gold"] = [t for t in r["gold"] if t in live_topics]
                if not r["gold"]:
                    r["outcome"] = "nothing"

        sessions = {r["session"] for r in rows}
        kinds: dict[object, str] = {}
        for tid, ttype in s.execute(
                sa_text("SELECT id, thread_type FROM threads")).all():
            if tid in sessions:
                kinds[tid] = ttype or "conversation"
    return rows, kinds


def evaluate(cases: list[dict], searcher, *, limit: int = 20) -> dict:
    """Score found-cases: does today's topic_search rank the clicked topic?"""
    reciprocal_ranks: list[float] = []
    hits_at = {k: 0 for k in RECALL_KS}
    latencies: list[float] = []
    for case in cases:
        gold = set(case["gold"])
        t0 = time.monotonic()
        results = searcher(case["query"], limit)
        latencies.append(time.monotonic() - t0)
        rank = 0
        for pos, tid in enumerate(results, start=1):
            if tid in gold:
                rank = pos
                break
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        for k in RECALL_KS:
            if rank and rank <= k:
                hits_at[k] += 1
    n = len(cases)
    return {
        "n": n,
        "mrr": sum(reciprocal_ranks) / n if n else 0.0,
        "recall": {k: hits_at[k] / n if n else 0.0 for k in RECALL_KS},
        "latency_p50_ms": sorted(latencies)[n // 2] * 1000 if n else 0.0,
    }


def production_searcher(query: str, limit: int) -> list[str]:
    """The live topic_search, as the librarian MCP serves it."""
    return [r["topic_id"] for r in kg_write.topic_search(query, limit=limit)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=20,
                    help="results per query (recall ceiling; the MCP tool's "
                    "own default is 10)")
    ap.add_argument("--mined-after", metavar="ISO", default=None)
    ap.add_argument("--trend-out", type=Path, default=None,
                    help="append the report as one JSONL row (~ ok)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    api.open_archive()

    # Headline: subject-lens uptake from the archive-tool trail.
    with use_session() as s:
        thread_events = retrieval_eval._trail_events(s, args.mined_after)
        all_types = {
            tid: (ttype or "conversation") for tid, ttype in s.execute(
                sa_text("SELECT id, thread_type FROM threads")).all()
        }
    report: dict = {"protocol": "kg-usage", "mined_after": args.mined_after}
    report["subject_uptake"] = subject_uptake(thread_events, all_types)
    for seg_name in ("conversation", "system"):
        seg_events = [
            e for e in thread_events
            if (all_types.get(e[0]) == "system") == (seg_name == "system")
        ]
        report[f"subject_uptake_{seg_name}"] = subject_uptake(
            seg_events, all_types)

    # Secondary: the librarian dedup tool's click protocol.
    rows, session_kinds = mine(args.mined_after)

    def segment(sess: object) -> str:
        return ("system" if session_kinds.get(sess) == "system"
                else "conversation")
    outcomes = {"found": 0, "created": 0, "nothing": 0}
    by_segment: dict[str, dict] = {}
    for r in rows:
        outcomes[r["outcome"]] += 1
        seg = by_segment.setdefault(
            segment(r["session"]), {"found": 0, "created": 0, "nothing": 0})
        seg[r["outcome"]] += 1
    report["searches"] = len(rows)
    report["outcomes"] = outcomes
    report["by_searcher"] = by_segment

    found = [r for r in rows if r["outcome"] == "found"]
    report["ranking"] = evaluate(found, production_searcher, limit=args.limit)
    report["ranking"]["recall"] = {
        str(k): v for k, v in report["ranking"]["recall"].items()}
    for seg_name in sorted(by_segment):
        seg_cases = [r for r in found if segment(r["session"]) == seg_name]
        if seg_cases:
            seg_report = evaluate(seg_cases, production_searcher,
                                  limit=args.limit)
            seg_report["recall"] = {
                str(k): v for k, v in seg_report["recall"].items()}
            report[f"ranking_{seg_name}"] = seg_report

    if args.trend_out:
        from datetime import datetime, timezone

        path = args.trend_out.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **report}) + "\n")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        u = report["subject_uptake"]
        print(f"subject uptake: {u['searches_with_topic_read']}/{u['n_searches']} "
              f"searches followed by a topic read "
              f"(rate {u['uptake_rate']:.4f}; "
              f"{u['topic_reads_after_search']} reads after search, "
              f"{u['topic_reads_cold']} cold, "
              f"{u['sessions_with_uptake']} sessions)")
        for seg_name in ("conversation", "system"):
            su = report[f"subject_uptake_{seg_name}"]
            print(f"  {seg_name:>13}: {su['searches_with_topic_read']}"
                  f"/{su['n_searches']} (rate {su['uptake_rate']:.4f})")
        n = report["searches"]
        print(f"librarian dedup topic_search: {n}   found: {outcomes['found']}   "
              f"created instead: {outcomes['created']}   "
              f"nothing: {outcomes['nothing']}")
        for seg_name, seg in sorted(by_segment.items()):
            total = sum(seg.values())
            print(f"  {seg_name:>13}: n={total:<5} "
                  + "  ".join(f"{k}={v}" for k, v in seg.items()))
        r = report["ranking"]
        print(f"re-findability (n={r['n']}): MRR {r['mrr']:.3f}   "
              + "   ".join(f"R@{k}: {v:.3f}" for k, v in r["recall"].items())
              + f"   p50 {r['latency_p50_ms']:.0f} ms")


if __name__ == "__main__":
    main()
