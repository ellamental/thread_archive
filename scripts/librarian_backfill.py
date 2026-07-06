#!/usr/bin/env python3
"""Bulk librarian backfill driver — drain the whole citation backlog.

A new archive starts with thousands of unreviewed conversations. This driver clears
them by spawning headless Claude Code instances that run the `/librarian` skill, in a
loop, until the queue is empty — the process-level "force the work" that complements the
per-thread `librarian-gate.py` hook (which forces each *spawned* instance to finish one
thread before the next).

It parallelizes by **lease-claims**: each spawned instance gets a distinct worker id
(`$THREAD_ARCHIVE_LIBRARIAN_WORKER`), and its `review_queue` claims the batch it works —
recording a timestamped lease so concurrent workers don't overlap, and reclaiming any
lease older than an hour (a dead worker's). Workers self-balance; there's no static
partition and no central queue assignment. SQLite WAL serializes the small writes; reads
run concurrently.

State is the data: a thread is 'done' once it gains a topic citation/link, so the driver's
loop simply asks whether any *unclaimed* unreviewed thread remains. A crashed/timed-out
instance loses nothing — its threads stay unreviewed, its lease lapses, and the next spawn
picks them up (citations are idempotent).

Run from the repo root, with the project venv (so `thread_archive` imports and the
spawned `claude` loads this repo's `.mcp.json` + `.claude/` skill and hook):

    .venv/bin/python scripts/librarian_backfill.py --workers 4 --batch 25

This is a host-level operator tool, deliberately outside the serverless package (it
shells out to `claude`); the package itself never spawns a model.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_claude() -> str:
    """Locate the claude CLI: PATH first, then the standard ~/.local/bin install."""
    found = shutil.which("claude")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "claude")


def _has_claimable_work(home: str | None) -> bool:
    """True if any unreviewed conversation is free to claim (not held by a live lease).
    Imported, not shelled — a cheap direct query against the same store + claim file the
    workers use."""
    import thread_archive as ta
    from thread_archive.knowledge._claims import has_claimable_work

    ta.open_archive(home)
    return has_claimable_work()


def _spawn(claude: str, batch: int, model: str, effort: str, env: dict, timeout: int) -> int:
    """Run one headless `/librarian <batch>` to completion (or timeout). Returns rc."""
    args = [
        claude, "--print",
        "--permission-mode", "bypassPermissions",
        "--model", model,
        "--effort", effort,
        f"/librarian {batch}",
    ]
    # Own session + group-kill on timeout: the instance spawns MCP subprocesses, so a
    # plain timeout would orphan that tree.
    proc = subprocess.Popen(
        args, cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        proc.communicate(timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                proc.wait(timeout=10)
                break
            except subprocess.TimeoutExpired:
                continue
        return -1  # timed out — the worker's lease lapses and the next spawn resumes it


def _worker(k: int, args, claude: str, base_env: dict, stats: dict) -> None:
    worker_id = f"w{k}-{os.getpid()}"
    env = {**base_env, "THREAD_ARCHIVE_LIBRARIAN_WORKER": worker_id}
    if args.home:
        env["THREAD_ARCHIVE_HOME"] = args.home
    spawns = 0
    while _has_claimable_work(args.home):
        if args.max_spawns and spawns >= args.max_spawns:
            print(f"[{worker_id}] hit --max-spawns={args.max_spawns}; stopping", flush=True)
            break
        spawns += 1
        print(f"[{worker_id}] spawn #{spawns} (/librarian {args.batch})", flush=True)
        rc = _spawn(claude, args.batch, args.model, args.effort, env, args.timeout)
        print(f"[{worker_id}] spawn #{spawns} exited rc={rc}", flush=True)
    stats[k] = spawns
    print(f"[{worker_id}] no claimable work left after {spawns} spawn(s)", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="librarian_backfill",
        description="Drain the librarian summary/citation backlog with headless Claude instances.",
    )
    parser.add_argument("--workers", type=int, default=1, help="parallel sharded instances (default 1)")
    parser.add_argument("--batch", type=int, default=25, help="threads per spawned /librarian run")
    parser.add_argument("--timeout", type=int, default=1800, help="per-spawn wall-clock seconds")
    parser.add_argument("--model", default="claude-opus-4-8", help="model for the librarian (opus per the exception)")
    parser.add_argument("--effort", default="xhigh", help="reasoning effort for the headless run")
    parser.add_argument("--max-spawns", type=int, default=0, help="cap spawns per worker (0 = until drained)")
    parser.add_argument("--home", default=None, help="archive home (default $THREAD_ARCHIVE_HOME or ~/.thread/archive)")
    args = parser.parse_args(argv)

    claude = _resolve_claude()
    if not Path(claude).exists() and shutil.which("claude") is None:
        print(f"error: claude CLI not found ({claude}). Install it first.", file=sys.stderr)
        return 1

    # Strip ANTHROPIC_API_KEY so spawned instances use the subscription OAuth tokens.
    base_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    print(f"librarian backfill: {args.workers} worker(s), batch={args.batch}, "
          f"model={args.model}, timeout={args.timeout}s", flush=True)

    stats: dict[int, int] = {}
    threads = [
        threading.Thread(target=_worker, args=(k, args, claude, base_env, stats), daemon=True)
        for k in range(args.workers)
    ]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\ninterrupted — workers will finish their current spawn and exit", flush=True)
        return 130

    total = sum(stats.values())
    print(f"backfill complete: {total} spawn(s) across {args.workers} worker(s); queue drained", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
