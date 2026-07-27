"""Orchestration coverage for the mining machinery — the agent-driving paths,
exercised through their seams (a fake agent runner, a seeded corpus) so no real
``claude`` is spawned and no tokens are spent.

Covered here is everything a miner runs *through* rather than any one miner's
own sampling: the ``run_claude`` subprocess envelope, the corpus tool seam
(``_corpus``) against real search over a tiny seeded store, and the CLI run
paths driven with a fake registry and a stub snapshot guard. A registered
miner's own ``run`` is driven end to end beside that miner
(``test_commit_mine_gold.py``).
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone

import pytest

from search_lab.mine import (  # noqa: E402
    _agent,
    _cli,
    _corpus,
)
from search_lab.mine import (  # noqa: E402
    _framework as fw,
)
from search_lab.mine._agent import run_claude  # noqa: E402

# ── seeding helpers ──────────────────────────────────────────────────────────

def _seed_searchable(title: str, content: str, *, thread_type: str = "conversation",
                     source_id: str | None = None) -> str:
    """A titled thread with one searchable event. Returns its thread id."""
    from thread_archive._retrieval import index_events
    from thread_archive._store import Event, Thread, get_session

    with get_session() as s:
        t = Thread(name=f"conv:{title}:{source_id or title}", title=title,
                   thread_type=thread_type, source="cc",
                   source_id=source_id or title)
        s.add(t)
        s.flush()
        e = Event(thread_id=t.id, stream_id=title, event_type="user_message_sent",
                  payload={"content": content},
                  occurred_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
        s.add(e)
        s.flush()
        index_events(s, [e])
        tid = t.id
        s.commit()
    return tid


def _fake_agent(reply: str):
    """An agent seam that always returns ``reply`` (and empty stats)."""
    def run(prompt, model, tool_cmd, **kw):
        return reply, {"num_turns": 1}
    return run


def _ctx(target=5, agent_run=None, **args):
    ns = argparse.Namespace(model="opus", jobs=1, seed=7, out=None, target=target,
                            **args)
    return fw.MineContext(snapshot_id="snap-1", target=target, model="opus",
                          jobs=1, tool_cmd="py tool", args=ns, agent_run=agent_run)


# ── run_claude envelope (via a fake subprocess runner) ───────────────────────

class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_claude_parses_a_successful_envelope():
    def runner(cmd, **kw):
        return _Proc(0, stdout=json.dumps(
            {"result": "the answer", "num_turns": 4,
             "total_cost_usd": 0.2, "duration_ms": 900}))

    text, stats = run_claude("p", "opus", "py tool", runner=runner)
    assert text == "the answer"
    assert stats["num_turns"] == 4 and stats["cost_usd"] == 0.2


def test_run_claude_reports_failure_modes():
    nonzero = run_claude("p", "opus", "t",
                         runner=lambda cmd, **kw: _Proc(1, stderr="boom"))
    assert nonzero == (None, {"error": "boom"})

    def timeout_runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    text, stats = run_claude("p", "opus", "t", runner=timeout_runner)
    assert text is None and stats["error"] == "timeout"

    bad = run_claude("p", "opus", "t",
                     runner=lambda cmd, **kw: _Proc(0, stdout="not json"))
    assert bad[0] is None and "error" in bad[1]


def test_default_model_is_opus():
    assert _agent.DEFAULT_MODEL == "opus"


def test_run_claude_never_exceeds_the_concurrency_ceiling():
    """The rate cap is an invariant, not a suggestion: however many miners fan out
    at once, no more than MAX_CONCURRENT_SESSIONS `claude` sessions are live. A
    reusable barrier sized to the ceiling proves it — exactly that many threads
    can sit inside the runner at a time, so each wave trips it and drains before
    the next acquires a slot; peak live count can never pass the ceiling."""
    import threading

    from search_lab.mine._agent import MAX_CONCURRENT_SESSIONS

    live = peak = 0
    accounting = threading.Lock()
    barrier = threading.Barrier(MAX_CONCURRENT_SESSIONS)
    broke = threading.Event()

    def runner(cmd, **kw):
        nonlocal live, peak
        with accounting:
            live += 1
            peak = max(peak, live)
        try:
            barrier.wait(timeout=5)  # a full wave has to gather before any leaves
        except threading.BrokenBarrierError:
            broke.set()
        with accounting:
            live -= 1
        return _Proc(0, stdout=json.dumps({"result": "ok"}))

    threads = [threading.Thread(
        target=lambda: run_claude("p", "opus", "t", runner=runner))
        for _ in range(MAX_CONCURRENT_SESSIONS * 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not broke.is_set()  # every wave gathered exactly the ceiling — no fewer
    assert peak == MAX_CONCURRENT_SESSIONS  # ...and no more


# ── the corpus tool seam (real search/read over a seeded store) ──────────────

def test_corpus_tool_search_and_read(archive_home, capsys):
    from thread_archive._store import init_db

    init_db()
    tid = _seed_searchable("the auth thread", "authentication uses rotating tokens")

    _corpus.main(["search", "authentication"])
    out = capsys.readouterr().out
    assert tid in out and "auth thread" in out

    _corpus.main(["read", tid])
    assert "token" in capsys.readouterr().out.lower()


def test_corpus_tool_search_skip_and_empty(archive_home, capsys):
    from thread_archive._store import init_db

    init_db()
    tid = _seed_searchable("lonely thread", "a very distinctive walrus phrase")

    _corpus.main(["search", "walrus", "--skip", tid])
    assert "(no results)" in capsys.readouterr().out  # the only hit was skipped

    _corpus.main(["search", "nonexistentterm"])
    assert "(no results)" in capsys.readouterr().out


def test_corpus_tool_read_unknown_thread_errors(archive_home):

    from thread_archive._store import init_db

    init_db()
    with pytest.raises(SystemExit, match="unknown thread"):
        _corpus.main(["read", "nope-not-a-thread"])


# ── CLI run paths (fake registry + stub snapshot guard) ──────────────────────

class _FakeMiner(fw.Miner):
    name = "fake"
    summary = "a test miner"
    measures = "nothing"
    unit = "item"
    cost = "free"
    target_kind = "per-case"
    target_help = "items"
    cases_stem = "fake-cases"

    def __init__(self):
        self.ran_with = None

    def run(self, ctx):
        self.ran_with = ctx
        return fw.MineResult(written=ctx.target, failed=0,
                             cases_path="/tmp/fake-cases.jsonl",
                             notes=["a note"])


def test_dispatch_runs_a_miner_through_the_seams(capsys):
    fake = _FakeMiner()
    rc = _cli.dispatch(["fake", "--target", "3"], registry=[fake],
                       open_fn=lambda: "snap-xyz")
    assert rc == 0
    assert fake.ran_with.target == 3 and fake.ran_with.snapshot_id == "snap-xyz"
    out = capsys.readouterr().out
    assert "3 case(s) written" in out and "a note" in out


def test_execute_records_the_run_denominator_to_the_ledger(tmp_path, monkeypatch):
    # A miner run through the CLI appends one row to the mining ledger carrying the
    # attempted count and the outcome breakdown — the abstention/drop denominator,
    # made durable instead of surviving only in the console line. HOME points the
    # gold dir (where the ledger lands, beside the cases) at a throwaway.
    from search_lab import mine_runs

    monkeypatch.setenv("HOME", str(tmp_path))
    gold_dir = tmp_path / ".thread" / "archive"

    class _Counting(_FakeMiner):
        def run(self, ctx):
            return fw.MineResult(written=2, failed=1, attempted=3,
                                 outcomes={"ok": 2, "none-of-pool": 1})

    rc = _cli.dispatch(["fake"], registry=[_Counting()], open_fn=lambda: "snap-led")
    assert rc == 0
    runs = mine_runs.read_runs(gold_dir)
    assert len(runs) == 1
    assert runs[0]["miner"] == "fake" and runs[0]["snapshot_id"] == "snap-led"
    assert runs[0]["attempted"] == 3 and runs[0]["outcomes"]["none-of-pool"] == 1


def test_dispatch_all_runs_percase_and_skips_batch(capsys):
    class _Batch(_FakeMiner):
        name = "batchy"
        target_kind = "batch"
        runnable_in_all = False

    rc = _cli.dispatch(["all", "2"], registry=[_FakeMiner(), _Batch()],
                       open_fn=lambda: "snap-1")
    assert rc == 0
    out = capsys.readouterr().out
    assert "skip batchy" in out
    assert "mine all done" in out


def test_dispatch_clamps_jobs_and_target(capsys):
    """A single miner run: an over-target and over-jobs request reach the miner
    already clamped, each with a printed notice."""
    from search_lab.mine._agent import MAX_CONCURRENT_SESSIONS

    fake = _FakeMiner()
    rc = _cli.dispatch(["fake", "--target", "999", "--jobs", "50"],
                       registry=[fake], open_fn=lambda: "snap-1")
    assert rc == 0
    assert fake.ran_with.target == fw.MAX_SESSIONS_PER_RUN
    assert fake.ran_with.jobs == MAX_CONCURRENT_SESSIONS
    out = capsys.readouterr().out
    assert "concurrent sessions" in out and "per run" in out


def test_dispatch_all_shares_one_budget_across_miners(capsys):
    """The sweep spends at most MAX_SESSIONS_PER_RUN in total, split across its
    miners — not that many per miner — and clamps jobs to the concurrency ceiling."""
    from search_lab.mine._agent import MAX_CONCURRENT_SESSIONS

    miners = [_FakeMiner() for _ in range(3)]
    for i, m in enumerate(miners):
        m.name = f"f{i}"
    rc = _cli.dispatch(["all", "20", "--jobs", "50"], registry=miners,
                       open_fn=lambda: "snap-1")
    assert rc == 0
    targets = [m.ran_with.target for m in miners]
    assert sum(targets) <= fw.MAX_SESSIONS_PER_RUN  # the whole sweep, not each miner
    assert targets == [9, 8, 8]  # even split, remainder to the front
    assert all(m.ran_with.jobs == MAX_CONCURRENT_SESSIONS for m in miners)
    out = capsys.readouterr().out
    assert "sessions total" in out and "--jobs 50 ->" in out


def test_dispatch_all_honors_target_when_it_fits_the_budget(capsys):
    """Under budget, every miner gets its full target and the plan reads plainly."""
    miners = [_FakeMiner() for _ in range(3)]
    for i, m in enumerate(miners):
        m.name = f"f{i}"
    rc = _cli.dispatch(["all", "5"], registry=miners, open_fn=lambda: "snap-1")
    assert rc == 0
    assert [m.ran_with.target for m in miners] == [5, 5, 5]
    assert "at target=5" in capsys.readouterr().out


def test_guarded_open_requires_a_snapshot(archive_home):

    from thread_archive._store import init_db

    init_db()
    # No snapshot.json in the home → the guard refuses (claude may or may not be
    # installed; either way the precondition fails).
    with pytest.raises(SystemExit):
        _cli._guarded_open()


def test_require_snapshot_reads_the_manifest(archive_home):
    (archive_home / "snapshot.json").write_text(json.dumps({"snapshot_id": "snap-aaa"}))
    assert fw.require_snapshot() == "snap-aaa"


# ── the -m entry point ───────────────────────────────────────────────────────

def test_module_main_routes_tool_and_list(archive_home, capsys):
    from search_lab.mine.__main__ import main
    from thread_archive._store import init_db

    init_db()
    tid = _seed_searchable("routed thread", "a distinctive kangaroo phrase")
    assert main(["tool", "search", "kangaroo"]) == 0
    assert tid in capsys.readouterr().out

    assert main([]) == 0  # no args → the registry list view
    listing = capsys.readouterr().out
    assert "Gold miners" in listing
    for name in ("commit",):
        assert name in listing
