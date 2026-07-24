#!/usr/bin/env python3
"""Build a SWE-chat corpus home and its session↔commit linkage file.

`SWE-chat <https://huggingface.co/datasets/SALT-NLP/SWE-chat>`_ is a public
collection of real coding-agent sessions from open-source developers (ODC-BY;
arXiv:2604.20779). Its transcripts are native Claude Code JSONL, so the shipped
claude-code importer ingests them unchanged — this harness only orchestrates the
build and derives the provenance linkage that ``thread_archive mine commit``
consumes.

Why this corpus is worth a home of its own: every gold file mined from the
operator's own archive is tuned against one corpus, by one author, over one set
of repositories. Hold-out discipline *within* that corpus cannot see overfitting
*to* it. SWE-chat is domain-matched (it is agent session logs, not the mismatched
third-party IR corpora ``beir_eval`` / ``cdr_eval`` calibrate against) yet written
by other people about other codebases, which makes a gold file mined here an
independent hold-out in the axis that actually matters.

Two phases, separable:

- **build** — ingest every transcript into a throwaway home, one thread per
  session, ``source_id`` = the SWE-chat ``session_id`` so the linkage can find it
  again. ``--vectors`` embeds (needed for the semantic arms; slow).
- **linkage** — join the parquet tables into ``commit-linkage.jsonl`` under the
  gold dir: one row per session whose commits are unambiguously its own.

  The join that matters: a checkpoint with ``session_count == 1`` and commits
  attached attributes those commits to exactly one session, so the session that
  produced that code is known *structurally* — no retrieval, no judge. A session
  is taken through **any** solo checkpoint it appears in, not only its canonical
  one (2132 sessions rather than 1366); appearing in a solo checkpoint is what
  makes the commits attributable, and which checkpoint the dataset marks canonical
  is irrelevant to that.

Requires ``pyarrow`` (dev extra) to read the parquet tables; the transcripts
themselves need nothing beyond the archive.

Usage::

    python evals/swechat_corpus.py --data ~/dev/swe-chat-data/swe-chat
    python evals/swechat_corpus.py --data ... --linkage-only   # reuse built home
    thread_archive snapshot <dir> && THREAD_ARCHIVE_HOME=<dir> \\
        thread_archive mine commit --target 10
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thread_archive import _api as api  # noqa: E402
from thread_archive._mine import commit_linked  # noqa: E402
from thread_archive._store import use_session  # noqa: E402

DEFAULT_HOME = Path.home() / ".cache" / "thread-evals" / "homes" / "swe-chat"
DEFAULT_DATA = Path.home() / "dev" / "swe-chat-data" / "swe-chat"

# Per-commit diff kept in the linkage file. The authoring agent sees at most
# MAX_PATCH_CHARS of it; storing a little more leaves room for multi-commit
# sessions without carrying the dataset's full 1 GB of patches into the sidecar.
LINKAGE_PATCH_CHARS = 8000

SOURCE = "claude-code"


def _require_pyarrow():
    """The parquet reader, or a pointed failure. Only the linkage phase needs it,
    so the dependency is dev-only rather than a product one."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise SystemExit(
            "the linkage phase needs pyarrow to read SWE-chat's parquet tables: "
            "pip install pyarrow  (it is in the archive 'dev' extra)")
    return pq


# ── build ───────────────────────────────────────────────────────────────────

