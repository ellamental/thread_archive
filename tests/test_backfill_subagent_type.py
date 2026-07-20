"""The subagent-type backfill: ``source_metadata.agent_type`` is read back off the
on-disk transcripts and stamped onto the subagent threads that predate the importer
reading it.

Two halves, tested apart: the disk scan (which files count as transcripts, what a
torn or half-naming one contributes) and the store pass (which threads are planned,
what preview refuses to write, what a second run finds). The scan resolves
``Path.home()`` on every call, so every test here pins ``$HOME`` at a scripted
projects tree — the real machine's transcripts are never read.
"""

from __future__ import annotations

import json

import pytest

from thread_archive._scripts import backfill_subagent_type as mod
from thread_archive._store import Thread, get_session, init_db
from thread_archive._truth.jsonl_log import _shard_depth, _thread_file, log_dir


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """A throwaway ``$HOME`` carrying an (empty) Claude Code projects tree."""
    home = tmp_path / "fake-home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _transcript(home, lines, *, project="-Users-x-proj", session="sess-1",
                nest="", name="agent-a1.jsonl"):
    """Write one subagent transcript under ``<home>/.claude/projects/…/subagents/``."""
    d = home / ".claude" / "projects" / project / session / "subagents"
    if nest:
        d = d / nest
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def _assistant(agent_id: str, agent_type: str) -> dict:
    """An assistant line of the shape Claude Code writes into a subagent transcript."""
    return {"type": "assistant", "uuid": "a1", "agentId": agent_id,
            "attributionAgent": agent_type,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}


def _seed_thread(name: str, meta: dict | None, *, source: str = "claude-code") -> int:
    with get_session() as s:
        t = Thread(name=name, source=source, thread_type="system", source_metadata=meta)
        s.add(t)
        s.flush()
        tid = t.id
        s.commit()
    return tid


def _meta(tid: int) -> dict:
    with get_session() as s:
        return s.get(Thread, tid).source_metadata or {}


def _run_main(*argv: str) -> int:
    return mod.main(list(argv))


# ── the disk scan ────────────────────────────────────────────────────────────

