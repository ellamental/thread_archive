#!/usr/bin/env python3
"""Build a SWE-chat corpus home and its session↔commit linkage file.

`SWE-chat <https://huggingface.co/datasets/SALT-NLP/SWE-chat>`_ is a public
collection of real coding-agent sessions from open-source developers (ODC-BY;
arXiv:2604.20779). Its transcripts are native Claude Code JSONL, so the shipped
claude-code importer ingests them unchanged — this harness only orchestrates the
build and derives the provenance linkage that ``python -m search_lab.mine commit``
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
- **linkage** — join the parquet tables into ``commit-linkage.jsonl``: one row per
  session whose commits are unambiguously its own.

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

  The linkage file covers only the sessions the built home actually holds, so its
  row count tracks the corpus budget below, not the dataset's eligible set.

Requires ``pyarrow`` (dev extra) to read the parquet tables; the transcripts
themselves need nothing beyond the archive.

Usage::

    python search_lab/swechat_corpus.py --data ~/dev/swe-chat-data/swe-chat
    python search_lab/swechat_corpus.py --data ... --linkage-only   # reuse built home
    THREAD_ARCHIVE_HOME=<home> python -m search_lab.mine commit --target 10
"""

from __future__ import annotations

import argparse
import collections
import functools
import json
import os
import re
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


#: Sessions kept per repository when a corpus budget is set. The miner grades at
#: most MAX_PARTIAL + MAX_CONFOUND (6 + 12) siblings into a pool, so a repo needs
#: only a few dozen members to give every gold a full-density confound set; past
#: that, extra sessions from the same codebase buy nothing the pool can use and
#: cost embedding time that another repo's confounds would use better.
DEFAULT_PER_REPO = 40


def choose_sessions(by_repo: dict[str, list[str]], linked: set[str], budget: int,
                    per_repo: int) -> set[str]:
    """The corpus selection itself, over plain data — which sessions a ``budget``
    buys. :func:`select_corpus` is the parquet-reading wrapper.

    Embedding is what bounds a corpus home (a full SWE-chat build is 249k
    embeddable docs at ~10-16 chunks/s), so the corpus has to be smaller than the
    download, and *what* gets dropped decides whether it still measures anything.
    Two rules do the work:

    - **Rank repos by commit-linked yield.** The budget should go where cases can
      actually be mined, not to repos with no attributable commits.
    - **Cap each repo, linked sessions first.** Session counts are power-law skewed
      — SWE-chat's largest repo is 870 of 5851 sessions, so taking repos whole would
      spend an entire modest budget inside one codebase and leave a benchmark that
      measures search over a single project. Capping spreads the budget across many
      repos while still leaving each gold far more siblings than its pool can hold.
    """
    ranked = sorted(by_repo.items(),
                    key=lambda kv: -sum(1 for s in kv[1] if s in linked))
    keep: set[str] = set()
    for _repo, members in ranked:
        if len(keep) >= budget:
            break
        # Linked sessions first: they are the ones that can become golds, and the
        # rest of the repo is only there to be their confounds.
        ordered = ([s for s in members if s in linked]
                   + [s for s in members if s not in linked])
        keep.update(ordered[:min(per_repo, budget - len(keep))])
    return keep


def select_corpus(data: Path, budget: int,
                  per_repo: int = DEFAULT_PER_REPO) -> set[str] | None:
    """Session ids for a corpus of about ``budget`` sessions, read from the parquet
    tables and chosen by :func:`choose_sessions`. ``None`` when ``budget`` is 0 —
    take everything."""
    if not budget:
        return None
    pq = _require_pyarrow()
    sessions = pq.read_table(data / "sessions.parquet",
                             columns=["session_id", "repo_id"]).to_pylist()
    by_repo: dict[str, list[str]] = {}
    for s in sessions:
        by_repo.setdefault(str(s["repo_id"]), []).append(str(s["session_id"]))
    linked = {r["session_id"] for r in _linkage_eligible(data)}
    return choose_sessions(by_repo, linked, budget, per_repo)


def build_home(data: Path, home: Path, *, limit: int, vectors: bool,
               fresh: bool, sessions: set[str] | None = None) -> int:
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

    if sessions is not None:
        transcripts = [p for p in transcripts if p.stem in sessions]

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


def repo_groups(data: Path, sessions: set[str]) -> dict[str, list[str]]:
    """``{repo_id: [session_id, ...]}`` over the sessions actually ingested — the
    grouping ``corpus_topics.py --groups`` turns into one topic per repository.

    A repository is the confound-dense subject the topic miner wants, established
    by provenance rather than by a model: every session in it shares file names,
    module names and domain vocabulary, so a query naming any of them has many
    near-misses and one right answer. Unlike a grouping read off the embedding
    graph, it owes nothing to the model the vector arm also ranks with."""
    pq = _require_pyarrow()
    t = pq.read_table(data / "sessions.parquet",
                      columns=["session_id", "repo_id"]).to_pylist()
    out: dict[str, list[str]] = {}
    for row in t:
        sid = str(row["session_id"])
        if sid in sessions:
            out.setdefault(str(row["repo_id"]), []).append(sid)
    return out


