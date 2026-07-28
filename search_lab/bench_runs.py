"""The benchmark run ledger: ``~/.local/state/thread-search-lab/bench-runs.jsonl``.

``python -m search_lab benchmark`` runs the bench as a set, and this is where a
run lands: one row per benchmark, carrying what ran, over which corpus, against
which retrieval code, what it measured, and how long it took. Each harness's own
``--json-out`` report keeps the per-query detail; this one is the *run level*, so
"what did the bench say at this configuration" is a lookup rather than a re-run.

It also decides what a re-run has to do. During a tuning loop the same set is run
repeatedly with one knob moved between passes, and re-scoring rows that cannot
have changed is the difference between a set you run every iteration and one you
run at the end of the week. Each row records a ``code_id`` and a ``corpus_id``,
and a row whose pair still matches is **fresh** — already measured, skipped
instantly, its recorded numbers reported as if it had just run.

``code_id`` is a content hash of the ranking code as it sits in the working tree,
not the commit: a tuning loop edits ``SearchParams`` defaults and does not commit
between passes, so a commit-keyed cache would skip every row after the first edit
and report the old numbers. The commit is recorded too, but only as the human
label for a row.

Append-only JSONL, advisory, fail-soft — a ledger write must never break the run
it records. ``THREAD_ARCHIVE_BENCH_RUNS_LOG=0`` disables it.

Lives in the lab's own state root (``eval_home.state_root``), which is neither
the archive home nor the corpus cache. Not the archive home because nothing on
this bench measures the archive — every row scores a throwaway corpus built from
a public dataset, so an install's directory has no business holding it. Not the
corpus cache because that tree is documented as safe to delete, and these rows
are the one part of a bench pass that cannot be rebuilt: the code and corpus a
row measured are gone the moment either changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LEDGER_FILE = "bench-runs.jsonl"

#: The source whose content defines a measurement's ``code_id``: the retrieval
#: stack (every ranking knob and arm) and the scoring core every harness runs
#: through. A change under either can move a number, so a row measured before
#: it is not a row you may report after it.
CODE_PATHS = ("src/thread_archive/_retrieval", "search_lab/eval_core.py")


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_BENCH_RUNS_LOG", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _repo_root() -> Path:
    """The archive checkout — this file's directory is its child."""
    return Path(__file__).resolve().parents[1]


def ledger_home() -> Path:
    """The lab's state root — where this history lives.

    Deliberately blind to ``THREAD_ARCHIVE_HOME``: the harnesses pin that at
    their own throwaway corpus for the length of a run, so honoring it would
    scatter the history across the very homes it describes, and those get wiped
    and rebuilt. A ledger location has to be a fact about the box, not about
    whichever corpus is mounted."""
    from eval_home import state_root

    return state_root()


