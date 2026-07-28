"""Retrieval at a terminal: `thread-archive search` and `thread-archive read`.

These verbs are the supported half of the CLI — the MCP tools with a shell in
front of them (thread_archive/_tools.py is the one implementation). So what is
tested here is the door, not the engine: that flags reach the tool unmangled,
that the text is the tool's own, and that a shell can read the outcome off the
exit code. Ranking, scope defaults, and rendering are covered against the MCP
door in test_mcp.py; duplicating them here would only pin the shared code twice.
"""

from __future__ import annotations

import json

import pytest

from thread_archive import _api as ta
from thread_archive import _tools
from thread_archive._retrieval import usage
from thread_archive.cli import main

from .helpers import cc_assistant, cc_user, write_jsonl


@pytest.fixture
def seeded(archive_home, tmp_path):
    """A two-thread archive: one about migrations, one about the parser."""
    for name, ask, answer in (
        ("mig", "how do we handle the schema migration", "run migrate, then reindex"),
        ("parse", "the cursor parser drops tool calls", "the payload shape changed"),
    ):
        f = tmp_path / f"{name}.jsonl"
        write_jsonl(f, [cc_user(name, ask), cc_assistant(name, answer)])
        ta.import_path(f)
    ta.checkpoint()
    return archive_home


def _thread_id(query: str) -> str:
    return ta.search(query)[0]["thread_id"]


def _ledger(home) -> list[dict]:
    path = home / usage.LEDGER_FILE
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── search ───────────────────────────────────────────────────────────────────

def test_search_prints_the_tools_own_answer(seeded, capsys) -> None:
    """The verb renders exactly what the tool returns — same header, same notes,
    same hits. Asserted by comparing against the tool call itself: any formatting
    the CLI invented on the side would show up here as a difference."""
    assert main(["search", "schema migration", "--limit", "3", "--home", str(seeded)]) == 0
    printed = capsys.readouterr().out

    expected = _tools.thread_search("schema migration", limit=3)
    assert printed == expected + "\n"
    assert "schema migration" in printed


def test_search_with_no_query_browses(seeded, capsys) -> None:
    """An omitted query is a browse — one row per thread, newest first — so the
    positional stays optional rather than argparse-required."""
    assert main(["search", "--home", str(seeded)]) == 0
    out = capsys.readouterr().out
    assert "how do we handle the schema migration" in out  # thread titles, not hits
    assert "the cursor parser drops tool calls" in out


def test_search_flags_reach_the_engine(seeded, capsys) -> None:
    """The structural filters are the reason this verb has 20-odd flags: each one
    has to arrive at the engine spelled the way the tool spells it."""
    home = str(seeded)
    # --source names a provider nobody imported → no hits; the real one → hits.
    assert main(["search", "migration", "--source", "chatgpt", "--home", home]) == 0
    assert "No results" in capsys.readouterr().out
    assert main(["search", "migration", "--source", "claude-code,cursor", "--home", home]) == 0
    assert "schema migration" in capsys.readouterr().out

    # --content-type narrows to one type: the assistant's answer, not the ask.
    assert main(["search", "migration", "--content-type", "text", "--home", home]) == 0
    typed = capsys.readouterr().out
    assert "run migrate, then reindex" in typed and "· text ·" in typed

    # --thread-id scopes to one conversation: the parser thread is reachable
    # until the scope says otherwise, and then it isn't.
    assert main(["search", "parser", "--home", home]) == 0
    assert "the cursor parser drops tool calls" in capsys.readouterr().out
    assert main(["search", "parser", "--thread-id", _thread_id("migration"),
                 "--home", home]) == 0
    assert "No results" in capsys.readouterr().out