def test_transcripts_found_at_any_depth_and_run_ledgers_are_not(claude_home) -> None:
    flat = _transcript(claude_home, [_assistant("ag-1", "Explore")])
    nested = _transcript(claude_home, [_assistant("ag-2", "general-purpose")],
                         nest="workflow/run-7", name="agent-deep.jsonl")
    # a workflow's run ledger sits beside the transcripts but is not one
    (nested.parent / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    # a session transcript outside subagents/ is the parent conversation, not an agent
    (claude_home / ".claude" / "projects" / "-Users-x-proj" / "sess-1.jsonl").write_text(
        "{}\n", encoding="utf-8")
    # a stray file at the projects level is not a project dir
    (claude_home / ".claude" / "projects" / "notes.txt").write_text("x", encoding="utf-8")

    assert set(mod._agent_transcripts()) == {flat, nested}


def test_every_claude_dir_is_scanned(claude_home) -> None:
    """A ``.claude`` → ``.claude1`` rename leaves both trees on disk; both are read."""
    a = _transcript(claude_home, [_assistant("ag-1", "Explore")])
    renamed = claude_home / ".claude1" / "projects" / "-Users-x-other" / "s" / "subagents"
    renamed.mkdir(parents=True)
    b = renamed / "agent-b.jsonl"
    b.write_text(json.dumps(_assistant("ag-2", "Plan")) + "\n", encoding="utf-8")

    assert set(mod._agent_transcripts()) == {a, b}
    assert mod.agent_types_on_disk() == {"ag-1": "Explore", "ag-2": "Plan"}


def test_absent_projects_dir_is_not_an_error(tmp_path, monkeypatch) -> None:
    """A machine with no ``~/.claude`` at all scans nothing rather than raising."""
    monkeypatch.setenv("HOME", str(tmp_path / "bare-home"))
    assert list(mod._agent_transcripts()) == []
    assert mod.agent_types_on_disk() == {}


def test_first_line_naming_both_fields_answers_for_the_file(claude_home) -> None:
    """The fields are constant across a transcript, so the first pair wins and the
    rest of the file is not consulted."""
    _transcript(claude_home, [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
        _assistant("ag-1", "Explore"),
        _assistant("ag-1", "SomethingElse"),
    ])
    assert mod.agent_types_on_disk() == {"ag-1": "Explore"}


def test_fields_split_across_lines_are_both_recovered(claude_home) -> None:
    _transcript(claude_home, [
        {"type": "system", "agentId": "ag-1"},
        {"type": "assistant", "attributionAgent": "code-reviewer"},
    ])
    assert mod.agent_types_on_disk() == {"ag-1": "code-reviewer"}


def test_torn_line_is_skipped_not_fatal(claude_home) -> None:
    """An unparseable line is stepped over, not fatal — a half-flushed record is the
    normal state of a transcript being written right now."""
    path = _transcript(claude_home, [_assistant("ag-1", "Explore")])
    torn = '{"agentId": "ag-torn", "attributionAgent": \n'
    path.write_text(torn + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert mod.agent_types_on_disk() == {"ag-1": "Explore"}


def test_transcript_naming_only_one_field_contributes_nothing(claude_home) -> None:
    _transcript(claude_home, [{"type": "assistant", "agentId": "ag-1"}],
                name="agent-idonly.jsonl")
    _transcript(claude_home, [{"type": "assistant", "attributionAgent": "Explore"}],
                session="sess-2", name="agent-typeonly.jsonl")
    assert mod.agent_types_on_disk() == {}


def test_unreadable_transcript_is_skipped(claude_home) -> None:
    """An unopenable path (here: a directory wearing a transcript's name) is stepped
    over, so one bad entry can't abort the sweep."""
    _transcript(claude_home, [_assistant("ag-1", "Explore")])
    bogus = (claude_home / ".claude" / "projects" / "-Users-x-proj" / "sess-1"
             / "subagents" / "agent-broken.jsonl")
    bogus.mkdir()
    assert mod.agent_types_on_disk() == {"ag-1": "Explore"}


# ── the store pass ───────────────────────────────────────────────────────────

def test_preview_reports_the_plan_without_writing(claude_home, capsys) -> None:
    init_db()
    _transcript(claude_home, [_assistant("ag-1", "Explore")])
    tid = _seed_thread("sub-1", {"is_subagent": True, "agent_id": "ag-1"})

    assert _run_main() == 0
    out = capsys.readouterr().out
    assert "1 agent ids name their type" in out
    assert "1 to stamp" in out
    assert f"{tid}: agent_type='Explore'" in out
    assert "preview only" in out
    assert "agent_type" not in _meta(tid), "preview must not write"


def test_apply_stamps_store_and_truth_and_is_idempotent(claude_home, capsys) -> None:
    init_db()
    _transcript(claude_home, [_assistant("ag-1", "Explore")])
    tid = _seed_thread("sub-1", {"is_subagent": True, "agent_id": "ag-1",
                                 "parent_thread_id": 99})

    assert _run_main("--apply") == 0
    assert "stamped agent_type on 1 threads" in capsys.readouterr().out

    # Only the one key is added; everything the thread already carried survives.
    assert _meta(tid) == {"is_subagent": True, "agent_id": "ag-1",
                          "parent_thread_id": 99, "agent_type": "Explore"}

    # The thread's truth file carries the re-staged metadata record (latest wins).
    path = _thread_file(log_dir(), tid, _shard_depth(log_dir()))
    records = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    threads = [r for r in records if r.get("type") == "thread"]
    assert threads and threads[-1]["source_metadata"]["agent_type"] == "Explore"

    # Re-run: the thread is already stamped, so nothing is planned.
    assert _run_main("--apply") == 0
    out = capsys.readouterr().out
    assert "1 already stamped" in out
    assert "0 to stamp" in out
    assert "nothing to do" in out


def test_hand_corrected_value_survives_a_rerun(claude_home) -> None:
    """A thread that already names a type is left alone even when disk disagrees."""
    init_db()
    _transcript(claude_home, [_assistant("ag-1", "Explore")])
    tid = _seed_thread("sub-1", {"is_subagent": True, "agent_id": "ag-1",
                                 "agent_type": "hand-corrected"})

    assert _run_main("--apply") == 0
    assert _meta(tid)["agent_type"] == "hand-corrected"


def test_rotated_away_transcript_is_unrecoverable_not_guessed(
    claude_home, capsys
) -> None:
    init_db()
    _transcript(claude_home, [_assistant("ag-1", "Explore")])
    gone = _seed_thread("sub-gone", {"is_subagent": True, "agent_id": "ag-vanished"})
    # a subagent thread that never recorded an agent_id at all is equally unrecoverable
    idless = _seed_thread("sub-idless", {"is_subagent": True})

    assert _run_main("--apply") == 0
    out = capsys.readouterr().out
    assert "2 unrecoverable" in out
    assert "agent_type" not in _meta(gone)
    assert "agent_type" not in _meta(idless)


def test_non_subagent_and_foreign_source_threads_are_never_touched(
    claude_home, capsys
) -> None:
    init_db()
    _transcript(claude_home, [_assistant("ag-1", "Explore")])
    plain = _seed_thread("plain", {"branched_from": 3})
    metaless = _seed_thread("metaless", None)
    # a non-claude-code thread is outside the query entirely, agent_id or not
    other = _seed_thread("cursor-sub", {"is_subagent": True, "agent_id": "ag-1"},
                         source="cursor")

    assert _run_main("--apply") == 0
    out = capsys.readouterr().out
    assert "0 to stamp" in out
    assert "0 unrecoverable" in out
    assert _meta(plain) == {"branched_from": 3}
    assert _meta(metaless) == {}
    assert "agent_type" not in _meta(other)


def test_preview_list_is_capped_at_ten(claude_home, capsys) -> None:
    init_db()
    for i in range(12):
        _transcript(claude_home, [_assistant(f"ag-{i}", "Explore")],
                    session=f"sess-{i}", name=f"agent-{i}.jsonl")
        _seed_thread(f"sub-{i}", {"is_subagent": True, "agent_id": f"ag-{i}"})

    assert _run_main() == 0
    out = capsys.readouterr().out
    assert "12 to stamp" in out
    assert out.count("agent_type='Explore'") == 10
    assert "… and 2 more" in out