# Cross-cutting subjects, named by the class of file a session touched. Each
# entry is (title, path predicate); a session joins a group when any of its
# `files_touched` paths matches. Ordered widest-net last so the printed summary
# reads from sharpest to broadest.
FILE_CLASSES: list[tuple[str, "re.Pattern[str]"]] = [
    ("CI workflow and pipeline config",
     re.compile(r"(^|/)\.github/workflows/|(^|/)(\.gitlab-ci\.yml|azure-pipelines\.yml)$")),
    ("Dependency and lockfile management",
     re.compile(r"(^|/)(package(-lock)?\.json|pnpm-lock\.yaml|yarn\.lock|Cargo\.(toml|lock)"
                r"|go\.(mod|sum)|requirements\.txt|pyproject\.toml|uv\.lock|Gemfile(\.lock)?"
                r"|composer\.json)$")),
    ("Styling and CSS",
     re.compile(r"\.(css|scss|sass|less)$|tailwind\.config\.")),
    ("Agent instruction files",
     re.compile(r"(^|/)(CLAUDE|AGENTS|GEMINI)\.md$|(^|/)\.cursorrules$|(^|/)\.claude/")),
    ("Test suites",
     re.compile(r"(^|/)(tests?|__tests__|spec)/|[._](test|spec)\.[a-z]+$|_test\.go$")),
]

# A group this small can't support a survey; a group this dominated by one repo
# is the repo topic again under another name.
MIN_CLASS_MEMBERS = 20
MAX_CLASS_REPO_SHARE = 0.5


def file_class_groups(data: Path, sessions: set[str]) -> dict[str, list[str]]:
    """``{subject: [session_id, ...]}`` grouped by the *class of file* a session
    touched — the cross-cutting counterpart to :func:`repo_groups`.

    A repository topic is confound-dense but trivially separable: each repo owns
    its own file and module names, so nothing in one repo competes with a query
    aimed at another. The subjects that cut *across* repos are the harder case,
    and ``files_touched`` names them out of the dataset itself: sessions that
    edited a workflow file were doing CI work whatever the project, and they
    collide on `yaml`, `runner`, `job`, `matrix` regardless of repo.

    Membership here is a proxy — touching a workflow file is not proof the
    session was *about* CI — and it does not need to be exact, because it never
    reaches the gold. The grouping only decides which subjects a survey agent is
    pointed at; the graded pool comes from the labeler judging each thread
    against the query's stated intent. Groups too small to survey, or so
    dominated by a single repo that they restate :func:`repo_groups`, are
    dropped.

    Unlike the embedding communities ``corpus_topics.py --propose`` offers, this
    reads a recorded fact about each session, so it shares no model with the
    vector arm and cannot cluster on harness boilerplate."""
    pq = _require_pyarrow()
    rows = pq.read_table(data / "sessions.parquet",
                         columns=["session_id", "repo_id", "files_touched"]).to_pylist()
    hits: dict[str, list[str]] = {title: [] for title, _ in FILE_CLASSES}
    repos: dict[str, collections.Counter] = {
        title: collections.Counter() for title, _ in FILE_CLASSES}
    for row in rows:
        sid = str(row["session_id"])
        if sid not in sessions:
            continue
        paths = _as_list(row.get("files_touched"))
        for title, pattern in FILE_CLASSES:
            if any(pattern.search(str(p)) for p in paths):
                hits[title].append(sid)
                repos[title][str(row["repo_id"])] += 1
    out = {}
    for title, members in hits.items():
        if len(members) < MIN_CLASS_MEMBERS:
            continue
        if repos[title].most_common(1)[0][1] / len(members) > MAX_CLASS_REPO_SHARE:
            continue
        out[title] = members
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
                    help="ingest only the first N transcripts (0 = all)")
    ap.add_argument("--max-sessions", type=int, default=0, metavar="N",
                    help="cap the corpus near N sessions, spread across repos "
                         "(repos ranked by commit-linked yield, each capped at "
                         "--per-repo, linked sessions first). Embedding the full "
                         "corpus is tens of hours; this is the knob that makes a "
                         "build finish. 0 = all")
    ap.add_argument("--per-repo", type=int, default=DEFAULT_PER_REPO, metavar="N",
                    help=f"sessions kept per repo under a budget (default "
                         f"{DEFAULT_PER_REPO}; the miner pools at most 18 "
                         "confounds, so more buys nothing)")
    ap.add_argument("--vectors", action="store_true",
                    help="embed after ingest (needed for the semantic arms)")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the home and rebuild from scratch")
    ap.add_argument("--linkage-only", action="store_true",
                    help="skip ingest; derive linkage from an already-built home")
    args = ap.parse_args(argv)

    home = eval_home.guard_home(args.home, what="SWE-chat corpus home")
    # No rerank arm here: this builds a corpus, it does not score one.
    eval_home.pin_arms(vectors=args.vectors, rerank="off")

    if not args.linkage_only:
        keep = select_corpus(args.data, args.max_sessions, args.per_repo)
        if keep is not None:
            print(f"corpus budget {args.max_sessions}: keeping {len(keep)} session(s), "
                  f"<={args.per_repo}/repo, ranked by commit-linked yield")
        build_home(args.data, home, limit=args.limit, vectors=args.vectors,
                   fresh=args.fresh, sessions=keep)

    mapping = thread_ids_by_session(home)
    if not mapping:
        raise SystemExit(f"no ingested sessions in {home} — build it first")
    gold = (args.gold_dir or default_gold_dir(args.data)).expanduser()
    groups = repo_groups(args.data, set(mapping))
    groups_path = gold / "repo-groups.json"
    groups_path.parent.mkdir(parents=True, exist_ok=True)
    groups_path.write_text(json.dumps(groups, indent=1) + "\n")
    print(f"repo groups: {len(groups)} repo(s) over {sum(map(len, groups.values()))} "
          f"session(s) -> {groups_path}")

    subjects = file_class_groups(args.data, set(mapping))
    subjects_path = gold / "subject-groups.json"
    subjects_path.write_text(json.dumps(subjects, indent=1) + "\n")
    print(f"subject groups: {len(subjects)} cross-repo subject(s) -> {subjects_path}")

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