def test_search_output_linkable_is_pipeable_json(seeded, capsys) -> None:
    """`--output linkable` is the scripting shape, so stdout must be the JSON and
    nothing else — no banner, no progress line."""
    assert main(["search", "migration", "--output", "linkable", "--home", str(seeded)]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and all("event_id" in r and "thread_id" in r for r in rows)


def test_search_rejects_an_out_of_contract_filter(seeded, capsys) -> None:
    """The engine names the values that work; the verb hands that message to
    stderr and exits 2, so a script sees a bad flag as a failure rather than as a
    search that found nothing."""
    assert main(["search", "x", "--group", "sideways", "--home", str(seeded)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "group must be" in captured.err


# ── read ─────────────────────────────────────────────────────────────────────

def test_read_replays_a_thread(seeded, capsys) -> None:
    assert main(["read", _thread_id("migration"), "--home", str(seeded)]) == 0
    out = capsys.readouterr().out
    assert "[USER" in out and "how do we handle the schema migration" in out


def test_read_mode_and_summary_flags(seeded, capsys) -> None:
    """`--mode chat` folds in the assistant's answer (the default user view drops
    it), and a bare `--summary` is the TOC rather than an error about a missing
    value."""
    home = str(seeded)
    thread = _thread_id("migration")
    assert main(["read", thread, "--mode", "chat", "--home", home]) == 0
    assert "run migrate, then reindex" in capsys.readouterr().out

    assert main(["read", thread, "--summary", "--home", home]) == 0
    toc = capsys.readouterr().out
    assert "(2 messages)" in toc and "| user |" in toc


def test_read_takes_a_session_uuid_and_an_event_anchor(seeded, capsys) -> None:
    """The three ref shapes resolve on this door too, and --around-event opens a
    search hit in its turn — the pair that makes `search` → `read` a real loop."""
    home = str(seeded)
    hit = ta.search("migration")[0]
    assert main(["read", hit["thread_id"], "--around-event", str(hit["event_id"]),
                 "--context-turns", "0", "--home", home]) == 0
    assert f"match:{hit['event_id']}" in capsys.readouterr().out

    from thread_archive._store import get_session
    from thread_archive._store.models import Thread

    with get_session() as s:
        source_id = s.get(Thread, hit["thread_id"]).source_id
    assert main(["read", source_id, "--home", home]) == 0
    assert "schema migration" in capsys.readouterr().out


def test_read_of_an_unknown_ref_exits_nonzero(seeded, capsys) -> None:
    """A typo'd id must be distinguishable from a thread that is simply short —
    the text says so for a person, the exit code for a script."""
    assert main(["read", "no-such-thread", "--home", str(seeded)]) == 1
    assert "not found" in capsys.readouterr().out


# ── the shared implementation ────────────────────────────────────────────────

def test_the_two_doors_answer_identically(seeded) -> None:
    """The point of one implementation: what the MCP tool serves an agent and what
    the verb prints are the same string, filters and all."""
    from thread_archive._mcp.server import thread_read, thread_search

    over_mcp = thread_search("parser", limit=5, group="none")
    over_cli = _tools.thread_search("parser", limit=5, group="none")
    assert over_mcp == over_cli

    thread = _thread_id("parser")
    assert thread_read(thread, mode="chat") == _tools.thread_read(thread, mode="chat")


def test_cli_calls_are_marked_in_the_usage_ledger(seeded, capsys) -> None:
    """Both doors log to the retrieval-usage ledger — it is the sampling frame the
    quality evals draw from, and an operator's terminal queries are a different
    population from an agent's. Only the CLI's rows carry the marker: MCP records
    keep exactly the shape they have always had."""
    main(["search", "migration", "--home", str(seeded)])
    main(["read", _thread_id("migration"), "--home", str(seeded)])
    _tools.thread_search("migration")  # the MCP default surface

    searched, was_read, over_mcp = _ledger(seeded)[-3:]
    assert (searched["kind"], searched["surface"]) == ("search", "cli")
    assert (was_read["kind"], was_read["surface"]) == ("read", "cli")
    assert over_mcp["kind"] == "search" and "surface" not in over_mcp


def test_the_surface_marker_does_not_leak_past_the_call(seeded) -> None:
    """`serving` is scoped, not a process-wide flag: a CLI call inside a process
    that also serves MCP (the tests, an embedded caller) must not relabel the
    calls after it."""
    assert _tools._served_by() is None
    main(["search", "migration", "--home", str(seeded)])
    assert _tools._served_by() is None
