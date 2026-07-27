"""Orchestration coverage for the miners — the agent-driving paths, exercised
through their seams (a fake agent runner, a seeded corpus) so no real ``claude``
is spawned and no tokens are spent.

Every miner's ``run`` is driven end to end: sample → agent → validate → write a
case file, with the agent replaced by a canned-reply stub. The corpus tool seam
(``_corpus``) and the ``run_claude`` subprocess envelope are covered the same
way — real search over a tiny seeded store, a fake subprocess runner.
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
    query_mined,
    querygen,
    rerank_judged,
    topic_mined,
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


def _seed_trail_query(query: str, gold_id: str) -> str:
    """A conversation session that searched ``query`` then read ``gold_id`` — the
    search→read pair the query/rerank samplers mine. Returns the session id."""
    from thread_archive._store import Event, Thread, get_session

    with get_session() as s:
        sess = Thread(name=f"sess:{query}", thread_type="conversation",
                      source="cc", source_id=f"sess:{query}")
        s.add(sess)
        s.flush()
        sid = sess.id
        clk = iter(range(10, 30))
        s.add(Event(thread_id=sid, stream_id="t", event_type="tool_use_complete",
                    payload={"tool_name": "mcp__thread-archive__thread_search",
                             "input": {"query": query}},
                    occurred_at=datetime(2026, 1, 2, 12, next(clk), tzinfo=timezone.utc)))
        s.add(Event(thread_id=sid, stream_id="t", event_type="tool_use_complete",
                    payload={"tool_name": "mcp__thread-archive__thread_read",
                             "input": {"thread_id": gold_id}},
                    occurred_at=datetime(2026, 1, 2, 12, next(clk), tzinfo=timezone.utc)))
        s.commit()
    return sid


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


# ── query miner run() ────────────────────────────────────────────────────────

def test_query_miner_run_writes_a_case(archive_home, tmp_path, capsys):
    from thread_archive._store import init_db

    init_db()
    gold = _seed_searchable("capture restart", "the capture daemon restart loop fix")
    _seed_trail_query("capture daemon restart", gold)

    out = tmp_path / "judged-cases.jsonl"
    reply = json.dumps({"gold": [gold], "grades": {gold: 2},
                        "confidence": "high", "rationale": "it answers"})
    ctx = _ctx(agent_run=_fake_agent(reply), queries=None, mined_after=None)
    ctx.args.out = out
    result = query_mined.MINER.run(ctx)

    assert result.written == 1
    row = json.loads(out.read_text().splitlines()[0])
    assert row["gold"] == [gold] and row["miner"] == "query"
    assert row["snapshot_id"] == "snap-1" and row["protocol"] == "agent-mined"


def test_query_miner_run_records_agent_failure(archive_home, tmp_path):
    from thread_archive._store import init_db

    init_db()
    gold = _seed_searchable("x thread", "some distinctive zebra content here")
    _seed_trail_query("zebra content", gold)

    out = tmp_path / "judged-cases.jsonl"
    # Agent returns None (failed) → no case, one failure recorded.
    ctx = _ctx(agent_run=lambda *a, **k: (None, {"error": "x"}),
               queries=None, mined_after=None)
    ctx.args.out = out
    result = query_mined.MINER.run(ctx)
    assert result.written == 0 and result.failed == 1
    assert not out.exists() or out.read_text() == ""


# ── rerank miner run() ───────────────────────────────────────────────────────

def test_rerank_miner_run_judges_the_pool(archive_home, tmp_path):
    from thread_archive._store import init_db

    init_db()
    gold = _seed_searchable("blue whale facts", "the blue whale is the largest animal")
    _seed_trail_query("largest animal", gold)

    out = tmp_path / "rerank-cases.jsonl"
    reply = json.dumps({"grades": {gold: 2}, "none_answer": False,
                        "rationale": "answers"})
    ctx = _ctx(agent_run=_fake_agent(reply), pool=10, queries=None, mined_after=None)
    ctx.args.out = out
    result = rerank_judged.MINER.run(ctx)

    assert result.written == 1
    row = json.loads(out.read_text().splitlines()[0])
    assert row["gold"] == [gold] and row["protocol"] == "rerank-judged"


def test_rerank_miner_run_flags_none_of_pool(archive_home, tmp_path):
    from thread_archive._store import init_db

    init_db()
    gold = _seed_searchable("unrelated", "a totally different octopus subject")
    _seed_trail_query("octopus subject", gold)

    out = tmp_path / "rerank-cases.jsonl"
    reply = json.dumps({"grades": {gold: 0}, "none_answer": True})
    ctx = _ctx(agent_run=_fake_agent(reply), pool=10, queries=None, mined_after=None)
    ctx.args.out = out
    result = rerank_judged.MINER.run(ctx)
    assert result.written == 0
    assert any("no answer in the pool" in n for n in result.notes)


def test_rerank_run_tracks_outcomes_and_stamps_provenance(archive_home, tmp_path):
    # The run carries its denominator (attempted + per-unit outcomes) for the
    # mining ledger, and every minted case records the model that judged it, the
    # prompt hash, and the miner commit — provenance so a re-mint is detectable.
    from thread_archive._store import init_db

    init_db()
    gold = _seed_searchable("blue whale facts", "the blue whale is the largest animal")
    _seed_trail_query("largest animal", gold)

    out = tmp_path / "rerank-cases.jsonl"
    reply = json.dumps({"grades": {gold: 2}, "none_answer": False, "rationale": "ok"})
    ctx = _ctx(agent_run=_fake_agent(reply), pool=10, queries=None, mined_after=None)
    ctx.args.out = out
    result = rerank_judged.MINER.run(ctx)

    assert result.attempted == 1 and result.outcomes == {"ok": 1}
    row = json.loads(out.read_text().splitlines()[0])
    assert row["judge_model"] == "opus"  # no resolved id in stats → requested alias
    assert len(row["prompt_sha"]) == 12
    assert "miner_commit" in row


# ── querygen miner run() ─────────────────────────────────────────────────────

def test_querygen_miner_run_generates_cases(archive_home, tmp_path):
    from sqlalchemy import text as sa_text

    from thread_archive._store import Thread, get_session, init_db

    init_db()
    with get_session() as s:
        t = Thread(name="conv:gen", title="a real conversation to target",
                   thread_type="conversation", source="cc", source_id="gen")
        s.add(t)
        s.flush()
        tid = t.id
        for i in range(3):
            s.execute(sa_text(
                "INSERT INTO events_fts (event_id, thread_id, event_type, content, "
                "content_type) VALUES (:e, :t, 'message', :c, 'user')"),
                {"e": 100 + i, "t": tid, "c": f"turn {i}"})
        s.commit()

    out = tmp_path / "findability-cases.jsonl"
    reply = json.dumps({"queries": [
        {"query": "the exact phrase", "difficulty": "verbatim"},
        {"query": "a fuzzy memory of it", "difficulty": "vague"}], "note": None})
    ctx = _ctx(agent_run=_fake_agent(reply))
    ctx.args.out = out
    result = querygen.MINER.run(ctx)

    assert result.written == 2
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert all(r["gold"] == [tid] for r in rows)
    assert {r["difficulty"] for r in rows} == {"verbatim", "vague"}
    # denominator for the ledger, and provenance on every generated case
    assert result.attempted == 1 and result.outcomes == {"ok": 1}
    assert all(r["gen_model"] == "opus" and len(r["prompt_sha"]) == 12
               and "miner_commit" in r for r in rows)


def test_querygen_miner_run_handles_untargetable_thread(archive_home, tmp_path):
    from sqlalchemy import text as sa_text

    from thread_archive._store import Thread, get_session, init_db

    init_db()
    with get_session() as s:
        t = Thread(name="conv:thin", title="a thin generic conversation",
                   thread_type="conversation", source="cc", source_id="thin")
        s.add(t)
        s.flush()
        for i in range(3):
            s.execute(sa_text(
                "INSERT INTO events_fts (event_id, thread_id, event_type, content, "
                "content_type) VALUES (:e, :t, 'message', :c, 'user')"),
                {"e": 200 + i, "t": t.id, "c": f"turn {i}"})
        s.commit()

    out = tmp_path / "findability-cases.jsonl"
    ctx = _ctx(agent_run=_fake_agent('{"queries": [], "note": "too generic"}'))
    ctx.args.out = out
    result = querygen.MINER.run(ctx)
    assert result.written == 0 and result.failed == 1


# ── topic miner run() (survey → labelers) ────────────────────────────────────

def test_topic_miner_run_surveys_then_labels(archive_home, tmp_path):
    from thread_archive._store import init_db

    init_db()
    gold = _seed_searchable("grief for a bot", "mourning a deleted AI companion")
    # Seed the topic and cite the gold thread to it.
    from thread_archive._store import Thread, get_session

    with get_session() as s:
        topic = Thread(name="topic:botgrief", title="bot grief", thread_type="topic",
                       source="cc", source_id="topic:botgrief")
        s.add(topic)
        s.commit()

    survey_reply = json.dumps({"facets": [], "angles": [
        {"query": "grieving a deleted AI", "intent": "bot-death grief",
         "confounds": ["human grief"],
         "candidates": [{"thread_id": gold, "note": "explicit"}]}]})
    label_reply = json.dumps({"grades": {gold: 2}, "reasons": {gold: "answers"}})

    def agent(prompt, model, tool_cmd, **kw):
        # The survey brief and the labeler brief carry distinct headers.
        if "designing a search-quality benchmark" in prompt:
            return survey_reply, {"num_turns": 1}
        return label_reply, {"num_turns": 1}

    out = tmp_path / "topic-cases-bot-grief.jsonl"
    ctx = _ctx(agent_run=agent, topic="bot grief", max_queries=20)
    ctx.args.out = out
    result = topic_mined.MINER.run(ctx)

    assert result.written == 1
    row = json.loads(out.read_text().splitlines()[0])
    assert row["gold"] == [gold] and row["protocol"] == "topic-mined"
    assert row["query"] == "grieving a deleted AI"


def test_topic_run_caps_labeler_fanout_at_the_ceiling(archive_home, tmp_path, capsys):
    """A survey that authors far more angles than the per-run ceiling still spawns
    at most MAX_SESSIONS_PER_RUN labelers, even when --max-queries is set higher."""
    from thread_archive._store import Thread, get_session, init_db

    init_db()
    gold = _seed_searchable("target thread", "a distinctive narwhal discussion")
    with get_session() as s:
        s.add(Thread(name="topic:big", title="big topic", thread_type="topic",
                     source="cc", source_id="topic:big"))
        s.commit()

    n_angles = fw.MAX_SESSIONS_PER_RUN + 8
    survey_reply = json.dumps({"facets": [], "angles": [
        {"query": f"angle number {i}", "intent": "x", "confounds": [],
         "candidates": [{"thread_id": gold, "note": "n"}]}
        for i in range(n_angles)]})
    label_reply = json.dumps({"grades": {gold: 2}, "reasons": {gold: "ok"}})
    calls = {"survey": 0, "label": 0}

    def agent(prompt, model, tool_cmd, **kw):
        if "designing a search-quality benchmark" in prompt:
            calls["survey"] += 1
            return survey_reply, {"num_turns": 1}
        calls["label"] += 1
        return label_reply, {"num_turns": 1}

    out = tmp_path / "topic-cases-big-topic.jsonl"
    ctx = _ctx(agent_run=agent, topic="big topic", max_queries=100)
    ctx.args.out = out
    result = topic_mined.MINER.run(ctx)

    assert calls["label"] == fw.MAX_SESSIONS_PER_RUN  # not all n_angles
    assert result.written == fw.MAX_SESSIONS_PER_RUN
    assert f"capping to {fw.MAX_SESSIONS_PER_RUN}" in capsys.readouterr().out


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
    for name in ("query", "topic", "rerank", "querygen", "commit"):
        assert name in listing


# ── remaining small branches ─────────────────────────────────────────────────

def test_read_query_list_parses_objects_bare_and_dedupes(tmp_path):
    f = tmp_path / "seed.jsonl"
    f.write_text('{"query": "alpha"}\n'
                 'bravo\n'
                 '\n'                       # blank skipped
                 '{"query": "alpha"}\n'     # dup dropped
                 'not-an-object-but-a-line\n')
    assert query_mined.read_query_list(f) == ["alpha", "bravo", "not-an-object-but-a-line"]


def test_parse_branches_reject_malformed_json():
    assert rerank_judged.parse_rerank("{bad json", {"A"}) is None
    assert rerank_judged.parse_rerank('{"grades": []}', {"A"}) is None  # grades not a dict
    assert querygen.parse_queries("{bad json") is None
    assert querygen.parse_queries('{"queries": "nope"}') is None  # queries not a list
    assert topic_mined.parse_labels("{bad json") is None


def test_query_miner_run_drops_when_no_valid_gold(archive_home, tmp_path):
    from thread_archive._store import init_db

    init_db()
    gold = _seed_searchable("y thread", "a distinctive platypus discussion")
    _seed_trail_query("platypus discussion", gold)

    out = tmp_path / "judged-cases.jsonl"
    # Agent parses fine but returns an empty gold set → no valid gold, dropped.
    reply = json.dumps({"gold": [], "grades": {}, "confidence": "low",
                        "rationale": "nothing fits"})
    ctx = _ctx(agent_run=_fake_agent(reply), queries=None, mined_after=None)
    ctx.args.out = out
    result = query_mined.MINER.run(ctx)
    assert result.written == 0 and result.failed == 1
