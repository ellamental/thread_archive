"""The code axis: which conversations touched a file, and which produced a commit.

Search answers "where did we *talk* about X". This answers "where did we *do* X to
this file" — a different question with a different index. The paths were always in
the archive (an ``Edit``'s ``file_path``, an ``apply_patch``'s header, a shell
command's arguments); folding them into :class:`~thread_archive._store.EventPath`
turns "which conversations edited rank.py" from a text search that happens to match
a path into an indexed lookup that can't miss the session that spelled the path
differently.

**The fold** (:func:`refresh_code_index`) is a cursor projection over the event log,
the same shape as ``_store._metrics``: append-only monotonic ids mean folding only
``id > through_event_id`` is exact, ``projection_version`` makes the extraction rules
revisable, and a log that shrank below the cursor (a reindex rebuilt it) rebuilds
rather than trusting a stale watermark. It walks **id windows**, not row counts, so
every batch is a primary-key range scan of bounded width — an event-type index seek
would have to sort a third of the corpus back into id order to resume.

**The queries** (:func:`blame_path`, :func:`blame_commit`, :func:`thread_files`) are
plain SQL over that projection, and they surface through the tools that already
exist rather than through one of their own: :func:`blame_path` is what a
``path``-scoped browse renders (:func:`.browse.browse_threads`),
:func:`thread_files` is ``thread_read(summary='files')``, and
:func:`blame_commit` backs the ``commit`` scope — the loop back from ``git blame``,
resolving a sha to every session that *contributed* to it (the one that ran ``git
commit`` is flagged among them, not substituted for them: a commit usually carries
work from several sittings, and wherever a human commits out of band it carries
nobody's).
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Iterable, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from .._store import use_session
from ._classify import resolve_relative_date
from ._paths import (
    ALL_OPS,
    COMMIT_PREFILTER_LIKES,
    TOUCH_OPS,
    basename_of,
    extract_commits,
    extract_paths,
    normalize_path,
)

logger = logging.getLogger(__name__)

#: Bump when the extraction rules change: a fold that finds a trailing version
#: discards both projections and rebuilds, because rows written under older rules
#: cannot be added to by newer ones.
PROJECTION_VERSION = 1

#: Event types the fold reads. Paths come from the tool *call* (which names the
#: file); commits come from the tool *result* (which prints the sha).
_PATH_EVENT_TYPES = ("tool_use_complete", "tool_use_started")
_COMMIT_EVENT_TYPES = ("tool_execution_completed", "tool_use_complete")

#: Ids per fold batch. Wide enough that the backfill isn't a million round trips,
#: narrow enough that one batch is a bounded amount of work to lose to a kill.
_WINDOW = 250_000

#: How far back a commit's authorship window is allowed to look for each file's
#: previous commit. Deep enough to cover a normal file's last touch, bounded so one
#: ``git log`` can't walk a decade of history; a file still uncovered is reported
#: rather than silently credited to everyone who ever edited it.
_HISTORY_WALK = 500

#: Files per commit whose contributors are resolved. A bulk checkpoint commit can
#: carry hundreds of paths and each is a query; the commit's own file count travels
#: in the result, so a truncated pass is visible rather than implied.
_COMMIT_FILE_CAP = 300

#: How many distinct paths a result samples. A repo-wide pattern matches hundreds
#: of files per thread and the caller wants the *sessions*; the true count rides
#: alongside as ``total_paths`` / ``n_paths`` so the sample is never mistaken for
#: the whole set.
_PATH_SAMPLE = 25


# ── The fold ─────────────────────────────────────────────────────────────────


def _thread_cwds(s: Session) -> dict[str, str]:
    """``thread_id → working directory`` for every thread that recorded one.

    The whole map, not a per-batch lookup: it is one small indexed read (a few
    thousand rows) against a fold that would otherwise re-query per event, and the
    cwd is what turns a relative ``src/foo.py`` into the same path the next session
    wrote absolutely.
    """
    rows = s.execute(sa_text(
        "SELECT id, json_extract(source_metadata, '$.cwd') FROM threads "
        "WHERE source_metadata IS NOT NULL"
    )).all()
    return {tid: cwd for tid, cwd in rows if cwd}


def _read_cursor(s: Session) -> tuple[int, int]:
    s.execute(sa_text(
        "INSERT OR IGNORE INTO code_cursor (id, through_event_id, projection_version) "
        "VALUES (1, 0, :v)"
    ), {"v": PROJECTION_VERSION})
    row = s.execute(sa_text(
        "SELECT through_event_id, projection_version FROM code_cursor WHERE id = 1"
    )).first()
    return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)


def _reset(s: Session) -> None:
    """Drop both projections and rewind the cursor — the response to a version
    change or a shrunken log. Safe because nothing here is truth."""
    s.execute(sa_text("DELETE FROM event_paths"))
    s.execute(sa_text("DELETE FROM event_commits"))
    s.execute(sa_text(
        "UPDATE code_cursor SET through_event_id = 0, projection_version = :v WHERE id = 1"
    ), {"v": PROJECTION_VERSION})


def _fold_window(s: Session, lo: int, hi: int, cwds: dict[str, str]) -> tuple[int, int]:
    """Fold events in ``(lo, hi]`` into both projections. Returns (paths, commits)."""
    path_rows: list[dict] = []
    for eid, tid, etype, occurred_at, payload in s.execute(sa_text(
        "SELECT id, thread_id, event_type, occurred_at, payload FROM events "
        "WHERE id > :lo AND id <= :hi AND event_type IN "
        "('tool_use_complete', 'tool_use_started') ORDER BY id"
    ), {"lo": lo, "hi": hi}):
        p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
        touched = extract_paths(etype, p, cwd=cwds.get(tid))
        if not touched:
            continue
        tool_name = (p.get("tool_name") or None)
        oa = str(occurred_at) if occurred_at else None
        for path, op in touched:
            path_rows.append({
                "event_id": eid, "thread_id": tid, "path": path,
                "basename": basename_of(path), "op": op,
                "tool_name": tool_name[:200] if tool_name else None, "occurred_at": oa,
            })

    # Commits: the payload pre-filter keeps this from being a Python parse of every
    # tool result in the window (they are the fattest rows in the archive).
    prefilter = " OR ".join(
        "payload LIKE :like" + str(i) for i in range(len(COMMIT_PREFILTER_LIKES))
    )
    params: dict = {"lo": lo, "hi": hi}
    params.update({"like" + str(i): v for i, v in enumerate(COMMIT_PREFILTER_LIKES)})
    commit_rows: list[dict] = []
    for eid, tid, etype, occurred_at, payload in s.execute(sa_text(
        "SELECT id, thread_id, event_type, occurred_at, payload FROM events "
        "WHERE id > :lo AND id <= :hi AND event_type IN "
        "('tool_execution_completed', 'tool_use_complete') AND (" + prefilter + ") ORDER BY id"
    ), params):
        p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
        for sha, subject in extract_commits(etype, p):
            commit_rows.append({
                "event_id": eid, "thread_id": tid, "sha": sha, "subject": subject,
                "repo": cwds.get(tid), "occurred_at": str(occurred_at) if occurred_at else None,
            })

    if path_rows:
        s.execute(sa_text(
            "INSERT INTO event_paths "
            "(event_id, thread_id, path, basename, op, tool_name, occurred_at) VALUES "
            "(:event_id, :thread_id, :path, :basename, :op, :tool_name, :occurred_at)"
        ), path_rows)
    if commit_rows:
        s.execute(sa_text(
            "INSERT INTO event_commits (event_id, thread_id, sha, subject, repo, occurred_at) "
            "VALUES (:event_id, :thread_id, :sha, :subject, :repo, :occurred_at)"
        ), commit_rows)
    return len(path_rows), len(commit_rows)


def refresh_code_index(
    *, max_batches: Optional[int] = None, session: Optional[Session] = None
) -> dict:
    """Bring the path + commit projections up to date with the event log.

    Idempotent and cheap when current (one indexed ``MAX(id)`` read, then a no-op).
    The first call on a fresh archive pays the backfill; ``max_batches`` bounds one
    invocation so a caller on a latency budget (the MCP server's lazy top-up) can
    make progress without owning the whole walk — the cursor makes the rest of it
    the next call's problem rather than lost work.

    Each batch commits its rows and its cursor advance together, so a kill leaves a
    consistent watermark rather than a window folded twice.
    """
    own = session is None
    with use_session(session) as s:
        upto = s.execute(sa_text("SELECT MAX(id) FROM events")).scalar()
        if upto is None:
            return {"paths": 0, "commits": 0, "through": 0, "done": True}
        upto = int(upto)
        through, version = _read_cursor(s)
        if version != PROJECTION_VERSION or through > upto:
            _reset(s)
            through = 0
        if own:
            s.commit()
        if through >= upto:
            return {"paths": 0, "commits": 0, "through": through, "done": True}
        cwds = _thread_cwds(s)

    paths = commits = batches = 0
    while through < upto:
        if max_batches is not None and batches >= max_batches:
            break
        hi = min(through + _WINDOW, upto)
        with use_session(session) as s:
            n_paths, n_commits = _fold_window(s, through, hi, cwds)
            s.execute(sa_text(
                "UPDATE code_cursor SET through_event_id = :hi, projection_version = :v "
                "WHERE id = 1"
            ), {"hi": hi, "v": PROJECTION_VERSION})
            if own:
                s.commit()
        paths += n_paths
        commits += n_commits
        batches += 1
        through = hi

    if paths or commits:
        logger.info(
            "code index: folded %d path row(s), %d commit(s) through event %d",
            paths, commits, through,
        )
    return {"paths": paths, "commits": commits, "through": through, "done": through >= upto}


def rebuild_code_index(session: Optional[Session] = None) -> dict:
    """Drop and re-derive both projections from the event log — the code-axis half
    of ``reindex``, and the heal for an index whose rows predate a rules change."""
    with use_session(session) as s:
        _read_cursor(s)  # ensure the row exists before resetting it
        _reset(s)
        if session is None:
            s.commit()
    return refresh_code_index(session=session)


def code_index_status(session: Optional[Session] = None) -> dict:
    """Counts + freshness for ``thread_archive status`` and ``verify``."""
    with use_session(session) as s:
        through, version = _read_cursor(s)
        paths = s.execute(sa_text("SELECT count(*) FROM event_paths")).scalar() or 0
        distinct = s.execute(sa_text("SELECT count(DISTINCT path) FROM event_paths")).scalar() or 0
        commits = s.execute(sa_text("SELECT count(*) FROM event_commits")).scalar() or 0
        upto = s.execute(sa_text("SELECT MAX(id) FROM events")).scalar() or 0
        if session is None:
            s.commit()
    return {
        "paths": int(paths), "distinct_paths": int(distinct), "commits": int(commits),
        "through_event_id": through, "max_event_id": int(upto),
        # Ordinary lag, not a fault: with a live watcher the cursor trails the log
        # between maintenance passes, and every read path tops the fold up before
        # querying — so a *query-time* gap is the only one that hides an answer.
        "pending": max(0, int(upto) - through),
        "projection_version": version, "current": through >= int(upto),
    }


# ── Path matching ────────────────────────────────────────────────────────────


def _like_escape(value: str) -> str:
    """Escape LIKE wildcards. Load-bearing here: ``_retrieval/rank.py`` is a
    perfectly ordinary path and ``_`` is a LIKE wildcard, so an unescaped pattern
    silently matches a superset."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def path_predicate(pattern: str, params: dict, *, prefix: str = "") -> str:
    """SQL matching ``pattern`` against ``event_paths``, appending bound params.

    Four shapes, distinguished by form alone — the caller never says which:

    - a **bare name** (``rank.py``) → the indexed ``basename`` equality, the cheap
      common case;
    - a **partial path** (``_retrieval/rank.py``) → basename equality *and* a suffix
      LIKE, so the index still cuts first;
    - an **absolute path** (``/repo/src``) → that file, or that directory's subtree;
    - a **glob** (``*.py``, ``src/**/rank.py``) → GLOB against the whole path.

    ``prefix`` qualifies the column names for a joined query (``p.``).
    """
    pat = pattern.strip().rstrip()
    col_path, col_base = prefix + "path", prefix + "basename"
    if not pat:
        return "1 = 0"
    if any(ch in pat for ch in "*?["):
        # A glob without a leading anchor is matched against any path tail, which is
        # what "*.py" plainly means to whoever typed it.
        params["cg"] = pat if pat.startswith(("/", "*")) else "*" + pat
        return col_path + " GLOB :cg"
    if pat.endswith("/"):
        params["cg"] = pat.rstrip("/") + "/*"
        return col_path + " GLOB :cg"
    if pat.startswith("/") or pat.startswith("~"):
        params["cp"] = pat
        params["cg"] = pat + "/*"
        return "(" + col_path + " = :cp OR " + col_path + " GLOB :cg)"
    if "/" in pat:
        tail = pat.lstrip("./")
        params["cs"] = "%/" + _like_escape(tail)
        params["cd"] = "%/" + _like_escape(tail) + "/%"
        clause = ("(" + col_path + " LIKE :cs ESCAPE '\\' OR "
                  + col_path + " LIKE :cd ESCAPE '\\')")
        base = basename_of(tail)
        if "." in base:
            # A file, so the basename index can do the cutting before the scan.
            params["cb"] = base
            return "(" + col_base + " = :cb AND " + clause + ")"
        return clause
    params["cb"] = pat
    return col_base + " = :cb"


def op_predicate(ops: Optional[Iterable[str]], params: dict, *, prefix: str = "") -> str:
    """``op IN (...)`` for the requested ops, or a tautology when unfiltered — so
    callers can always ``AND`` it in without branching on emptiness."""
    ops = [o for o in (ops or ()) if o in ALL_OPS]
    if not ops:
        return "1 = 1"
    params.update({"cop" + str(i): o for i, o in enumerate(ops)})
    names = ", ".join(":cop" + str(i) for i in range(len(ops)))
    return prefix + "op IN (" + names + ")"


# ── Queries ──────────────────────────────────────────────────────────────────


def blame_path(
    pattern: str,
    *,
    ops: Optional[Iterable[str]] = None,
    limit: int = 20,
    since: Optional[str] = None,
    until: Optional[str] = None,
    sources: Optional[list[str]] = None,
    agents: str = "exclude",
    session: Optional[Session] = None,
) -> dict:
    """The conversations that touched ``pattern``, most-recently-active first.

    Each thread carries its op tally (``edit: 3, read: 12``), its first and last
    touch, the distinct paths it matched, and the event id of its strongest, newest
    touch — the anchor a caller opens with ``thread_read(around_event=...)``. Ranking
    is by recency of the *strongest* op present, so the session that edited the file
    last quarter outranks the one that grepped past it yesterday.
    """
    params: dict = {"lim": max(1, min(int(limit), 2000))}
    where = [path_predicate(pattern, params, prefix="p."),
             op_predicate(ops, params, prefix="p.")]
    if since:
        where.append("p.occurred_at >= :since")
        params["since"] = resolve_relative_date(since)
    if until:
        where.append("p.occurred_at <= :until")
        params["until"] = resolve_relative_date(until)
    if sources:
        params.update({"csrc" + str(i): v for i, v in enumerate(sources)})
        names = ", ".join(":csrc" + str(i) for i in range(len(sources)))
        where.append("t.source IN (" + names + ")")
    if agents == "exclude":
        where.append("t.thread_type != 'system'")
    elif agents == "only":
        where.append("t.thread_type = 'system'")
    where.append("NOT t.exclude_from_search")
    clause = " AND ".join(where)

    with use_session(session) as s:
        rows = s.execute(sa_text(
            "SELECT p.thread_id, t.title, t.source, t.thread_type, p.op, p.path, "
            "       count(*) AS n, min(p.occurred_at) AS first_at, "
            "       max(p.occurred_at) AS last_at, max(p.event_id) AS last_event "
            "FROM event_paths p JOIN threads t ON t.id = p.thread_id "
            "WHERE " + clause + " "
            "GROUP BY p.thread_id, p.op, p.path"
        ), params).all()

        threads: dict[str, dict] = {}
        path_totals: dict[str, int] = {}
        for tid, title, source, ttype, op, path, n, first_at, last_at, last_event in rows:
            entry = threads.setdefault(tid, {
                "thread_id": tid, "title": title, "source": source,
                "thread_type": ttype, "ops": {}, "paths": set(),
                "first": first_at, "last": last_at, "event_id": last_event,
                "_rank_op": None, "_rank_at": None,
            })
            entry["ops"][op] = entry["ops"].get(op, 0) + int(n)
            entry["paths"].add(path)
            if first_at and (entry["first"] is None or first_at < entry["first"]):
                entry["first"] = first_at
            if last_at and (entry["last"] is None or last_at > entry["last"]):
                entry["last"] = last_at
            # The anchor event is the newest occurrence of the strongest op the
            # thread performed — opening on an edit beats opening on a grep.
            strength = ALL_OPS.index(op)
            if entry["_rank_op"] is None or strength < entry["_rank_op"] or (
                strength == entry["_rank_op"] and (last_at or "") > (entry["_rank_at"] or "")
            ):
                entry["_rank_op"], entry["_rank_at"] = strength, last_at
                entry["event_id"] = last_event
            path_totals[path] = path_totals.get(path, 0) + int(n)

    out = []
    for entry in threads.values():
        # A repo-wide pattern puts hundreds of files under a single thread; the
        # sample is bounded and the true count travels beside it, so a truncated
        # list never reads as the whole answer.
        entry["n_paths"] = len(entry["paths"])
        entry["paths"] = sorted(entry["paths"])[:_PATH_SAMPLE]
        rank_op = entry.pop("_rank_op")
        entry["top_op"] = ALL_OPS[rank_op] if rank_op is not None else None
        entry.pop("_rank_at", None)
        out.append(entry)
    # Strongest op first, then most recent — "who changed this" before "who read it".
    out.sort(key=lambda e: (ALL_OPS.index(e["top_op"]) if e["top_op"] else 99,
                            _neg_key(e["last"])))
    result = {
        "pattern": pattern,
        "matched_paths": sorted(path_totals, key=lambda p: -path_totals[p])[:_PATH_SAMPLE],
        "total_paths": len(path_totals),
        "total_threads": len(out),
        "threads": out[: params["lim"]],
    }
    if not out:
        # An empty answer has two very different meanings — "nobody touched it" and
        # "the projection hasn't reached those events yet" — and only the cursor can
        # tell them apart. Paid only on a miss, which is the one time it matters.
        result["index_current"] = code_index_status(session=session)["current"]
    return result


def _neg_key(value: Optional[str]) -> tuple:
    """Sort key that puts the newest timestamp first inside an ascending sort
    (strings have no negation, and mixing reverse= across two keys does not)."""
    return (0, [-ord(c) for c in value]) if value else (1, [])


def thread_files(
    thread_id: str,
    *,
    ops: Optional[Iterable[str]] = None,
    limit: int = 100,
    session: Optional[Session] = None,
) -> dict:
    """The files one conversation touched — the inverse lookup, for "what did this
    session actually change"."""
    params: dict = {"tid": thread_id, "lim": max(1, min(int(limit), 1000))}
    clause = "p.thread_id = :tid AND " + op_predicate(ops, params, prefix="p.")
    with use_session(session) as s:
        rows = s.execute(sa_text(
            "SELECT p.path, p.op, count(*) AS n, max(p.event_id) AS last_event, "
            "       max(p.occurred_at) AS last_at "
            "FROM event_paths p WHERE " + clause + " GROUP BY p.path, p.op"
        ), params).all()
    files: dict[str, dict] = {}
    for path, op, n, last_event, last_at in rows:
        entry = files.setdefault(path, {"path": path, "ops": {}, "event_id": last_event,
                                        "last": last_at})
        entry["ops"][op] = entry["ops"].get(op, 0) + int(n)
        if (last_at or "") > (entry["last"] or ""):
            entry["last"], entry["event_id"] = last_at, last_event
    ranked = sorted(
        files.values(),
        key=lambda e: (min((ALL_OPS.index(o) for o in e["ops"]), default=99), _neg_key(e["last"])),
    )
    return {"thread_id": thread_id, "total_files": len(ranked), "files": ranked[: params["lim"]]}


# ── Commits ──────────────────────────────────────────────────────────────────


def _commit_rows(s: Session, sha: str) -> list[dict]:
    """Recorded commits matching ``sha`` in either abbreviation direction — git
    prints 7 characters and callers paste 40, so neither side can assume it holds
    the longer string."""
    rows = s.execute(sa_text(
        "SELECT c.event_id, c.thread_id, c.sha, c.subject, c.repo, c.occurred_at, "
        "       t.title, t.source "
        "FROM event_commits c JOIN threads t ON t.id = c.thread_id "
        "WHERE c.sha = :sha OR c.sha GLOB :pfx OR :sha GLOB c.sha || '*' "
        "ORDER BY c.occurred_at DESC LIMIT 20"
    ), {"sha": sha, "pfx": sha + "*"}).all()
    return [
        {"event_id": eid, "thread_id": tid, "sha": found, "subject": subject,
         "repo": repo, "occurred_at": oa, "title": title, "source": source}
        for eid, tid, found, subject, repo, oa, title, source in rows
    ]


def _git(args: list[str], cwd: str, timeout: float = 5.0) -> Optional[str]:
    """Read-only git, or None. Never a shell; never fatal — an unreadable repo is a
    missing answer, not an error."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _candidate_repos(s: Session, limit: int = 30) -> list[str]:
    """Git roots the archive has seen sessions run in, most-recently-active first.

    The archive already knows the user's repositories — every claude-code thread
    records its working directory — so a bare sha needs no ``repo`` argument in the
    common case.
    """
    rows = s.execute(sa_text(
        "SELECT DISTINCT json_extract(source_metadata, '$.cwd') AS cwd "
        "FROM threads WHERE source_metadata IS NOT NULL AND cwd IS NOT NULL "
        "ORDER BY updated_at DESC LIMIT :lim"
    ), {"lim": limit * 4}).scalars().all()
    roots: list[str] = []
    for cwd in rows:
        root = _git(["rev-parse", "--show-toplevel"], cwd)
        if not root:
            continue
        root = root.strip()
        if root and root not in roots:
            roots.append(root)
            if len(roots) >= limit:
                break
    return roots


def _git_commit_facts(sha: str, repo: str) -> Optional[dict]:
    """``{sha, committed_at, subject, repo, files}`` for ``sha`` in ``repo``, or None."""
    out = _git(["show", "--no-patch", "--format=%H%x00%cI%x00%s", sha], repo)
    if not out:
        return None
    parts = out.strip().split("\x00")
    if len(parts) < 3:
        return None
    names = _git(["show", "--name-only", "--format=", sha], repo) or ""
    files = [line.strip() for line in names.splitlines() if line.strip()]
    return {
        "sha": parts[0], "committed_at": parts[1], "subject": parts[2],
        "repo": repo, "files": files,
    }


def _previous_touch(sha: str, repo: str, files: list[str]) -> tuple[dict[str, str], bool]:
    """``{file → when it was last committed before this}``, and whether the walk
    ran out of history before covering every file.

    This is what bounds a commit's *authorship window*. Without it, "the sessions
    that edited these files before the commit" reaches back over the whole archive:
    a file touched in fifty sessions across a year would credit all fifty to a commit
    that carries one afternoon's work. The changes a commit actually contains are the
    ones made since each of its files was last committed, so that is the floor.

    One ``git log --name-only`` walk backwards from the parent, not a call per file —
    a bulk checkpoint commit can carry hundreds of paths. A file absent from the walk
    (added by this commit, or older than the window) gets no floor: for a newly-added
    path every prior edit to it *is* the work that created it.
    """
    out = _git(["log", "--format=%x00%cI", "--name-only", "-n", str(_HISTORY_WALK),
                sha + "^"], repo, timeout=15.0)
    if not out:
        return {}, False  # a root commit, or no history to walk — no floor anywhere
    wanted, floors = set(files), {}
    walked = 0
    for record in out.split("\x00"):
        if not record.strip():
            continue
        walked += 1
        lines = record.strip().splitlines()
        when = lines[0].strip()
        for name in lines[1:]:
            name = name.strip()
            if name in wanted and name not in floors:
                floors[name] = when
        if len(floors) >= len(wanted):
            break
    return floors, walked >= _HISTORY_WALK and len(floors) < len(wanted)


def blame_commit(
    sha: str,
    *,
    repo: Optional[str] = None,
    limit: int = 20,
    session: Optional[Session] = None,
) -> dict:
    """The conversations a commit is made of.

    A commit is the product of work, and that work is rarely one session's: a bulk
    checkpoint carries days of it, and even an agent's own commit usually lands
    changes it made across several sittings. So the answer is every session that
    **contributed** — the ones whose edits to the commit's files are inside its
    authorship window (after each file was last committed, up to this commit) —
    ranked by how much of the commit they account for.

    The session that *ran* ``git commit`` is one of them, flagged ``committed``,
    not a different answer. Treating it as the answer was the original mistake here:
    wherever a human commits out of band it is nobody, and where an agent commits it
    is usually just the session that typed the command.

    ``resolution`` says what the answer is built from:

    - **contributors** — the commit was read out of git and the window applied. File
      overlap inside the window is strong evidence, not proof, and the caller is told
      so; a contributor that also ran the commit carries direct provenance.
    - **recorded-only** — git could not reach the sha (no repo given, and it is in
      none the archive has seen), but a session's own output shows it creating the
      commit. That session is all that can be said.
    - **unknown** — neither. The sha is in no session's output and no known repo.
    """
    sha = (sha or "").strip().lower()
    if not sha or not all(c in "0123456789abcdef" for c in sha) or len(sha) < 4:
        return {"sha": sha, "resolution": "invalid", "threads": [], "total_threads": 0,
                "note": "a commit sha is 4+ hex characters"}
    limit = max(1, min(int(limit), 100))

    with use_session(session) as s:
        recorded = _commit_rows(s, sha)
        # The committing session names its repo even when the caller didn't: it is
        # the working directory the commit was actually run in.
        repos = [repo] if repo else (
            [r["repo"] for r in recorded if r["repo"]] + _candidate_repos(s)
        )
        facts = None
        for candidate in repos:
            if not candidate:
                continue
            facts = _git_commit_facts(sha, candidate)
            if facts:
                break

        if not facts:
            if recorded:
                return {
                    "sha": sha, "resolution": "recorded-only", "commit": recorded[0],
                    "total_threads": len(recorded),
                    "threads": [{**r, "committed": True, "matched_files": [],
                                 "coverage": None, "ops": {}, "last": r["occurred_at"]}
                                for r in recorded],
                    "note": "this session's output shows it creating the commit, but the "
                            "repository is not reachable from here — so the other sessions "
                            "whose work went into it cannot be identified (pass repo=…)",
                }
            return {
                "sha": sha, "resolution": "unknown", "threads": [], "total_threads": 0,
                "searched_repos": [r for r in repos if r],
                "note": "no session's output recorded this commit, and it is in none "
                        "of the repositories this archive has seen sessions run in",
            }

        ceiling = resolve_relative_date(facts["committed_at"])
        floors, capped = _previous_touch(facts["sha"], facts["repo"], facts["files"])
        contributors: dict[str, dict] = {}
        for rel in facts["files"][:_COMMIT_FILE_CAP]:
            absolute = normalize_path(rel, facts["repo"])
            if not absolute:
                continue
            params: dict = {"p": absolute, "hi": ceiling}
            floor = floors.get(rel)
            window = ""
            if floor:
                params["lo"] = resolve_relative_date(floor)
                window = " AND p.occurred_at > :lo"
            for tid, title, source, op, n, last_at, last_event in s.execute(sa_text(
                "SELECT p.thread_id, t.title, t.source, p.op, count(*) AS n, "
                "       max(p.occurred_at) AS last_at, max(p.event_id) AS last_event "
                "FROM event_paths p JOIN threads t ON t.id = p.thread_id "
                "WHERE p.path = :p AND p.occurred_at <= :hi" + window
                + "  AND p.op IN ('edit', 'write', 'delete') AND t.thread_type != 'system' "
                "GROUP BY p.thread_id, p.op"
            ), params).all():
                entry = contributors.setdefault(tid, {
                    "thread_id": tid, "title": title, "source": source,
                    "matched_files": set(), "ops": {}, "last": last_at,
                    "event_id": last_event, "committed": False,
                })
                entry["matched_files"].add(rel)
                entry["ops"][op] = entry["ops"].get(op, 0) + int(n)
                if (last_at or "") > (entry["last"] or ""):
                    entry["last"], entry["event_id"] = last_at, last_event
        # The session that ran the commit belongs in the list whether or not it
        # edited anything — it may have committed another session's work, which is
        # exactly the case that made "the committer is the author" wrong.
        for row in recorded:
            entry = contributors.setdefault(row["thread_id"], {
                "thread_id": row["thread_id"], "title": row["title"],
                "source": row["source"], "matched_files": set(), "ops": {},
                "last": row["occurred_at"], "event_id": row["event_id"],
                "committed": False,
            })
            entry["committed"] = True

    total = len(facts["files"]) or 1
    ranked = []
    for entry in contributors.values():
        entry["matched_files"] = sorted(entry["matched_files"])
        entry["coverage"] = round(len(entry["matched_files"]) / total, 3)
        ranked.append(entry)
    # Most of the commit first; the committing session breaks ties, then recency.
    ranked.sort(key=lambda e: (-len(e["matched_files"]), not e["committed"],
                               _neg_key(e["last"])))
    note = ("sessions whose edits to this commit's files fall inside its "
            "authorship window — evidence of contribution, not proof")
    if capped:
        note += (f"; history older than {_HISTORY_WALK} commits was not walked, "
                 "so some files have no lower bound and may over-credit")
    return {
        "sha": sha, "resolution": "contributors", "commit": facts,
        "committed_by": [e["thread_id"] for e in ranked if e["committed"]],
        "total_threads": len(ranked), "threads": ranked[:limit],
        "window_capped": capped, "note": note,
    }


def path_scope_sql(pattern: str, params: dict, *, column: str = "thread_id") -> str:
    """A ``<column> IN (...)`` scope for the search layer: the threads that touched
    ``pattern``. Thread-granular on purpose — the useful composition is "search X
    among the conversations that worked on this file", not "find events that are
    both a path row and a text match". A subquery rather than a resolved id list, so
    a pattern matching thousands of threads needs no bound-parameter cap."""
    return (column + " IN (SELECT DISTINCT thread_id FROM event_paths WHERE "
            + path_predicate(pattern, params) + ")")


__all__ = [
    "PROJECTION_VERSION",
    "TOUCH_OPS",
    "ALL_OPS",
    "refresh_code_index",
    "rebuild_code_index",
    "code_index_status",
    "blame_path",
    "blame_commit",
    "thread_files",
    "path_predicate",
    "op_predicate",
    "path_scope_sql",
]
