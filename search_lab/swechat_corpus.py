#!/usr/bin/env python3
"""Build a SWE-chat corpus home and its session↔commit linkage file.

`SWE-chat <https://huggingface.co/datasets/SALT-NLP/SWE-chat>`_ is a public
collection of real coding-agent sessions from open-source developers (ODC-BY;
arXiv:2604.20779). Its transcripts are native Claude Code JSONL, so the shipped
claude-code importer ingests them unchanged — this harness only orchestrates the
build and derives the provenance linkage that ``python -m search_lab.mine commit``
consumes.

Why this corpus is worth a home of its own: it ships **session ↔ commit
provenance**, and that is what a gold label has to be fixed by. A label
established by searching the corpus can only describe what the incumbent ranker
already reaches; a commit is an artifact outside retrieval that says which
session did the work, whatever search thinks. No other corpus on this bench
carries that, so this is the only home the ``commit`` miner can run in. It is
domain-matched besides — agent session logs, not the mismatched third-party IR
corpora ``beir_eval`` / ``cdr_eval`` calibrate against — and written by other
people about other codebases, so nothing here was authored by whoever tunes the
ranker.

Two phases, separable:

- **build** — ingest every transcript into a throwaway home, one thread per
  session, ``source_id`` = the SWE-chat ``session_id`` so the linkage can find it
  again. ``--vectors`` embeds (needed for the semantic arms; slow).
- **linkage** — join the parquet tables into ``commit-linkage.jsonl``: one row per
  session whose commits are unambiguously its own.

Both paths end by folding the code axis (:func:`fold_code_index`) before the
stamp. Nothing else builds ``event_paths`` here — a corpus home has no watcher —
and without it ``mine edited`` has no path projection to enumerate gold from, so
the corpus would carry commit provenance and no completeness rung.

Derived artifacts — the linkage and the golds mined from it — land in ``gold/``
beside the download (see :func:`default_gold_dir`), not in the archive's private
gold dir. Either phase ends by stamping the home as a snapshot in place (no copy:
it was built frozen), which is what ``mine`` and ``retrieval_eval --cases`` bind
their golds to.

  The join that matters: a checkpoint with ``session_count == 1`` and commits
  attached attributes those commits to exactly one session, so the session that
  produced that code is known *structurally* — no retrieval, no judge. A session
  is taken through **any** solo checkpoint it appears in, not only its canonical
  one: appearing in a solo checkpoint is what makes the commits attributable, and
  which checkpoint the dataset marks canonical is irrelevant to that.

  The linkage file covers only the sessions the built home actually holds, so a
  transcript the importer could not read takes its commits out of the linkage
  too.

Requires ``pyarrow`` (dev extra) to read the parquet tables; the transcripts
themselves need nothing beyond the archive.

Usage::

    python search_lab/swechat_corpus.py --data ~/dev/swe-chat-data/swe-chat
    python search_lab/swechat_corpus.py --data ... --linkage-only   # reuse built home
    THREAD_ARCHIVE_HOME=<home> python -m search_lab.mine commit --target 10
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# The lab dir too, so bare sibling imports (eval_core, snapshot, …) resolve
# however this file was loaded: as a script, by path, or as search_lab.X.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_home  # noqa: E402
from mine import commit_linked  # noqa: E402
from snapshot import stamp_snapshot  # noqa: E402

from thread_archive import _api as api  # noqa: E402
from thread_archive._store import use_session  # noqa: E402

DEFAULT_HOME = eval_home.CACHE_ROOT / "homes" / "swe-chat"
DEFAULT_DATA = Path.home() / "dev" / "swe-chat-data" / "swe-chat"


def default_gold_dir(data: Path) -> Path:
    """Where this corpus's derived artifacts belong: ``gold/`` beside the download,
    not the archive's own gold dir.

    The archive keeps mined cases out of any repo because they quote the operator's
    real conversations. Nothing mined here does — the corpus is a public dataset, the
    golds cite public sessions, and they are only meaningful next to the download
    they resolve against. Beside the data they can travel with it; under
    ``~/.thread/archive`` they are private-by-default artifacts nobody else can use.
    A sibling of the download rather than a directory inside it, so re-running the
    dataset fetch never sees them."""
    return data.parent / "gold"

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

#: ``type`` values Claude Code writes on a transcript's opening line. Most of
#: these are preambles rather than turns — a session more often opens on a
#: ``file-history-snapshot`` or a ``progress`` record than on a ``user`` one —
#: and the preambles carry none of the session keys, so the vocabulary has to be
#: able to identify the file on its own.
CC_FIRST_LINE_TYPES = frozenset({
    "user", "assistant", "system", "summary", "attachment", "progress",
    "queue-operation", "permission-mode", "file-history-snapshot",
    "custom-title", "ai-title", "pr-link",
})


def is_claude_code_transcript(path: Path) -> bool:
    """Whether a transcript is line-delimited Claude Code JSON, the one shape the
    shipped importer reads.

    SWE-chat's ``transcripts/`` mixes providers under a single ``.jsonl``
    extension: the OpenCode sessions are *pretty-printed* JSON objects, so every
    line fails to parse and the importer rejects the file after logging a parse
    error per line — 2.5 M of them across a full build. Cheaper and quieter to
    recognize the shape here. The check reads one line, and keys on the fields the
    Claude Code parser needs rather than on the filename.

    A bare ``type`` key does not identify the harness. Codex writes line-delimited
    JSON too, and its opening record is ``{"timestamp": …, "type": "session_meta",
    "payload": {…}}`` — line-shaped, ``type``-bearing, and unreadable by the Claude
    Code parser, which lifts it into a thread of about four events instead of the
    several hundred a real session holds. So a record with no session key has to
    name a ``type`` Claude Code actually writes."""
    try:
        with path.open(errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                return isinstance(rec, dict) and (
                    "sessionId" in rec or "parentUuid" in rec
                    or rec.get("type") in CC_FIRST_LINE_TYPES)
    except (json.JSONDecodeError, OSError):
        return False
    return False


def build_home(data: Path, home: Path, *, limit: int, vectors: bool,
               fresh: bool) -> int:
    """Ingest SWE-chat transcripts into ``home``, one thread per session. Returns
    the count imported. ``source_id`` is the session id (the transcript stem), which
    is what makes the linkage phase able to map a session to its thread.

    Every readable transcript is taken. The corpus is the download minus what the
    line-stream importer cannot parse — OpenCode and most Gemini CLI sessions are
    pretty-printed JSON objects, Cursor's are neither, and Codex's line-delimited
    JSON is a different envelope the Claude Code parser mangles rather than reads
    (see :func:`is_claude_code_transcript`). Archive's OpenCode and Cursor
    importers are DB scanners with no JSON-export path, so those sessions have no
    route in at all. That is the benchmark's one coverage bound and it is worth
    roughly a sixth of the download; the skipped count is reported rather than
    absorbed silently."""
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

    # The ingest is a tracked load run, like the embed after it: progress/ETA in
    # <home>/load-state.json while it runs, a ledger row when it ends — a corpus
    # build must be as visible as any other archive load.
    from thread_archive._ops.load_runs import load_run

    done = 0
    with load_run("import", home=home, note="swe-chat corpus build") as run:
        with run.phase("import", total=len(usable)) as ph:
            for i, path in enumerate(usable, 1):
                try:
                    api.import_path(path, provider=SOURCE, source_id=path.stem)
                    done += 1
                except Exception as exc:  # one malformed transcript must not end the build
                    print(f"  ! {path.name}: {type(exc).__name__}: {exc}")
                    ph.count("failed", 1)
                ph.advance()
                if i % 250 == 0:
                    print(f"  ingested {i}/{len(usable)} ({done} ok)")
    print(f"ingested {done}/{len(usable)} transcripts into {home}")
    if vectors:
        print("embedding (slow)...")
        api.embed()
    return done


def fold_code_index(home: Path) -> dict:
    """Fold the code axis over this home, so ``event_paths`` describes it.

    A live archive gets this from its watcher. A corpus home is built by a script
    and has no watcher, so without an explicit fold the projection stays empty —
    and an empty ``event_paths`` is not a degraded path axis but an absent one:
    ``mine edited`` finds no path with two editing conversations and exits, and a
    ``path``-scoped browse answers nothing.

    Run on both build paths, including ``--linkage-only``, because it is the fold
    that an already-built home is most likely to be missing. Idempotent — it is a
    cursor projection, so a folded home costs one cursor read. It reads only the
    event log, so the corpus fingerprint (a hash over ``events``) does not move and
    golds already mined here stay valid."""
    os.environ["THREAD_ARCHIVE_HOME"] = str(home)
    print("folding the code axis (event_paths / event_commits)...")
    result = api.code_index(home=str(home))
    print(f"  code axis: {result['paths']} path row(s) over "
          f"{result['distinct_paths']} distinct path(s), "
          f"{result['commits']} commit row(s)")
    return result


def repo_groups(data: Path, sessions: set[str]) -> dict[str, list[str]]:
    """``{repo_id: [session_id, ...]}`` over the sessions actually ingested — the
    repository label ``swechat_bench.py`` tags each exported corpus row with.

    Membership is established by provenance rather than by a model: every session
    in a repository shares file names, module names and domain vocabulary, so a
    query naming any of them has many near-misses and one right answer. Unlike a
    grouping read off the embedding graph, it owes nothing to the model the
    vector arm also ranks with."""
    pq = _require_pyarrow()
    t = pq.read_table(data / "sessions.parquet",
                      columns=["session_id", "repo_id"]).to_pylist()
    out: dict[str, list[str]] = {}
    for row in t:
        sid = str(row["session_id"])
        if sid in sessions:
            out.setdefault(str(row["repo_id"]), []).append(sid)
    return out


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


@functools.lru_cache(maxsize=1)
def _linkage_eligible(data: Path) -> list[dict]:
    """Sessions whose commits are attributable to them alone, each with the commit
    material a case is authored from. Independent of any built home: the corpus
    selector ranks repos with it *before* ingest, and :func:`build_linkage` maps
    thread ids onto it afterward. Cached — the commits table is a 1 GB read."""
    pq = _require_pyarrow()
    read = lambda name, cols: pq.read_table(  # noqa: E731
        data / f"{name}.parquet", columns=cols).to_pylist()

    checkpoints = read("checkpoints", ["checkpoint_pk", "checkpoint_id",
                                       "session_count"])
    commits = read("commits", ["checkpoint_pk", "commit_sha", "commit_message",
                               "files_changed", "patch", "is_agent_author",
                               "status"])
    # A large minority of SWE-chat's commit rows are ``commit_not_found``: the
    # session↔sha link survived but the commit itself was never retrieved, so
    # message and patch are empty. They carry no material to author a query from,
    # so they are dropped here rather than becoming units with an empty diff. The
    # count is printed, because it bounds how many cases the corpus can yield.
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
        pks = {id_to_pk.get(i) for i in _as_list(s["checkpoint_ids"])}
        pks.add(s["canonical_checkpoint_pk"])
        owned = [c for pk in (pks & solo) for c in by_checkpoint[pk]]
        if not owned:
            continue
        rows.append({
            "session_id": str(s["session_id"]),
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


def build_linkage(data: Path, thread_of_session: dict[str, str]) -> list[dict]:
    """Linkage rows for the sessions actually present in the built home. A row
    naming a thread the corpus doesn't hold is a case whose gold can never rank, so
    an uningested session yields nothing."""
    out: list[dict] = []
    for row in _linkage_eligible(data):
        tid = thread_of_session.get(row["session_id"])
        if tid is not None:
            out.append({"thread_id": tid, **row})
    return out


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
    ap.add_argument("--gold-dir", type=Path, default=None,
                    help="where this corpus's derived artifacts land "
                         "(default: gold/ beside the download)")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"linkage file (default <gold-dir>/{commit_linked.LINKAGE_NAME})")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="ingest only the first N transcripts (0 = all). A smoke "
                         "test for the build itself — it truncates in filename "
                         "order, so it yields no corpus worth measuring against")
    ap.add_argument("--vectors", action="store_true",
                    help="embed after ingest (needed for the semantic arms)")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the home and rebuild from scratch")
    ap.add_argument("--linkage-only", action="store_true",
                    help="skip ingest; derive linkage from an already-built home")
    args = ap.parse_args(argv)

    home = eval_home.guard_home(args.home, what="SWE-chat corpus home")
    eval_home.pin_arms(vectors=args.vectors)

    if not args.linkage_only:
        build_home(args.data, home, limit=args.limit, vectors=args.vectors,
                   fresh=args.fresh)

    mapping = thread_ids_by_session(home)
    if not mapping:
        raise SystemExit(f"no ingested sessions in {home} — build it first")
    fold_code_index(home)
    gold = (args.gold_dir or default_gold_dir(args.data)).expanduser()
    groups = repo_groups(args.data, set(mapping))
    groups_path = gold / "repo-groups.json"
    groups_path.parent.mkdir(parents=True, exist_ok=True)
    groups_path.write_text(json.dumps(groups, indent=1) + "\n")
    print(f"repo groups: {len(groups)} repo(s) over {sum(map(len, groups.values()))} "
          f"session(s) -> {groups_path}")

    rows = build_linkage(args.data, mapping)
    out = write_linkage(rows, (args.out or gold / commit_linked.LINKAGE_NAME).expanduser())

    repos = len({r["repo"] for r in rows})
    print(f"linkage: {len(rows)} session(s) with attributable commits across "
          f"{repos} repo(s) -> {out}")

    # The home is the snapshot: it is built from a fixed download and nothing
    # appends to it, so it needs the manifest mining binds against, not a copy of
    # itself. Stamped last, once the corpus is final — the id is a content
    # fingerprint, so a rebuild takes a new one and the previous run's golds read
    # as stale instead of scoring against a corpus that changed underneath them.
    manifest = stamp_snapshot(str(home))
    print(f"snapshot: {home} stamped {manifest['snapshot_id']} "
          f"({manifest['counts']['threads']} threads, "
          f"{manifest['counts']['vectors']} vectors)")
    print(f"next: THREAD_ARCHIVE_HOME={home} python -m search_lab.mine commit --target 10 \\\n"
          f"        --linkage {out} --out {gold / 'commit-cases.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
