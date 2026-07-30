"""The corpus graph outlives the process (``_retrieval.graph_cache``).

Held only in memory, the graph starts empty at every restart and the coherence
re-rank stands down until a build lands — so for the first several seconds of a
process the same query comes back in a different order, with nothing in the
output to say so. These cover that a restart now serves a graph immediately, that
what it serves ranks identically to what it wrote, and that every way of getting
it wrong lands on a rebuild rather than on a graph nobody can vouch for.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import text as sa_text

from thread_archive._retrieval import embed_graph, graph_cache, vectors
from thread_archive._store import Event, Thread, get_engine, get_session, init_db

DIM = 768


def _unit(axis: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


def _seed(clusters=(("a1", 0), ("a2", 0), ("b1", 1), ("b2", 1))) -> dict[str, str]:
    """Conversations in orthogonal vector clusters — one community per axis.
    Additive, so a second call moves the store token the way ingest does."""
    init_db()
    ids: dict[str, str] = {}
    vecs: list[tuple[int, str, np.ndarray]] = []
    with get_session() as s:
        eid = int(s.execute(sa_text("SELECT coalesce(max(id), 0) FROM events")).scalar())
        for name, axis in clusters:
            t = Thread(name=f"t-{name}", title=name.upper(), thread_type="conversation")
            s.add(t)
            s.flush()
            ids[name] = t.id
            eid += 1
            s.add(Event(id=eid, thread_id=t.id, stream_id=f"st-{name}",
                        event_type="message", payload={},
                        occurred_at=datetime.now(timezone.utc)))
            vecs.append((eid, "text", _unit(axis)))
        s.commit()
    vectors.index_vectors(vecs)
    embed_graph.reset_cache()
    return ids


def _restart() -> None:
    """The state a new process starts in: no graph in memory, whatever is on disk."""
    embed_graph.reset_cache()


def _params() -> dict:
    return embed_graph._cache_params(embed_graph.KNN, embed_graph.MIN_SIM)


def _cache_dir() -> Path:
    return Path(get_engine().url.database).parent / graph_cache.DIRNAME


def _meta_path() -> Path:
    (meta,) = list(_cache_dir().glob("graph-*.json"))
    return meta


def _rewrite(doc: dict) -> None:
    _meta_path().write_text(json.dumps(doc), encoding="utf-8")


def _wait_for_refresh(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while embed_graph._REFRESHING and time.monotonic() < deadline:
        time.sleep(0.01)


def _token() -> tuple:
    with get_session() as s:
        return vectors._validity_token(s)


def _age_cache(seconds: float) -> None:
    """Backdate every persisted file — the mtime an age probe reads."""
    when = time.time() - seconds
    for p in _cache_dir().glob("*"):
        os.utime(p, (when, when))


# ── The point of the exercise ────────────────────────────────────────────────


def test_a_restart_has_a_graph_to_rank_with_immediately(archive_home) -> None:
    """The regression this exists to prevent: a fresh process serving searches with
    no graph, so the coherence re-rank stands down and the same query comes back
    ordered differently than it did a minute ago.

    ``get()`` is the search path's entry point and never blocks on a build, so
    before this the only honest answer during a restart was ``None``."""
    _seed()
    embed_graph.build()  # a previous process's build, written down

    _restart()
    assert embed_graph.get() is not None, (
        "a restart must rank with the persisted graph, not with no graph at all"
    )


def test_what_comes_back_ranks_exactly_like_what_went_down(archive_home) -> None:
    """Serving *a* graph is not enough — it has to be the same graph, or the
    restart still changes the ranking, just less visibly."""
    from thread_archive import _retrieval

    ids = _seed()
    built = embed_graph.build()

    def hit(tid, eid):
        return {"thread_id": tid, "event_id": eid, "full_content": "x"}

    pool = [hit(ids["b1"], 1), hit(ids["a1"], 2), hit(ids["a2"], 3)]
    before = _retrieval._apply_coherence(list(pool), gamma=0.05)

    _restart()
    loaded = embed_graph.get()

    assert loaded is not built, "a fresh object, not the one still in memory"
    assert loaded.thread_ids == built.thread_ids
    assert loaded.community == built.community
    assert loaded.edges == built.edges
    assert np.array_equal(loaded.centroids, built.centroids)
    assert _retrieval._apply_coherence(list(pool), gamma=0.05) == before

    # The centroids round-trip too, so the whole CorpusGraph API survives the
    # restart — not just the community map the re-rank happens to read today.
    assert loaded.similarity(_unit(0), [ids["a1"]])[ids["a1"]] > 0.99
    assert loaded.centroid(ids["b1"]) is not None


def test_an_unmoved_store_skips_the_rebuild_entirely(archive_home) -> None:
    """``build()`` is the authoritative path — an eval calls it — so when the corpus
    has not moved since the last process wrote its graph, there is nothing left to
    compute even under the strictest caller."""
    _seed()
    built = embed_graph.build()

    _restart()
    again = embed_graph.build()
    assert again is not built
    assert again.thread_ids == built.thread_ids
    assert again.community == built.community


def test_a_moved_store_serves_the_old_graph_and_converges_on_its_own(archive_home) -> None:
    """Under continuous ingest the token moves every few minutes, so a restart
    almost never finds its own graph. Requiring a match would make persistence
    useless exactly where it matters; serving stale while the refresh runs behind
    it is what a long-lived process already does."""
    old = _seed()
    embed_graph.build()
    new = _seed(clusters=(("c1", 2), ("c2", 2)))  # token moves: fresh vectors

    _restart()
    served = embed_graph.get()
    assert served is not None
    assert set(served.thread_ids) == set(old.values()), "the graph as it was written"
    assert new["c1"] not in served.community, (
        "a thread the graph has not seen takes no boost — it is not misplaced"
    )

    _wait_for_refresh()
    fresh = embed_graph.build()
    assert set(fresh.thread_ids) == set(old.values()) | set(new.values())


def test_build_refuses_the_stale_graph_get_is_happy_to_serve(archive_home) -> None:
    """The two entry points want different things. ``get()`` wants something to
    rank with; ``build()`` is asked for the graph of the store as it stands, and
    answering it from a superseded file would mean the refresh never lands."""
    _seed()
    embed_graph.build()
    new = _seed(clusters=(("c1", 2), ("c2", 2)))

    _restart()
    assert new["c1"] in embed_graph.build().thread_ids


# ── The starting-server door ─────────────────────────────────────────────────


def test_a_starting_process_loads_the_graph_instead_of_rebuilding_it(archive_home) -> None:
    """``warm()`` is what a starting server calls, and the point of it is that it
    computes nothing when a partition is already on disk.

    Proven by what it serves rather than by watching it work: the store has moved
    since the graph was written, so a rebuild would have to include the new threads.
    Serving the old set is only possible by loading."""
    old = _seed()
    embed_graph.build()
    new = _seed(clusters=(("c1", 2), ("c2", 2)))  # the token moves, as ingest moves it

    _restart()
    served = embed_graph.warm()
    assert served is not None
    assert set(served.thread_ids) == set(old.values()), (
        "warm rebuilt the graph it could have read off disk"
    )
    assert new["c1"] not in served.community, (
        "a thread the graph has not seen takes no boost — it is not misplaced"
    )
    _wait_for_refresh()


def test_warm_builds_when_there_is_nothing_to_load(archive_home) -> None:
    """A first run, or a build-shape change that invalidated every file. Cheapness is
    not the contract — being useful is — so with no graph to serve, warm pays."""
    ids = _seed()
    _restart()
    assert not _cache_dir().exists() or not list(_cache_dir().glob("graph-*.json"))

    served = embed_graph.warm()
    assert served is not None
    assert set(served.thread_ids) == set(ids.values())


def test_warm_on_a_store_with_no_embedded_vectors_is_not_an_error(archive_home) -> None:
    """The graph degrades with the semantic arm, not separately."""
    init_db()
    assert embed_graph.warm() is None


# ── One build per machine, not one per process ───────────────────────────────


def test_a_graph_another_process_just_wrote_suppresses_the_rebuild(archive_home) -> None:
    """The restart burst: several processes come up at once, each finds a token that
    ingest has moved, and each would rebuild the same corpus-wide partition
    concurrently. The floor is how any of them learns someone already did it."""
    _seed()
    embed_graph.build()
    _seed(clusters=(("c1", 2), ("c2", 2)))

    assert embed_graph._rebuild_redundant(), "a graph written seconds ago is enough"

    _restart()
    embed_graph.warm()
    _wait_for_refresh()
    assert not embed_graph._REFRESHING, "a redundant rebuild was started anyway"


def test_an_aged_graph_lets_the_rebuild_through(archive_home) -> None:
    """The floor bounds how often the machine rebuilds, not whether it ever does."""
    _seed()
    embed_graph.build()
    assert embed_graph._rebuild_redundant()

    _age_cache(embed_graph.rebuild_floor_s() + 60.0)
    assert not embed_graph._rebuild_redundant()


def test_nothing_on_disk_never_suppresses_a_build(archive_home) -> None:
    init_db()
    assert not embed_graph._rebuild_redundant(), (
        "the floor suppresses duplicate work, never the only copy of it"
    )


def test_the_rebuild_floor_can_be_turned_off(archive_home, monkeypatch) -> None:
    _seed()
    embed_graph.build()
    assert embed_graph._rebuild_redundant()

    monkeypatch.setenv("THREAD_ARCHIVE_GRAPH_REBUILD_FLOOR_S", "0")
    assert not embed_graph._rebuild_redundant()


def test_a_bad_rebuild_floor_setting_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_GRAPH_REBUILD_FLOOR_S", "shortly")
    assert embed_graph.rebuild_floor_s() == 900.0
    monkeypatch.delenv("THREAD_ARCHIVE_GRAPH_REBUILD_FLOOR_S")
    assert embed_graph.rebuild_floor_s() == 900.0


def test_the_age_probe_reports_an_absent_graph_as_none(archive_home) -> None:
    """``None`` is the answer that must not read as "just built" — it is what gates
    the floor off so a first build can happen."""
    init_db()
    assert graph_cache.newest_age_s() is None

    _seed()
    embed_graph.build()
    age = graph_cache.newest_age_s()
    assert age is not None and age < 60.0


# ── Every failure lands on a rebuild ─────────────────────────────────────────


def test_a_different_community_engine_is_a_miss(archive_home) -> None:
    """Leiden and the Louvain fallback partition some regions differently, so one
    engine's graph is not the other's. An install that changes engines must rebuild
    rather than inherit a partition its own code would not have produced."""
    _seed()
    embed_graph.build()
    doc = json.loads(_meta_path().read_text(encoding="utf-8"))
    doc["params"]["engine"] = "a-different-engine"
    _rewrite(doc)
    assert graph_cache.load(_params()) is None


def test_different_build_parameters_are_a_miss(archive_home) -> None:
    _seed()
    embed_graph.build()
    pristine = _meta_path().read_text(encoding="utf-8")
    for change in ({"knn": 3}, {"min_sim": 0.9}, {"cts": ["text"]}, {"format": 99}):
        doc = json.loads(pristine)
        doc["params"].update(change)
        _rewrite(doc)
        assert graph_cache.load(_params()) is None, f"{change} must not be served"
    _rewrite(json.loads(pristine))
    assert graph_cache.load(_params()) is not None, "the pristine file still loads"


def test_a_damaged_document_is_a_miss_not_a_partial_graph(archive_home) -> None:
    """The arrays are positional and the community map is keyed by thread id, so a
    truncated or hand-edited file would not fail loudly — it would rank
    differently."""
    _seed()
    embed_graph.build()
    pristine = _meta_path().read_text(encoding="utf-8")
    for damage in (
        lambda doc: doc["thread_ids"].pop(),             # centroids no longer align
        lambda doc: doc.update(community={"a": "1"}),    # a community that isn't an int
        lambda doc: doc.update(community=[1, 2]),
        lambda doc: doc.update(edges="lots"),
        lambda doc: doc.update(thread_ids=[1, 2, 3]),
        lambda doc: doc.pop("built_at"),
        lambda doc: doc.pop("params"),
    ):
        doc = json.loads(pristine)
        damage(doc)
        _rewrite(doc)
        assert graph_cache.load(_params()) is None


def test_unreadable_or_incomplete_files_are_a_miss(archive_home) -> None:
    _seed()
    embed_graph.build()
    meta = _meta_path()
    centroids = _cache_dir() / meta.name.replace("graph-", "centroids-").replace(
        ".json", ".npy")

    centroids.unlink()
    assert graph_cache.load(_params()) is None, "a document without its centroids"

    meta.write_text("{not json", encoding="utf-8")
    assert graph_cache.load(_params()) is None


def test_an_empty_or_absent_cache_directory_is_a_miss(archive_home) -> None:
    init_db()
    assert graph_cache.load(_params()) is None
    _cache_dir().mkdir(parents=True, exist_ok=True)
    assert graph_cache.load(_params()) is None


def test_a_graph_past_its_age_bound_is_a_miss(archive_home, monkeypatch) -> None:
    """Staleness costs coverage: threads created since the graph was built take no
    boost. The bound is on how much of the corpus that may be."""
    _seed()
    embed_graph.build()
    assert graph_cache.load(_params()) is not None

    monkeypatch.setenv("THREAD_ARCHIVE_GRAPH_CACHE_TTL_S", "0.000001")
    assert graph_cache.load(_params()) is None


def test_a_clock_that_moved_backwards_is_a_miss(archive_home) -> None:
    _seed()
    embed_graph.build()
    doc = json.loads(_meta_path().read_text(encoding="utf-8"))
    doc["built_at"] = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    _rewrite(doc)
    assert graph_cache.load(_params()) is None


def test_persistence_can_be_turned_off(archive_home, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_GRAPH_CACHE_TTL_S", "0")
    _seed()
    embed_graph.build()
    assert not _cache_dir().exists() or not list(_cache_dir().glob("graph-*.json"))
    _restart()
    assert embed_graph.get() is None, "off is the pre-persistence behavior"
    _wait_for_refresh()


def test_a_bad_ttl_setting_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_GRAPH_CACHE_TTL_S", "soon")
    assert graph_cache.max_age_s() == 7 * 86400.0
    monkeypatch.delenv("THREAD_ARCHIVE_GRAPH_CACHE_TTL_S")
    assert graph_cache.max_age_s() == 7 * 86400.0


# ── Invalidation, throttling, housekeeping ───────────────────────────────────


def test_re_embedding_in_place_drops_the_persisted_graph(archive_home) -> None:
    """An upsert rewrites a vector without moving the store token, so the tag
    cannot see it and no other process can know. Dropped at the source, where the
    write happens, rather than aged out."""
    ids = _seed()
    embed_graph.build()
    assert graph_cache.load(_params()) is not None

    with get_session() as s:
        eid = s.execute(sa_text("SELECT id FROM events WHERE thread_id = :t"),
                        {"t": ids["a1"]}).scalar()
    vectors.index_vectors([(int(eid), "text", _unit(5))])  # same key, new vector

    assert graph_cache.load(_params()) is None, (
        "a graph built from vectors that have since changed must not be served"
    )


def test_saves_are_throttled(archive_home) -> None:
    """Tens of MB of centroids against a token that moves every few minutes. The
    disk copy exists to be *a* recent graph for the next restart, never the
    current one."""
    _seed()
    g = embed_graph.build()  # the process's first build always lands
    assert graph_cache.save(_token(), g, _params()) is False
    assert graph_cache.save(_token(), g, _params(), force=True) is True


def test_superseded_files_do_not_accumulate(archive_home) -> None:
    _seed()
    embed_graph.build()
    aged = time.time() - graph_cache._SWEEP_GRACE_S - 60
    for p in _cache_dir().glob("*"):
        os.utime(p, (aged, aged))

    _seed(clusters=(("c1", 2),))
    graph_cache.save(_token(), embed_graph.build(), _params(), force=True)

    assert len(list(_cache_dir().glob("graph-*.json"))) == 1
    assert len(list(_cache_dir().glob("centroids-*.npy"))) == 1


def test_a_reader_sweeps_too_not_only_a_writer(archive_home) -> None:
    """A process that reads a graph and never builds one — the quiet archive, the
    short-lived tool — would otherwise leave the superseded pair on disk until
    something happened to write, which on a quiet archive is never."""
    _seed()
    embed_graph.build()
    _seed(clusters=(("c1", 2),))
    graph_cache.save(_token(), embed_graph.build(), _params(), force=True)
    aged = time.time() - graph_cache._SWEEP_GRACE_S - 60
    for p in _cache_dir().glob("*"):
        os.utime(p, (aged, aged))
    assert len(list(_cache_dir().glob("graph-*.json"))) == 2

    assert graph_cache.load(_params()) is not None
    assert len(list(_cache_dir().glob("graph-*.json"))) == 1
    assert len(list(_cache_dir().glob("centroids-*.npy"))) == 1


def test_a_writer_that_died_mid_save_does_not_leak_its_partial(archive_home) -> None:
    """A ``.tmp.`` file carries the tag of the graph it was going to become, so one
    left by a killed writer matches the tag the sweep is keeping. Tens of MB that
    would never be collected."""
    _seed()
    embed_graph.build()
    tag = _meta_path().stem[len("graph-"):]
    orphan = _cache_dir() / f"centroids-{tag}.npy.tmp.99999"
    orphan.write_bytes(b"half a matrix")
    aged = time.time() - graph_cache._SWEEP_GRACE_S - 60
    os.utime(orphan, (aged, aged))

    assert graph_cache.load(_params()) is not None
    assert not orphan.exists()
    assert _meta_path().exists(), "the live pair it shares a tag with survives"


def test_a_superseded_file_within_the_grace_window_is_left_alone(archive_home) -> None:
    """A reader that has chosen a tag and not yet opened its centroids would lose
    the race. It degrades to a rebuild rather than an error, so the grace is
    politeness — but there is no reason to spend it."""
    _seed()
    embed_graph.build()
    _seed(clusters=(("c1", 2),))
    graph_cache.save(_token(), embed_graph.build(), _params(), force=True)
    assert len(list(_cache_dir().glob("graph-*.json"))) == 2


def test_drop_clears_everything(archive_home) -> None:
    _seed()
    embed_graph.build()
    graph_cache.drop()
    assert graph_cache.load(_params()) is None
    _restart()
    assert embed_graph.get() is None
    _wait_for_refresh()


def test_status_says_whether_a_restart_will_rank_the_same(archive_home, capsys) -> None:
    """The symptom this feature addresses is invisible from the outside — a process
    with no graph answers every search, just in a different order. So the operator
    report says which state the archive is in, and never builds one to find out."""
    from thread_archive import _api as api
    from thread_archive import cli

    _seed()
    st = api.status()
    assert st["graph_cache"]["present"] is False
    cli.report_status(st)
    assert "a restart re-ranks until it rebuilds" in capsys.readouterr().out

    embed_graph.build()
    st = api.status()
    assert st["graph_cache"]["present"] is True
    cli.report_status(st)
    out = capsys.readouterr().out
    assert "graph:   4 threads, 2 edges" in out


def test_status_stays_quiet_about_a_graph_an_archive_cannot_have(capsys) -> None:
    """A lexical-only archive embeds nothing, so there is no graph to have and none
    to miss. A warning there would be noise about a shape of the product."""
    from thread_archive import cli

    cli.report_status({"home": "/h", "truth_dir": "/t", "index_path": "/i",
                       "threads": 0, "events": 0, "fts_indexed": 0,
                       "vectors_indexed": 0, "graph_cache": {"present": False}})
    assert "graph:" not in capsys.readouterr().out


def test_describe_answers_whether_a_restart_will_have_a_graph(archive_home) -> None:
    assert graph_cache.describe() == {"present": False}
    _seed()
    embed_graph.build()
    d = graph_cache.describe()
    assert d["present"] is True
    assert d["threads"] == 4 and d["edges"] == 2
    assert d["engine"] in ("leiden", "louvain")
    assert d["bytes"] > 0 and 0 <= d["age_s"] < 60
    assert d["token"] == [_token()[1], _token()[2]]
