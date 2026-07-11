"""Mine golden retrieval-eval cases from the archive's own usage.

Every ``thread_search`` call and the ``thread_read`` that follows it are archived
events, so searches-that-led-to-reads are implicit relevance judgments — real
agent queries paired with the thread the agent actually chose to open. This
script walks each session's tool trail, pairs a search's query with a
subsequently-read thread, and keeps the pair only when that thread id appears in
the search's own result text (i.e. the read was a *click on a result*, not an
independent lookup).

Output is golden-set JSONL for ``retrieval_eval.py --golden``. A query the agent
followed into several threads emits one case with every clicked thread relevant
(finding any of them is a hit):

    {"query": "...", "thread_ids": [N, ...]}

Read-only. The golden set contains real (sometimes personal) queries, so it lives
in the archive home — data, not repo:

    .venv/bin/python scripts/golden_from_usage.py --out ~/.thread/archive/golden-queries.jsonl
    .venv/bin/python scripts/retrieval_eval.py --golden ~/.thread/archive/golden-queries.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text as sa_text  # noqa: E402

from thread_archive import api  # noqa: E402
from thread_archive._store import use_session  # noqa: E402

# The archive search/read tools as they appear across MCP configs and history:
# mcp__thread-archive__thread_search, thread_search, thread-search,
# thread-commands:thread_search, … — match on the suffix, tolerate any prefix.
_SEARCH_NAME = re.compile(r"thread[_-]search$")
_READ_NAME = re.compile(r"thread[_-]read$")

# How many events after a search a read still counts as a click on it.
_CLICK_WINDOW_EVENTS = 60


def _tool_rows():
    """Every search/read tool-use and search tool-result row, session-ordered:
    ``(event_id, session_thread_id, content_type, tool_name, result_content,
    input_dict)``."""
    sql = sa_text(
        "SELECT f.event_id, f.thread_id, f.content_type, f.tool_name, f.content, e.payload "
        "FROM events_fts f JOIN events e ON e.id = f.event_id "
        "WHERE f.content_type IN ('tool', 'tool_result') "
        "AND (f.tool_name LIKE '%thread%search%' OR f.tool_name LIKE '%thread%read%') "
        "ORDER BY f.thread_id, f.event_id"
    )
    with use_session() as s:
        for eid, tid, ct, tool, content, payload in s.execute(sql):
            p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
            yield eid, tid, ct, tool or "", content or "", p.get("input") or {}


def _valid_targets() -> set[int]:
    """Searchable conversation threads a golden case may point at."""
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT id FROM threads "
            "WHERE thread_type = 'conversation' AND NOT exclude_from_search"
        )).all()
    return {r[0] for r in rows}


def _source_id_map() -> dict[str, int]:
    """provider session uuid → thread id (newest wins, matching read's resolver)."""
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT source_id, id FROM threads WHERE source_id IS NOT NULL ORDER BY id"
        )).all()
    return {sid: tid for sid, tid in rows}


def _target_thread(value, by_source: dict[str, int]) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        if v.isdigit():
            return int(v)
        return by_source.get(v)
    return None


def mine(min_query_terms: int) -> list[dict]:
    by_source = _source_id_map()
    valid = _valid_targets()
    cases: list[dict] = []
    seen: set[tuple[str, int]] = set()

    session = None
    # Recent searches in the current session: (event_id, query, [result texts]).
    recent: list[list] = []

    for eid, tid, ct, tool, content, tool_input in _tool_rows():
        if tid != session:
            session, recent = tid, []

        if _SEARCH_NAME.search(tool):
            if ct == "tool":
                query = tool_input.get("query")
                if isinstance(query, str) and query.strip() and "startswith" not in tool_input:
                    recent.append([eid, query.strip(), []])
                    recent[:] = recent[-8:]
            elif ct == "tool_result" and recent:
                # A search's output lands after its call; attach to the newest
                # search still missing a result, else the newest outright.
                target = next((r for r in reversed(recent) if not r[2]), recent[-1])
                target[2].append(content)
            continue

        if _READ_NAME.search(tool) and ct == "tool":
            read_tid = _target_thread(tool_input.get("thread_id"), by_source)
            if read_tid is None or read_tid == tid or read_tid not in valid:
                continue
            id_re = re.compile(r"\b" + str(read_tid) + r"\b")
            for s_eid, query, results in reversed(recent):
                if eid - s_eid > _CLICK_WINDOW_EVENTS:
                    break
                if not any(id_re.search(r) for r in results):
                    continue
                if len(query.split()) < min_query_terms:
                    break
                key = (query.lower(), read_tid)
                if key not in seen:
                    seen.add(key)
                    cases.append({"query": query, "thread_id": read_tid})
                break  # credit the nearest qualifying search only

    # One case per query, every clicked thread relevant — separate rows would
    # score each click's siblings as misses.
    by_query: dict[str, dict] = {}
    for c in cases:
        entry = by_query.setdefault(c["query"].lower(), {"query": c["query"], "thread_ids": []})
        entry["thread_ids"].append(c["thread_id"])
    return list(by_query.values())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write JSONL here (default: stdout)")
    ap.add_argument("--min-terms", type=int, default=2,
                    help="drop queries with fewer terms (default 2 — single tokens "
                         "are usually id lookups, not retrieval)")
    args = ap.parse_args()

    api.open_archive()
    cases = mine(args.min_terms)

    lines = ["# golden retrieval cases mined from archived search→read behavior",
             "# regenerate: .venv/bin/python scripts/golden_from_usage.py"]
    lines += [json.dumps(c, ensure_ascii=False) for c in cases]
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {len(cases)} cases to {args.out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