def hash_sources(root: Path, entries: tuple[str, ...]) -> str:
    """A content hash of every ``.py`` under ``entries`` (files or directories,
    relative to ``root``).

    Path and content both feed the digest, in sorted order, so the id is stable
    across checkouts and machines and moves with a rename as well as an edit. A
    missing entry contributes nothing rather than raising — a partial checkout
    should measure what it has, not refuse to record."""
    h = hashlib.sha256()
    files: list[Path] = []
    for entry in entries:
        path = root / entry
        if path.is_dir():
            files.extend(p for p in path.rglob("*.py"))
        elif path.is_file():
            files.append(path)
    for path in sorted(files):
        h.update(str(path.relative_to(root)).encode())
        h.update(b"\x00")
        h.update(path.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def code_id(extra: tuple[str, ...] = ()) -> str:
    """The hash of the ranking and scoring source as it sits on disk *now*, plus
    whatever ``extra`` paths a caller counts as its own code.

    Uncommitted edits count, which is the whole point: the tuning loop's unit of
    change is an edited default that is never committed between passes, and it has
    to invalidate a recorded measurement exactly the way a commit would.

    ``extra`` is how a benchmark row adds the harness that produced it, so editing
    one harness invalidates its own rows and leaves the rest fresh. The shared set
    stays narrow for the same reason: widen it to the whole lab and every edit
    anywhere — a progress line, a docstring — would re-run the bench, which is a
    cache nobody keeps."""
    return hash_sources(_repo_root(), CODE_PATHS + extra)


def record_run(
    *,
    row: str,
    argv: list[str],
    corpus_id: Optional[str],
    measures: dict[str, Any],
    elapsed_s: float,
    status: str,
    code: Optional[str] = None,
    home: Optional[Path] = None,
    performance: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Append one benchmark row's run and return the record.

    ``status`` is ``ok`` / ``failed`` / ``skipped`` — a failure is recorded rather
    than dropped, because "this row stopped being runnable at this configuration"
    is exactly the kind of thing a silent gap hides. ``code`` is the row's own code
    id (it knows which harness produced it); absent, the shared one is recorded.

    ``performance`` is what the run cost — latency distribution, per-stage
    profile, throughput — kept separate from ``measures``, which is what it
    scored. Both were produced by the same searches, and only together do they
    say whether a configuration that scores better is one you would ship: a
    tuning pass that lifts nDCG and doubles p99 is a trade, and a ledger that
    recorded only the first half would file it as a win. Omitted when the harness
    reported none, rather than written as an empty block that reads as measured.

    Fail-soft: a write error is logged and swallowed."""
    from run_meta import git_commit

    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": "bench-run",
        "row": row,
        "status": status,
        "code_id": code or code_id(),
        "corpus_id": corpus_id,
        "commit": git_commit(),
        "elapsed_s": round(elapsed_s, 1),
        "measures": measures,
        "argv": argv,
    }
    if performance:
        record["performance"] = performance
    if not _enabled():
        return record
    try:
        path = (home or ledger_home()) / LEDGER_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("could not record benchmark run", exc_info=True)
    return record


def run_id(record: dict[str, Any]) -> str:
    """A stable handle for one recorded run — what a link addresses it by.

    A content hash rather than a position in the file: the ledger is append-only
    and a record is never rewritten, so hashing the record itself survives the
    file growing under it, and survives a compaction that a line number would
    not. Two byte-identical records share an id, which is the honest answer —
    nothing recorded distinguishes them."""
    blob = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# ── per-query detail ─────────────────────────────────────────────────────────
# One file per run, beside the ledger rather than inside it. A bench pass scores
# ~5,600 queries across its rows, so inlining the detail would add about a
# megabyte per pass to a file that is read *whole* on every page load, to render
# a table that shows none of it. Keyed by run id, so opening one run reads one
# file and the list view reads none.

QUERIES_DIR = "bench-queries"

#: How many runs keep their per-query detail. The aggregate history is small and
#: kept forever; this is the bulky half, and its value drops off fast — the
#: queries a configuration failed matter while that configuration is one you are
#: still deciding about. Ten bench passes' worth.
QUERIES_KEEP = 60


def queries_dir(home: Optional[Path] = None) -> Path:
    return (home or ledger_home()) / QUERIES_DIR


def write_queries(run_id_: str, rows: list[dict[str, Any]], *,
                  home: Optional[Path] = None) -> Optional[Path]:
    """Store one run's per-query rows and prune the oldest beyond the cap.

    Fail-soft like the ledger write it accompanies: losing the detail must never
    fail the run that produced it, and a run whose sidecar could not be written
    is still a run — the page reads its absence as "no detail kept", which is
    also what a pruned run reads as."""
    if not rows or not _enabled():
        return None
    try:
        path = queries_dir(home) / f"{run_id_}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
        prune_queries(home=home)
        return path
    except OSError:
        logger.warning("could not record per-query detail", exc_info=True)
        return None


def read_queries(run_id_: str, *, home: Optional[Path] = None) -> list[dict[str, Any]]:
    """One run's per-query rows, or empty when none were kept. Never an error:
    a run from before the detail was recorded, one whose harness reported none,
    and one pruned by the cap are all "no detail here"."""
    # The id reaches this from a URL, so it must not be able to name a path.
    if not run_id_ or not all(c in "0123456789abcdef" for c in run_id_):
        return []
    try:
        blob = json.loads((queries_dir(home) / f"{run_id_}.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return blob if isinstance(blob, list) else []


def prune_queries(*, home: Optional[Path] = None, keep: int = QUERIES_KEEP) -> int:
    """Drop all but the ``keep`` newest sidecars. Returns how many were removed."""
    try:
        files = sorted(queries_dir(home).glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for path in files[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def read_runs(home: Optional[Path] = None, *, row: Optional[str] = None,
              limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Recorded runs, newest first, optionally for one row. A missing or
    unreadable ledger reads as empty (no history yet), never an error."""
    path = (home or ledger_home()) / LEDGER_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    runs: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row is None or record.get("row") == row:
            runs.append(record)
    runs.reverse()
    return runs[:limit] if limit is not None else runs


def latest_ok(home: Optional[Path] = None, *, row: str) -> Optional[dict[str, Any]]:
    """The most recent *successful* run of ``row``, or None.

    Successful only: a failed row carries no numbers to report and nothing to
    skip on, and treating it as the reference would make a broken row look fresh
    forever."""
    for record in read_runs(home, row=row):
        if record.get("status") == "ok":
            return record
    return None


def is_fresh(record: Optional[dict], *, corpus_id: Optional[str],
             code: Optional[str] = None) -> bool:
    """Whether a recorded run still describes what a run right now would measure.

    Fresh means the code is unchanged *and* the corpus is the one that was
    measured. ``code`` is the row's own code id, defaulting to the shared one.
    ``corpus_id`` of None means the caller could not cheaply establish the corpus's
    identity (the per-question haystacks build hundreds of small homes rather than
    one), in which case the code hash decides alone — those corpora come from
    immutable dataset files, so the risk it leaves open is a dataset re-download,
    not an ordinary tuning pass."""
    if not record or record.get("status") != "ok":
        return False
    if record.get("code_id") != (code or code_id()):
        return False
    return corpus_id is None or record.get("corpus_id") == corpus_id