def is_claude_code_transcript(path: Path) -> bool:
    """Whether a transcript is line-delimited Claude Code JSON, the one shape the
    shipped importer reads.

    SWE-chat's ``transcripts/`` mixes providers under a single ``.jsonl``
    extension: the OpenCode sessions are *pretty-printed* JSON objects, so every
    line fails to parse and the importer rejects the file after logging a parse
    error per line — 2.5 M of them across a full build. Cheaper and quieter to
    recognize the shape here. The check reads one line, and keys on the fields the
    Claude Code parser needs rather than on the filename."""
    try:
        with path.open(errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                return isinstance(rec, dict) and (
                    "sessionId" in rec or "parentUuid" in rec or "type" in rec)
    except (json.JSONDecodeError, OSError):
        return False
    return False


def build_home(data: Path, home: Path, *, limit: int, vectors: bool,
               fresh: bool) -> int:
    """Ingest SWE-chat transcripts into ``home``, one thread per session. Returns
    the count imported. ``source_id`` is the session id (the transcript stem), which
    is what makes the linkage phase able to map a session to its thread.

    The corpus this builds is **Claude Code only** — the other harnesses SWE-chat
    collects (OpenCode, Codex, Gemini CLI, Cursor) ship transcript shapes the
    line-stream importer can't read, and archive's OpenCode/Cursor importers are DB
    scanners with no JSON-export path. That is a coverage bound on the resulting
    benchmark, not a silent drop: the skipped count is reported."""
    transcripts = sorted((data / "transcripts").glob("*.jsonl"))
    if not transcripts:
        raise SystemExit(f"no transcripts under {data / 'transcripts'}")
    if limit:
        transcripts = transcripts[:limit]

    usable = [p for p in transcripts if is_claude_code_transcript(p)]
    skipped = len(transcripts) - len(usable)
    if skipped:
        print(f"skipping {skipped}/{len(transcripts)} non-Claude-Code transcript(s) "
              "(other harnesses' formats)")

    if fresh:
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    os.environ["THREAD_ARCHIVE_HOME"] = str(home)
    api.open_archive(str(home))

    done = 0
    for i, path in enumerate(usable, 1):
        try:
            api.import_path(path, provider=SOURCE, source_id=path.stem)
            done += 1
        except Exception as exc:  # one malformed transcript must not end the build
            print(f"  ! {path.name}: {type(exc).__name__}: {exc}")
        if i % 250 == 0:
            print(f"  ingested {i}/{len(usable)} ({done} ok)")
    print(f"ingested {done}/{len(usable)} transcripts into {home}")
    if vectors:
        print("embedding (slow)...")
        api.embed()
    return done


def thread_ids_by_session(home: Path) -> dict[str, str]:
    """``session_id -> thread_id`` for everything ingested into ``home``. Reads the
    store rather than a build-time map, so ``--linkage-only`` works against a home
    built by an earlier run."""
    os.environ["THREAD_ARCHIVE_HOME"] = str(home)
    api.open_archive(str(home))
    from sqlalchemy import text as sa_text

    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT source_id, id FROM threads WHERE source = :src "
            "AND thread_type = 'conversation'"), {"src": SOURCE}).all()
    return {str(sid): str(tid) for sid, tid in rows}


# ── linkage ─────────────────────────────────────────────────────────────────

