"""The mining agent's corpus access — production search + thread reads over the
frozen snapshot the run is pointed at.

Invoked as a subprocess by the mining agents (``python -m search_lab.mine
tool search|read ...``), never in-process, so the agent's Bash allowlist can be
pinned to exactly this command. Read-only. The snapshot is whatever
``THREAD_ARCHIVE_HOME`` names — the run exports it before spawning agents, and
the subprocess inherits it, so there is no per-invocation corpus bound to get
wrong.
"""

from __future__ import annotations

import argparse
import json

from thread_archive import _api as api
from thread_archive._retrieval.read import resolve_thread_ref
from thread_archive._store import use_session


def tool_search(args: argparse.Namespace) -> None:
    """Production search over the snapshot, minus the ``--skip`` sessions (the
    originating session quotes the query verbatim). Deep by default (limit 50):
    mining is recall-bound, so a shallow cap would bake the incumbent ranker's
    blind spots into the gold. Prints one JSON line per hit."""
    api.open_archive()
    skip = {s for s in (args.skip or "").split(",") if s and s != "-"}
    hits = api.search(args.query, limit=args.limit + len(skip))
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


def tool_read(args: argparse.Namespace) -> None:
    """Read one thread from the snapshot, resolving whatever ref shape the agent
    passes to a canonical id first."""
    api.open_archive()
    with use_session() as s:
        tid = resolve_thread_ref(s, str(args.thread_id).strip())
        if tid is None:
            raise SystemExit(f"unknown thread: {args.thread_id}")
    print(api.read_thread(tid, mode=args.mode, offset=args.offset,
                          max_chars=args.max_chars))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mine tool",
                                 description="mining agent corpus access (snapshot, read-only)")
    sub = ap.add_subparsers(dest="tool_cmd", required=True)

    ts = sub.add_parser("search")
    ts.add_argument("query")
    ts.add_argument("--skip", default="")
    ts.add_argument("--limit", type=int, default=50)

    tr = sub.add_parser("read")
    tr.add_argument("thread_id")
    tr.add_argument("--mode", default="ends",
                    choices=["ends", "chat", "user", "full", "last"])
    tr.add_argument("--offset", type=int, default=0)
    tr.add_argument("--max-chars", type=int, default=12000)

    args = ap.parse_args(argv)
    {"search": tool_search, "read": tool_read}[args.tool_cmd](args)
    return 0
