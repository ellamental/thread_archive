"""The code axis: paths touched, commits produced, and the lookups over them.

Three layers, tested apart: the pure extractor (a payload → the files it named,
across every provider spelling), the cursor fold that turns those into the
``event_paths`` / ``event_commits`` projections, and the queries the ``path=`` /
``commit=`` scopes on ``thread_search`` and ``thread_read(summary='files')`` are
built on.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

import pytest

from thread_archive import _api as ta
from thread_archive._retrieval import code, format_results
from thread_archive._retrieval._paths import (
    command_cwd,
    extract_commits,
    extract_paths,
    normalize_path,
    tool_op,
)
from thread_archive._store import Event, Thread, get_session

# ── extraction ───────────────────────────────────────────────────────────────


def _call(tool: str, tool_input, cwd="/proj"):
    return extract_paths("tool_use_complete", {"tool_name": tool, "input": tool_input}, cwd=cwd)


@pytest.mark.parametrize(
    "tool, tool_input, expected",
    [
        # Claude Code
        ("Edit", {"file_path": "src/a.py", "old_string": "x"}, [("/proj/src/a.py", "edit")]),
        ("Write", {"file_path": "/abs/b.py", "content": "x"}, [("/abs/b.py", "write")]),
        ("Read", {"file_path": "src/c.py"}, [("/proj/src/c.py", "read")]),
        ("NotebookEdit", {"notebook_path": "nb.ipynb"}, [("/proj/nb.ipynb", "edit")]),
        ("Grep", {"pattern": "x", "path": "src/"}, [("/proj/src", "search")]),
        # Cursor
        ("search_replace", {"file_path": "/p/d.py", "old_string": "a"}, [("/p/d.py", "edit")]),
        ("delete_file", {"path": "/p/e.py"}, [("/p/e.py", "delete")]),
        ("ripgrep_raw_search", {"path": "/p/f.py", "pattern": "q"}, [("/p/f.py", "search")]),
        # Grok / cloth
        ("read_file", {"target_file": "README.md", "limit": 100}, [("/proj/README.md", "read")]),
        ("read_file", {"path": "/p/g.ts"}, [("/p/g.ts", "read")]),
        # A tool that names no files at all
        ("TodoWrite", {"todos": [{"content": "x"}]}, []),
        ("WebFetch", {"url": "https://example.com/a.py"}, []),
    ],
)
def test_extract_paths_per_provider_spelling(tool, tool_input, expected):
    """One act, many spellings: the same edit arrives as ``Edit``, ``search_replace``,
    or ``edit_file`` depending on the harness, and all three must land on one op."""
    assert _call(tool, tool_input) == expected


def test_mcp_prefixed_tool_names_reduce_to_their_verb():
    assert tool_op("mcp__server__read_file") == "read"
    assert tool_op("mcp_thread-archive_read_file") == "read"
    assert tool_op("Read") == "read"
    assert tool_op("AskUserQuestion") is None


def test_apply_patch_envelope_yields_a_path_per_verb():
    body = (
        "*** Begin Patch\n"
        "*** Update File: backend/routes/system.py\n@@\n-a\n+b\n"
        "*** Add File: backend/new.py\n+x\n"
        "*** Delete File: backend/old.py\n"
        "*** End Patch\n"
    )
    assert _call("apply_patch", body) == [
        ("/proj/backend/routes/system.py", "edit"),
        ("/proj/backend/new.py", "write"),
        ("/proj/backend/old.py", "delete"),
    ]


def test_unified_diff_headers_are_the_fallback():
    body = "--- a/src/x.py\n+++ b/src/x.py\n@@\n-a\n+b\n"
    assert _call("apply_patch", {"patch": body}) == [("/proj/src/x.py", "edit")]


def test_diff_ignores_dev_null_side_of_an_add():
    body = "--- /dev/null\n+++ b/src/new.py\n@@\n+a\n"
    assert _call("apply_patch", {"diff": body}) == [("/proj/src/new.py", "edit")]


def test_shell_commands_yield_only_extension_bearing_tokens():
    """A command line is mostly flags and prose. Without the extension filter the
    ``run`` op would record every regex and URL fragment in the corpus as a file."""
    got = _call("Bash", {"command": "grep -rn 'def foo' src/a.py | head -20 && ls -la"})
    assert got == [("/proj/src/a.py", "run")]


def test_shell_command_cd_moves_the_resolution_base():
    """``cd lab && pytest tests/x.py`` resolves against lab/, not the session dir —
    otherwise the row names a path that never existed."""
    got = _call("Bash", {"command": "cd archive && .venv/bin/pytest tests/test_a.py -q"})
    assert got == [("/proj/archive/tests/test_a.py", "run")]
    assert command_cwd("cd /tmp; cat a.py", "/proj") == "/tmp"
    assert command_cwd("echo hi", "/proj") == "/proj"
    assert command_cwd("cd --help", "/proj") == "/proj"


def test_shell_paths_are_capped_per_command():
    cmd = " ".join(f"f{i}.py" for i in range(50))
    assert len(_call("Bash", {"command": cmd})) == 8


def test_codex_exec_command_shape():
    got = extract_paths(
        "tool_use_complete",
        {"tool_name": "exec_command", "input": {"cmd": "python scripts/build.py"}},
        cwd="/proj",
    )
    assert got == [("/proj/scripts/build.py", "run")]


def test_build_and_dependency_noise_is_dropped():
    assert _call("Read", {"file_path": "node_modules/react/index.js"}) == []
    assert _call("Read", {"file_path": "/p/.venv/lib/site-packages/x.py"}) == []
    assert _call("Read", {"file_path": "__pycache__/x.pyc"}) == []


def test_normalize_path_collapses_and_anchors():
    assert normalize_path("src/../lib/a.py", "/proj") == "/proj/lib/a.py"
    assert normalize_path("~/notes.md", "/proj") == "~/notes.md"  # symbolic, never guessed
    assert normalize_path("C:\\code\\a.py", None) == "C:/code/a.py"
    assert normalize_path("'/quoted/a.py'", None) == "/quoted/a.py"


@pytest.mark.parametrize("raw", [
    "", "   ", None, 42,                # nothing usable
    "https://x.dev/a.py",               # a URL is not a path
    ".", "..", "/", "-",                # not a file reference
    "x" * 500,                          # implausible length
    "a\nb.py", "a\x00b.py",             # not a single path
    "src/../..",                        # collapses to nothing anchorable
])
def test_normalize_path_rejects_non_paths(raw):
    assert normalize_path(raw, None) is None


def test_canonical_tool_of_nothing():
    from thread_archive._retrieval._paths import canonical_tool

    assert canonical_tool(None) == "" and canonical_tool("") == ""


def test_path_lists_and_nested_shapes():
    assert _call("Read", {"paths": ["a.py", "b.py"]}) == [
        ("/proj/a.py", "read"), ("/proj/b.py", "read")]
    assert _call("Read", {"files": [{"file_path": "c.py"}, 7]}) == [("/proj/c.py", "read")]


def test_a_command_list_and_a_flag_value_are_both_scanned():
    assert _call("Bash", {"commands": ["cat a.py", 3, "cat b.py"]}) == [
        ("/proj/a.py", "run"), ("/proj/b.py", "run")]
    assert _call("Bash", {"command": "pytest --cov-report=json:out.json"}) == [
        ("/proj/out.json", "run")]


def test_apply_patch_envelope_under_an_input_key():
    body = "*** Begin Patch\n*** Update File: src/a.py\n@@\n"
    assert _call("apply_patch", {"input": body}) == [("/proj/src/a.py", "edit")]


def test_a_bare_string_input_to_a_run_tool_is_scanned():
    assert extract_paths(
        "tool_use_complete", {"tool_name": "exec", "input": "python build.py"}, cwd="/proj",
    ) == [("/proj/build.py", "run")]


def test_paths_per_event_are_capped():
    body = "".join(f"*** Update File: f{i}.py\n" for i in range(100))
    assert len(_call("apply_patch", body)) == 64


def test_an_empty_payload_names_nothing():
    assert extract_paths("tool_use_complete", {}) == []
    assert extract_commits("tool_execution_completed", {}) == []
    assert extract_commits("tool_execution_completed", {"output": "no brackets here"}) == []
    assert extract_commits("user_message_sent", {"output": "[main abc1234] x\n 1 insertion(+)"}) == []


def test_a_commit_repeated_in_one_output_records_once():
    out = ("[main abc1234] x\n 1 file changed, 1 insertion(+)\n"
           "[main abc1234] x\n 1 file changed, 1 insertion(+)\n")
    assert extract_commits("tool_execution_completed", {"output": out}) == [("abc1234", "x")]


def test_tool_results_name_no_paths():
    """Paths come from the call, never the result — a grep's output is full of
    filenames the session did not touch."""
    assert extract_paths("tool_execution_completed", {"output": "src/a.py:1: match"}) == []


def test_duplicate_path_and_op_collapses():
    got = _call("Bash", {"command": "cat a.py && cat a.py"})
    assert got == [("/proj/a.py", "run")]


# ── commit extraction ────────────────────────────────────────────────────────


def test_commit_output_is_recognized_with_its_diffstat():
    out = "[main 31bade5] Fix the ranker\n 3 files changed, 10 insertions(+), 2 deletions(-)\n"
    assert extract_commits("tool_execution_completed", {"output": out}) == [
        ("31bade5", "Fix the ranker")
    ]


@pytest.mark.parametrize("out", [
    "[detached HEAD abc1234] wip\n 1 file changed, 1 insertion(+)\n",
    "[main (root-commit) deadbee] first\n 2 files changed, 5 insertions(+), 0 deletions(-)\n",
])
def test_commit_output_variants(out):
    assert extract_commits("tool_execution_completed", {"output": out})


def test_reading_a_commit_is_not_authoring_one():
    """``git log`` shows a hundred shas the session did not create. Without the
    diffstat corroboration every reader would register as provenance."""
    log = "commit 31bade5abcdef\nAuthor: someone\nDate: today\n\n    [main 31bade5] quoted\n"
    assert extract_commits("tool_execution_completed", {"output": log}) == []


def test_failed_commit_records_nothing():
    out = "[main 31bade5] x\n 1 file changed, 1 insertion(+)\n"
    assert extract_commits("tool_execution_completed", {"output": out, "is_error": True}) == []


# ── the fold ─────────────────────────────────────────────────────────────────


def _tool_event(s, thread, tool, tool_input, day=1, etype="tool_use_complete"):
    e = Event(
        thread_id=thread.id, stream_id="s", event_type=etype,
        payload={"tool_name": tool, "input": tool_input},
        occurred_at=datetime(2026, 1, day, 10, 0, 0, tzinfo=timezone.utc),
    )
    s.add(e)
    return e


def _seed(cwd="/proj", title="t", thread_type="conversation"):
    """A thread plus a handful of tool events, written straight at the store layer."""
    with get_session() as s:
        t = Thread(name=f"n:{title}", title=title, thread_type=thread_type,
                   source="claude-code", source_id=title,
                   source_metadata={"cwd": cwd} if cwd else None)
        s.add(t)
        s.flush()
        _tool_event(s, t, "Edit", {"file_path": "src/rank.py", "old_string": "a"}, day=1)
        _tool_event(s, t, "Read", {"file_path": "src/rank.py"}, day=2)
        _tool_event(s, t, "Read", {"file_path": "src/other.py"}, day=2)
        tid = t.id
        s.commit()
    return tid


def test_fold_populates_both_projections(archive_home):
    ta.open_archive()
    tid = _seed()
    with get_session() as s:
        t = s.get(Thread, tid)
        e = Event(
            thread_id=tid, stream_id="s", event_type="tool_execution_completed",
            payload={"tool_name": "unknown",
                     "output": "[main abc1234] done\n 1 file changed, 1 insertion(+)\n"},
            occurred_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
        s.add(e)
        assert t is not None
        s.commit()

    result = code.refresh_code_index()
    assert result["paths"] == 3
    assert result["commits"] == 1
    status = code.code_index_status()
    assert status["current"] and status["distinct_paths"] == 2


def test_fold_is_idempotent_and_incremental(archive_home):
    ta.open_archive()
    _seed()
    first = code.refresh_code_index()
    assert first["paths"] == 3
    assert code.refresh_code_index()["paths"] == 0  # already through

    with get_session() as s:
        t = s.query(Thread).one()
        _tool_event(s, t, "Write", {"file_path": "src/new.py", "content": "x"}, day=4)
        s.commit()
    assert code.refresh_code_index()["paths"] == 1
    assert code.code_index_status()["paths"] == 4


def test_rows_folded_by_an_older_ruleset_are_rebuilt_not_appended_to(archive_home):
    """The state a live index is in the first time it opens under new extraction
    rules: a full projection stamped with the previous version. Rows written under
    old rules cannot be added to by new ones, so the fold discards and re-derives."""
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    from sqlalchemy import text as sa_text

    with get_session() as s:
        s.execute(sa_text("UPDATE code_cursor SET projection_version = projection_version - 1"))
        s.commit()
    again = code.refresh_code_index()
    assert again["paths"] == 3  # re-derived from zero, not appended
    assert code.code_index_status()["paths"] == 3


def test_a_shrunken_log_rebuilds(archive_home):
    """A cursor past the log's end means a reindex rebuilt it under us; the event
    ids the projection keys on no longer name the same rows."""
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    from sqlalchemy import text as sa_text

    with get_session() as s:
        s.execute(sa_text("UPDATE code_cursor SET through_event_id = 999999"))
        s.commit()
    assert code.refresh_code_index()["paths"] == 3


def test_max_batches_bounds_one_call_without_losing_work(archive_home):
    ta.open_archive()
    _seed()
    partial = code.refresh_code_index(max_batches=0)
    assert partial["done"] is False and partial["paths"] == 0
    assert code.refresh_code_index()["paths"] == 3


def test_rebuild_code_index_rederives(archive_home):
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    assert code.rebuild_code_index()["paths"] == 3
    assert code.code_index_status()["paths"] == 3


def test_a_thread_without_a_recorded_cwd_keeps_relative_paths(archive_home):
    ta.open_archive()
    _seed(cwd=None)
    code.refresh_code_index()
    assert code.blame_path("rank.py")["matched_paths"] == ["src/rank.py"]


# ── blame_path ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pattern", [
    "rank.py",                 # bare name
    "src/rank.py",             # partial path
    "/proj/src/rank.py",       # absolute file
    "/proj/src/",              # directory subtree
    "*.py",                    # glob
    "src/*.py",                # anchored glob
])
def test_blame_path_pattern_shapes(archive_home, pattern):
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    assert code.blame_path(pattern)["total_threads"] == 1


def test_blame_path_underscores_are_not_like_wildcards(archive_home):
    """``_retrieval/rank.py`` is an ordinary path and ``_`` is a LIKE wildcard —
    unescaped, the pattern silently matches a superset."""
    ta.open_archive()
    with get_session() as s:
        t = Thread(name="n:u", title="u", source="claude-code", source_id="u",
                   source_metadata={"cwd": "/proj"})
        s.add(t)
        s.flush()
        _tool_event(s, t, "Edit", {"file_path": "xretrieval/rank.py"}, day=1)
        s.commit()
    code.refresh_code_index()
    assert code.blame_path("_retrieval/rank.py")["total_threads"] == 0


def test_blame_path_reports_ops_and_an_anchor_event(archive_home):
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    thread = code.blame_path("rank.py")["threads"][0]
    assert thread["ops"] == {"edit": 1, "read": 1}
    assert thread["top_op"] == "edit"
    assert thread["event_id"] > 0
    assert thread["paths"] == ["/proj/src/rank.py"]
    assert thread["n_paths"] == 1


def test_a_repo_wide_query_reports_counts_it_cannot_list(archive_home):
    """"Which sessions changed anything in this repo" is a directory pattern, and it
    matches more files than any result should carry — so the samples are bounded and
    the true counts travel beside them."""
    ta.open_archive()
    with get_session() as s:
        t = Thread(name="n:wide", title="wide", source="claude-code", source_id="wide",
                   source_metadata={"cwd": "/repo"})
        s.add(t)
        s.flush()
        for i in range(40):
            _tool_event(s, t, "Edit", {"file_path": f"src/mod{i:02d}.py"}, day=1)
        _tool_event(s, t, "Read", {"file_path": "/elsewhere/other.py"}, day=1)
        s.commit()
    code.refresh_code_index()

    result = code.blame_path("/repo", ops=["edit", "write", "delete"])
    assert result["total_paths"] == 40 and len(result["matched_paths"]) == 25
    assert result["threads"][0]["n_paths"] == 40 and len(result["threads"][0]["paths"]) == 25

    rendered = format_results(ta.search("", path="/repo", path_ops=["edit"]), "")
    assert "40 file(s)" in rendered
    # The trailing-slash form is the same query, and the scope really is a scope.
    assert code.blame_path("/repo/")["total_paths"] == 40


def test_blame_path_ops_filter(archive_home):
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    assert code.blame_path("rank.py", ops=["edit"])["threads"][0]["ops"] == {"edit": 1}
    assert code.blame_path("other.py", ops=["edit"])["total_threads"] == 0


def test_blame_path_ranks_changes_above_looks(archive_home):
    """The session that edited the file outranks the one that only read it, however
    recently the reader looked."""
    ta.open_archive()
    _seed(title="editor")
    with get_session() as s:
        t = Thread(name="n:reader", title="reader", source="claude-code", source_id="reader",
                   source_metadata={"cwd": "/proj"})
        s.add(t)
        s.flush()
        _tool_event(s, t, "Read", {"file_path": "src/rank.py"}, day=28)
        s.commit()
    code.refresh_code_index()
    titles = [t["title"] for t in code.blame_path("rank.py")["threads"]]
    assert titles == ["editor", "reader"]


def test_blame_path_filters_agents_sources_and_time(archive_home):
    ta.open_archive()
    _seed(title="main")
    _seed(title="agentrun", thread_type="system")
    code.refresh_code_index()
    assert code.blame_path("rank.py")["total_threads"] == 1
    assert code.blame_path("rank.py", agents="include")["total_threads"] == 2
    assert code.blame_path("rank.py", agents="only")["total_threads"] == 1
    assert code.blame_path("rank.py", sources=["cursor"])["total_threads"] == 0
    assert code.blame_path("rank.py", until="2025-01-01")["total_threads"] == 0


def test_blame_path_misses_report_nothing_rather_than_everything(archive_home):
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    assert code.blame_path("nope.py")["threads"] == []
    assert code.blame_path("")["threads"] == []


def test_a_miss_says_whether_the_index_had_even_reached_those_events(archive_home):
    """'Nobody touched it' and 'not folded yet' are different answers, and only the
    cursor can tell them apart."""
    ta.open_archive()
    _seed()
    assert code.blame_path("rank.py")["index_current"] is False  # nothing folded yet
    code.refresh_code_index()
    assert code.blame_path("nope.py")["index_current"] is True


def test_blame_path_windows_by_time(archive_home):
    ta.open_archive()
    _seed()  # edited on day 1, read on day 2
    code.refresh_code_index()
    assert code.blame_path("rank.py", since="2026-01-02")["threads"][0]["ops"] == {"read": 1}
    assert code.blame_path("rank.py", since="2026-06-01")["total_threads"] == 0


def test_blame_path_matches_a_directory_by_relative_name(archive_home):
    """A pattern whose last segment has no extension is a directory: it matches the
    subtree, and cannot lean on the basename index to do the cutting."""
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    assert code.blame_path("proj/src")["total_threads"] == 1
    assert code.blame_path("proj/lib")["total_threads"] == 0


# ── thread_files ─────────────────────────────────────────────────────────────


def test_thread_files_is_the_inverse_lookup(archive_home):
    ta.open_archive()
    tid = _seed()
    code.refresh_code_index()
    files = code.thread_files(tid)
    assert files["total_files"] == 2
    assert files["files"][0]["path"] == "/proj/src/rank.py"  # the edited one leads
    assert files["files"][0]["ops"] == {"edit": 1, "read": 1}


# ── blame_commit ─────────────────────────────────────────────────────────────


def _git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


def _seed_commit(sha="abc1234", subject="done", cwd="/proj"):
    """A session whose tool output shows it running ``git commit``."""
    with get_session() as s:
        t = Thread(name=f"n:{sha}:{subject}", title="committer", source="claude-code",
                   source_id=f"{sha}:{subject}", source_metadata={"cwd": cwd})
        s.add(t)
        s.flush()
        s.add(Event(
            thread_id=t.id, stream_id="s", event_type="tool_execution_completed",
            payload={"tool_name": "unknown",
                     "output": f"[main {sha}] {subject}\n 1 file changed, 1 insertion(+)\n"},
            occurred_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        ))
        tid = t.id
        s.commit()
    return tid


def _seed_editor(repo, title, relpath, day=1):
    """A session that edited ``relpath`` inside ``repo``."""
    with get_session() as s:
        t = Thread(name=f"n:{title}", title=title, source="claude-code", source_id=title,
                   source_metadata={"cwd": str(repo)})
        s.add(t)
        s.flush()
        _tool_event(s, t, "Edit", {"file_path": relpath, "old_string": "a"}, day=day)
        tid = t.id
        s.commit()
    return tid


def _stamp_touches(thread_id, when):
    """Move a thread's recorded touches to ``when``.

    Tests need edits on both sides of a real git commit's timestamp, and a git
    commit happens *now* — so the conversation side is what moves. The projection
    is the thing under test, so its own column is the honest place to set it."""
    from sqlalchemy import text as sa_text

    with get_session() as s:
        s.execute(sa_text("UPDATE event_paths SET occurred_at = :w WHERE thread_id = :t"),
                  {"w": when, "t": thread_id})
        s.commit()


def _repo_with_commit(tmp_path, files, message, repo=None, when=None):
    """A real git repository with one more commit on top; returns (path, sha).

    ``when`` (an ISO timestamp) dates the commit. The authorship window is a
    *time* window, so a test that needs edits on either side of it needs commits
    at known times rather than three of them in the same millisecond."""
    repo = repo or (tmp_path / "repo")
    repo.mkdir(exist_ok=True)
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when

    def run(*a):
        subprocess.run(["git", "-C", str(repo), *a], capture_output=True, check=True, env=env)

    if not (repo / ".git").exists():
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
    for name, body in files.items():
        (repo / name).write_text(body)
        run("add", name)
    run("commit", "-qm", message)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return repo, sha


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_the_committing_session_does_not_displace_the_contributors(archive_home, tmp_path):
    """The mistake this replaces: a commit resolved to whoever ran ``git commit``
    and stopped looking. A commit carries work from several sittings, and the
    session that typed the command is often not the one that did it."""
    repo, sha = _repo_with_commit(tmp_path, {"rank.py": "x\n"}, "change the ranker")

    ta.open_archive()
    author = _seed_editor(repo, "the author", "rank.py", day=2)
    committer = _seed_commit(sha=sha[:7], subject="change the ranker", cwd=str(repo))
    code.refresh_code_index()

    result = code.blame_commit(sha, repo=str(repo))
    assert result["resolution"] == "contributors"
    ids = [t["thread_id"] for t in result["threads"]]
    assert author in ids and committer in ids
    assert result["committed_by"] == [committer]
    by_id = {t["thread_id"]: t for t in result["threads"]}
    assert by_id[author]["matched_files"] == ["rank.py"] and by_id[author]["coverage"] == 1.0
    assert by_id[author]["committed"] is False
    # The one that edited the most leads; running the commit is an annotation.
    assert ids[0] == author


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_contributors_are_bounded_by_the_authorship_window(archive_home, tmp_path):
    """Work already committed is not this commit's. Without the per-file floor a
    file edited across a year of sessions would credit every one of them."""
    repo, _first = _repo_with_commit(tmp_path, {"rank.py": "one\n"}, "first",
                                     when="2026-01-10T12:00:00+00:00")
    repo, second = _repo_with_commit(tmp_path, {"rank.py": "two\n"}, "second", repo=repo,
                                     when="2026-01-20T12:00:00+00:00")

    ta.open_archive()
    before = _seed_editor(repo, "already committed", "rank.py", day=1)
    after = _seed_editor(repo, "in this commit", "rank.py", day=28)
    code.refresh_code_index()
    _stamp_touches(before, "2026-01-05 00:00:00.000000")   # before `first` committed it
    _stamp_touches(after, "2026-01-15 00:00:00.000000")    # between the two commits

    ids = [t["thread_id"] for t in code.blame_commit(second, repo=str(repo))["threads"]]
    assert after in ids
    assert before not in ids


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_a_file_added_by_the_commit_has_no_floor(archive_home, tmp_path):
    """Nothing committed it before, so every prior edit to that path is the work
    that created it."""
    repo, sha = _repo_with_commit(tmp_path, {"brand_new.py": "x\n"}, "add it")
    ta.open_archive()
    tid = _seed_editor(repo, "made it", "brand_new.py", day=1)
    code.refresh_code_index()
    _stamp_touches(tid, "2020-01-01 00:00:00.000000")
    assert [t["thread_id"] for t in code.blame_commit(sha, repo=str(repo))["threads"]] == [tid]


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_a_commit_nobody_committed_still_resolves_its_contributors(archive_home, tmp_path):
    """Ella's case, and every repo where a human commits: no session ran the
    command, so 'who committed it' is nobody and the contributors are the answer."""
    repo, sha = _repo_with_commit(tmp_path, {"rank.py": "x\n"}, "checkpoint")
    ta.open_archive()
    tid = _seed_editor(repo, "did the work", "rank.py", day=2)
    code.refresh_code_index()

    result = code.blame_commit(sha, repo=str(repo))
    assert result["committed_by"] == []
    assert [t["thread_id"] for t in result["threads"]] == [tid]

    from thread_archive._mcp.server import _commit_note

    note = _commit_note(result)
    assert "1 contributing session(s)" in note
    assert "committed outside any session" in note


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_a_commit_with_no_contributors_says_so(archive_home, tmp_path):
    repo, sha = _repo_with_commit(tmp_path, {"untouched.py": "x\n"}, "nobody's work")
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    result = code.blame_commit(sha, repo=str(repo))
    assert result["resolution"] == "contributors" and result["threads"] == []


def test_recorded_only_when_the_repository_is_out_of_reach(archive_home):
    """The sha is in a session's output but no reachable repo — so the committing
    session is genuinely all that can be said, and the result says that."""
    ta.open_archive()
    tid = _seed_commit()
    code.refresh_code_index()
    result = code.blame_commit("abc1234")
    assert result["resolution"] == "recorded-only"
    assert [t["thread_id"] for t in result["threads"]] == [tid]
    assert result["threads"][0]["committed"] is True

    from thread_archive._mcp.server import _commit_note

    assert "only the committing session is known" in _commit_note(result)


def test_blame_commit_matches_across_abbreviation_lengths(archive_home):
    """git printed 7 characters; the caller pasted 40. Neither side can assume it
    holds the longer string."""
    ta.open_archive()
    _seed_commit(sha="abc1234")
    code.refresh_code_index()
    assert code.blame_commit("abc1234def567890")["resolution"] == "recorded-only"


def test_blame_commit_rejects_a_non_sha(archive_home):
    ta.open_archive()
    assert code.blame_commit("not-a-sha")["resolution"] == "invalid"
    assert code.blame_commit("ab")["resolution"] == "invalid"


def test_blame_commit_unknown_names_where_it_looked(archive_home):
    """No session recorded the sha and the only directory the archive has seen
    (``/proj``) is not a repository — so there is nowhere left to look, and the
    answer says so rather than returning an empty list that reads like 'nobody'."""
    ta.open_archive()
    _seed()
    code.refresh_code_index()
    result = code.blame_commit("deadbeef")
    assert result["resolution"] == "unknown"
    assert result["threads"] == []
    assert "no session" in result["note"]


def test_git_helpers_fail_soft(archive_home, tmp_path):
    """An unreadable repository is a missing answer, not an exception — a blame
    that raises is worse than one that says it could not find the commit."""
    assert code._git(["rev-parse", "HEAD"], str(tmp_path / "nope")) is None
    assert code._git(["rev-parse", "HEAD"], str(tmp_path)) is None  # not a repo
    assert code._git_commit_facts("deadbeef", str(tmp_path)) is None
    assert code._previous_touch("deadbeef", str(tmp_path), ["a.py"]) == ({}, False)


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_candidate_repos_come_from_the_directories_sessions_ran_in(archive_home, tmp_path):
    """The archive already knows the user's repositories — every session records its
    working directory — so a bare sha needs no ``repo`` argument."""
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    ta.open_archive()
    _seed(cwd=str(repo / "sub"), title="in-repo")
    _seed(cwd="/nowhere", title="not-a-repo")
    with get_session() as s:
        assert code._candidate_repos(s) == [
            subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, check=True).stdout.strip()
        ]


# ── the search filter ────────────────────────────────────────────────────────


def _import_session(tmp_path, name, text, tool_path):
    lines = [
        {"type": "user", "uuid": f"u-{name}", "timestamp": "2026-01-01T10:00:00Z",
         "cwd": "/proj", "message": {"role": "user", "content": text}},
        {"type": "assistant", "uuid": f"a-{name}", "timestamp": "2026-01-01T10:00:05Z",
         "cwd": "/proj",
         "message": {"role": "assistant", "model": "claude-opus-5", "content": [
             {"type": "tool_use", "id": f"t-{name}", "name": "Edit",
              "input": {"file_path": tool_path, "old_string": "a", "new_string": "b"}}]}},
    ]
    f = tmp_path / f"{name}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    ta.import_path(f)


def test_search_path_filter_scopes_a_query(archive_home, tmp_path):
    ta.open_archive()
    _import_session(tmp_path, "a", "tune the retry backoff in the ranker", "src/rank.py")
    _import_session(tmp_path, "b", "tune the retry backoff in the importer", "src/other.py")
    code.refresh_code_index()

    everywhere = ta.search("backoff", limit=10)
    scoped = ta.search("backoff", limit=10, path="rank.py")
    assert len(everywhere) == 2
    assert len(scoped) == 1
    assert scoped[0]["thread_title"] == "tune the retry backoff in the ranker"


def test_empty_query_with_a_path_browses_the_sessions_that_worked_on_it(archive_home, tmp_path):
    ta.open_archive()
    _import_session(tmp_path, "a", "one", "src/rank.py")
    _import_session(tmp_path, "b", "two", "src/unrelated.py")
    code.refresh_code_index()

    assert len(ta.search("", limit=10)) == 2
    assert len(ta.search("", limit=10, path="rank.py")) == 1
    assert ta.search("", limit=10, path="nothing.py") == []


# ── the API + MCP surface ────────────────────────────────────────────────────


def test_api_blame_takes_exactly_one_direction(archive_home):
    ta.open_archive()
    with pytest.raises(ValueError):
        ta.blame()
    with pytest.raises(ValueError):
        ta.blame(path="a.py", commit="abc1234")


def test_api_blame_resolves_a_thread_ref(archive_home):
    ta.open_archive()
    tid = _seed()
    assert ta.blame(thread_id=tid)["total_files"] == 2
    assert ta.blame(thread_id="01NOSUCHTHREADIDATALL0000X")["files"] == []


def test_api_status_reports_the_code_index(archive_home):
    ta.open_archive()
    _seed()
    assert ta.status()["code_pending"] > 0  # nothing folded yet
    ta.code_index()
    status = ta.status()
    assert status["code_paths_indexed"] == 3
    assert status["code_files"] == 2
    assert status["code_current"] is True
    assert status["code_pending"] == 0

    from thread_archive.cli import report_status

    report_status(status)  # the operator line renders both states


def test_the_code_axis_rides_thread_search(archive_home):
    """The merged surface: a path scope is a browse when the query is empty and a
    filter when it isn't — the two modes thread_search already had."""
    from thread_archive._mcp.server import thread_search

    ta.open_archive()
    tid = _seed()
    ta.code_index()

    listed = thread_search(query="", path="rank.py")
    assert "touched this path" in listed and tid in listed
    assert "edit 1 · read 1" in listed
    assert "summary='files'" in listed  # the inverse lookup is signposted

    assert "conversation(s) touched" not in thread_search(query="", path="nope.py")
    assert "edit 1" in thread_search(query="", path="rank.py", path_ops="edit,read")
    assert "read 1" not in thread_search(query="", path="rank.py", path_ops="edit")