def _as_list(value) -> list:
    """SWE-chat stores list-ish columns as JSON strings; normalize both shapes."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            v = json.loads(value)
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return []


def build_linkage(data: Path, thread_of_session: dict[str, str]) -> list[dict]:
    """Join the parquet tables into linkage rows for sessions with attributable
    commits. Only sessions present in ``thread_of_session`` (i.e. actually ingested)
    yield a row — a linkage row naming a thread that isn't in the corpus is a case
    whose gold can never rank."""
    pq = _require_pyarrow()
    read = lambda name, cols: pq.read_table(  # noqa: E731
        data / f"{name}.parquet", columns=cols).to_pylist()

    checkpoints = read("checkpoints", ["checkpoint_pk", "checkpoint_id",
                                       "session_count"])
    commits = read("commits", ["checkpoint_pk", "commit_sha", "commit_message",
                               "files_changed", "patch", "is_agent_author",
                               "status"])
    # Roughly half of SWE-chat's commit rows are ``commit_not_found``: the
    # session↔sha link survived but the commit itself was never retrieved, so
    # message and patch are empty. They carry no material to author a query from,
    # so they are dropped here rather than becoming units with an empty diff.
    unretrieved = sum(1 for c in commits if c["status"] != "ok")
    commits = [c for c in commits if c["status"] == "ok"]
    sessions = read("sessions", ["session_id", "repo_id", "checkpoint_ids",
                                 "canonical_checkpoint_pk", "files_touched"])

    # ``repo_id`` is already ``owner/name`` — unique and readable, which the bare
    # ``repositories.name`` is not (7 of 205 names collide across owners, and
    # grouping on one would make unrelated repos each other's confounds).
    id_to_pk = {c["checkpoint_id"]: c["checkpoint_pk"] for c in checkpoints}

    by_checkpoint: dict[object, list[dict]] = {}
    for c in commits:
        by_checkpoint.setdefault(c["checkpoint_pk"], []).append(c)

    # A checkpoint attributes its commits to one session only when it holds one.
    solo = {c["checkpoint_pk"] for c in checkpoints
            if c["session_count"] == 1 and c["checkpoint_pk"] in by_checkpoint}

    rows: list[dict] = []
    for s in sessions:
        tid = thread_of_session.get(str(s["session_id"]))
        if tid is None:
            continue
        pks = {id_to_pk.get(i) for i in _as_list(s["checkpoint_ids"])}
        pks.add(s["canonical_checkpoint_pk"])
        owned = [c for pk in (pks & solo) for c in by_checkpoint[pk]]
        if not owned:
            continue
        rows.append({
            "session_id": str(s["session_id"]),
            "thread_id": tid,
            "repo": str(s["repo_id"]),
            "files": [str(f) for f in _as_list(s["files_touched"])],
            "commits": [{
                "sha": c["commit_sha"],
                "message": (c["commit_message"] or "").strip(),
                "patch": (c["patch"] or "")[:LINKAGE_PATCH_CHARS],
                "files": (c["files_changed"] or "").split("\n")[:40],
                "agent_authored": bool(c["is_agent_author"]),
            } for c in owned if (c["commit_message"] or "").strip()],
        })
    rows = [r for r in rows if r["commits"]]
    print(f"  linkage: {unretrieved} commit row(s) skipped as unretrieved "
          f"(status != ok)")
    return rows


def write_linkage(rows: list[dict], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return out


# ── entry point ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA,
                    help=f"SWE-chat download dir (default {DEFAULT_DATA})")
    ap.add_argument("--home", type=Path, default=DEFAULT_HOME,
                    help=f"corpus home to build (default {DEFAULT_HOME})")
    ap.add_argument("--out", type=Path, default=None,
                    help="linkage file (default ~/.thread/archive/"
                         f"{commit_linked.LINKAGE_NAME})")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="ingest only the first N transcripts (0 = all)")
    ap.add_argument("--vectors", action="store_true",
                    help="embed after ingest (needed for the semantic arms)")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the home and rebuild from scratch")
    ap.add_argument("--linkage-only", action="store_true",
                    help="skip ingest; derive linkage from an already-built home")
    args = ap.parse_args(argv)

    home = args.home.expanduser().resolve()
    real = Path(os.environ.get("THREAD_ARCHIVE_HOME") or
                Path.home() / ".thread" / "archive").expanduser().resolve()
    # The build wipes and rewrites this home; overlapping the real archive would
    # destroy it. Same guard haystack_eval keeps over its throwaway homes.
    if home == real or real in home.parents or home in real.parents:
        raise SystemExit(f"refusing: corpus home {home} overlaps the real archive {real}")

    if not args.linkage_only:
        build_home(args.data, home, limit=args.limit, vectors=args.vectors,
                   fresh=args.fresh)

    mapping = thread_ids_by_session(home)
    if not mapping:
        raise SystemExit(f"no ingested sessions in {home} — build it first")
    rows = build_linkage(args.data, mapping)
    out = write_linkage(rows, (args.out or commit_linked.linkage_path()).expanduser())

    repos = len({r["repo"] for r in rows})
    print(f"linkage: {len(rows)} session(s) with attributable commits across "
          f"{repos} repo(s) -> {out}")
    print("next: thread_archive snapshot <dir>; THREAD_ARCHIVE_HOME=<dir> "
          "thread_archive mine commit --target 10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
