"""Stamp ``source_metadata.agent_type`` onto subagent threads imported before the
importer read it.

Claude Code names the *kind* of agent a subagent transcript belongs to in
``attributionAgent`` (``Explore``, ``general-purpose``, a custom agent name …) on
every assistant line. The importer read the transcript's ``agentId`` but not that,
so a subagent thread recorded which run it was and whose child it was, but never
what it was — the one field that makes a subagent thread identifiable as more than
an anonymous id. Fresh imports carry it now (``_importers.claude_code``'s
``_cc_origin_metadata``); this backfills the threads that predate that.

The value is read back from the on-disk transcripts, which are the only surviving
copy — the field is not preserved in any event payload, so a thread whose transcript
Claude Code has since rotated away is **unrecoverable and skipped**. Expect partial
coverage and a large skip count; that is the honest ceiling, not a failure. Both
source fields are constant across a transcript, so the first line carrying them
answers for the file and the scan stops there.

Writing is narrow by construction: only ``threads.source_metadata`` gains a key.
No event payload, no ``dedup_key``, no FTS or vector row is touched, so re-import
identity and the store's key-hash invariant are untouched. Durability is the
thread's re-staged truth record — latest-wins on reindex, the same seam
``set_thread_summary`` uses for thread metadata. A thread that already carries an
``agent_type`` is left alone, so the script is idempotent and a hand-corrected
value survives a re-run.

Preview (read-only): .venv/bin/python src/thread_archive/_scripts/backfill_subagent_type.py
Apply:               .venv/bin/python src/thread_archive/_scripts/backfill_subagent_type.py --apply
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Iterator

from sqlalchemy import select

from thread_archive._api import open_archive
from thread_archive._store import use_session
from thread_archive._store.models import Thread
from thread_archive._truth.jsonl_log import record_thread
from thread_archive._watcher.sources import discover_claude_dirs


def _agent_transcripts() -> Iterator[Path]:
    """Every subagent transcript still on disk, at any depth under ``subagents/``
    (workflow runs nest theirs further down). Matched on the ``agent-`` prefix
    the watcher uses, so a workflow's ``journal.jsonl`` run ledger is not read as
    a transcript."""
    for projects_dir in discover_claude_dirs():
        if not projects_dir.exists():
            continue
        for project_dir in projects_dir.iterdir():
            if project_dir.is_dir():
                yield from project_dir.glob("*/subagents/**/agent-*.jsonl")


def agent_types_on_disk() -> Dict[str, str]:
    """``agentId`` -> ``attributionAgent`` for every readable transcript that
    names both. Unparseable lines are skipped, not fatal: a torn final line is
    the normal state of a transcript being written right now."""
    found: Dict[str, str] = {}
    for path in _agent_transcripts():
        agent_id = agent_type = None
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "agentId" not in line and "attributionAgent" not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    agent_id = agent_id or rec.get("agentId")
                    agent_type = agent_type or rec.get("attributionAgent")
                    if agent_id and agent_type:
                        break
        except OSError:
            continue
        if agent_id and agent_type:
            found[agent_id] = agent_type
    return found


def main() -> int:
    apply = "--apply" in sys.argv

    on_disk = agent_types_on_disk()
    print(f"{len(on_disk)} agent ids name their type in a transcript still on disk")

    open_archive(None)
    with use_session() as session:
        threads = session.execute(
            select(Thread).where(Thread.source == "claude-code")
        ).scalars().all()

        planned, already, unrecoverable = [], 0, 0
        for t in threads:
            meta = t.source_metadata or {}
            if not meta.get("is_subagent"):
                continue
            if meta.get("agent_type"):
                already += 1
                continue
            agent_type = on_disk.get(meta.get("agent_id"))
            if agent_type is None:
                unrecoverable += 1
                continue
            planned.append((t, agent_type))

        print(f"  {already} already stamped")
        print(f"  {len(planned)} to stamp")
        print(f"  {unrecoverable} unrecoverable (transcript no longer on disk)")
        for t, agent_type in planned[:10]:
            print(f"    {t.id}: agent_type={agent_type!r}")
        if len(planned) > 10:
            print(f"    … and {len(planned) - 10} more")

        if not apply:
            print("\npreview only — rerun with --apply to write")
            return 0
        if not planned:
            print("\nnothing to do")
            return 0

        for t, agent_type in planned:
            # Rebind rather than mutate: source_metadata is a JSON column, and an
            # in-place dict edit is not seen as dirty by the ORM.
            t.source_metadata = {**(t.source_metadata or {}), "agent_type": agent_type}
            record_thread(session, t)  # latest-wins metadata in the thread's truth file
        session.commit()
        print(f"\nstamped agent_type on {len(planned)} threads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