def test_a_path_browse_opens_at_the_touch_not_the_thread_tail(archive_home):
    """The anchor is the point of the row: opening a 400-turn session at its tail
    tells you nothing about the edit you asked after."""
    ta.open_archive()
    _seed()
    ta.code_index()
    [row] = ta.search("", path="rank.py")
    anchor = code.blame_path("rank.py")["threads"][0]["event_id"]
    assert row["event_id"] == anchor
    assert row["event_id"] != ta.search("")[0]["event_id"]  # not the newest event
    assert row["_path_ops"] == {"edit": 1, "read": 1}


def test_thread_read_files_summary_is_the_inverse_lookup(archive_home):
    from thread_archive._mcp.server import thread_read

    ta.open_archive()
    tid = _seed()
    out = thread_read(thread_id=tid, summary="files")
    assert "files touched" in out and "/proj/src/rank.py" in out
    assert "edit 1 · read 1" in out

    empty = thread_read(thread_id=_seed_commit(), summary="files")
    assert "No files recorded" in empty
    assert "Unknown summary kind" in thread_read(thread_id=tid, summary="nonsense")


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_commit_scope_notes_what_the_rows_cannot_say(archive_home, tmp_path):
    """The rows are ordinary thread rows either way. Which sessions contributed how
    much of the commit, and which of them ran it, can only ride as a note."""
    from thread_archive._mcp.server import thread_search

    repo, sha = _repo_with_commit(tmp_path, {"rank.py": "x\n"}, "change the ranker")
    ta.open_archive()
    author = _seed_editor(repo, "the author", "rank.py", day=2)
    ta.code_index()

    out = thread_search(query="", commit=sha, repo=str(repo))
    assert "1 contributing session(s)" in out and author in out
    assert "committed outside any session" in out
    assert f"{author} 1/1" in out


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_commit_rows_keep_the_share_ranking_the_note_states(archive_home, tmp_path):
    """The note ranks by share of the commit; if the rows below re-sorted by last
    activity the top row would not be the session the note put first."""
    repo, sha = _repo_with_commit(
        tmp_path, {"a.py": "x\n", "b.py": "x\n", "c.py": "x\n"}, "three files")
    ta.open_archive()
    small = _seed_editor(repo, "touched one", "a.py", day=1)
    big = _seed_editor(repo, "touched all three", "a.py", day=2)
    with get_session() as s:
        t = s.get(Thread, big)
        assert t is not None
        _tool_event(s, t, "Edit", {"file_path": "b.py"}, day=2)
        _tool_event(s, t, "Edit", {"file_path": "c.py"}, day=2)
        s.commit()
    code.refresh_code_index()
    # The smaller contributor is the more recently active one.
    _stamp_touches(big, "2026-01-01 00:00:00.000000")
    _stamp_touches(small, "2026-06-01 00:00:00.000000")

    assert [t["thread_id"] for t in code.blame_commit(sha, repo=str(repo))["threads"]] == [
        big, small]

    from thread_archive._mcp.server import thread_search

    out = thread_search(query="", commit=sha, repo=str(repo))
    assert out.index(big) < out.index(small)
    # And the header must not claim an ordering the rows aren't in.
    assert "in the order the scope ranked them" in out
    assert "browse (no query) — one row per thread, by last activity" not in out

    assert "is not a commit sha" in thread_search(query="", commit="zzzz")
    unknown = thread_search(query="", commit="deadbeef")
    assert "no match" in unknown and "repo=" in unknown


def test_a_wide_path_browse_names_what_it_folded(archive_home):
    """Many spellings of one filename: the row says how many files it stands for
    rather than dropping the count silently."""
    ta.open_archive()
    for i in range(7):
        with get_session() as s:
            t = Thread(name=f"n:{i}", title=f"t{i}", source="claude-code", source_id=f"s{i}",
                       source_metadata={"cwd": f"/proj{i}"})
            s.add(t)
            s.flush()
            _tool_event(s, t, "Edit", {"file_path": f"dir{i}/rank.py"}, day=1 + i)
            s.commit()
    ta.code_index()
    assert len(ta.search("", path="rank.py", limit=50)) == 7
    assert code.blame_path("rank.py")["total_paths"] == 7


def test_reindex_rebuilds_the_code_index(archive_home, tmp_path):
    """The projection is derived from the events, so a fresh index must publish
    with it filled rather than answering 'nobody touched that file'."""
    ta.open_archive()
    _import_session(tmp_path, "a", "hello", "src/rank.py")
    ta.code_index()
    assert code.code_index_status()["paths"] == 1

    result = ta.reindex()
    assert result["code_paths"] == 1
    assert code.blame_path("rank.py")["total_threads"] == 1
