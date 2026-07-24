"""A fresh SQLite store round-trips threads and events.

- create a fresh store
- insert/read a thread and events
- close/reopen and read the same data back
- JSON columns (payload, source_metadata) round-trip
- init_db is idempotent
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from thread_archive._store import (
    Event,
    ImportState,
    Thread,
    build_engine,
    get_session,
    init_db,
    use_engine,
)


def _dsn(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'index.db'}"


def _now() -> datetime:
    return datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_create_insert_read_reopen(tmp_path) -> None:
    dsn = _dsn(tmp_path)

    eng = build_engine(dsn)
    init_db(eng)
    with use_engine(eng), get_session() as s:
        t = Thread(
            name="sess-1",
            title="hello",
            thread_type="conversation",
            source="claude-code",
            source_id="abc",
            source_metadata={"branched_from": 3},
        )
        s.add(t)
        s.flush()
        tid = t.id
        s.add_all(
            [
                Event(
                    thread_id=tid,
                    stream_id="stream-1",
                    event_type="user_message_sent",
                    payload={"text": "hi"},
                    occurred_at=_now(),
                    dedup_key="k1",
                ),
                Event(
                    thread_id=tid,
                    stream_id="stream-1",
                    event_type="text_delta",
                    payload={"text": "hello back"},
                    occurred_at=_now(),
                    dedup_key="k2",
                ),
            ]
        )
        s.commit()
    eng.dispose()

    # Reopen a fresh engine over the same file — the data must survive.
    eng2 = build_engine(dsn)
    with use_engine(eng2), get_session() as s:
        t = s.execute(select(Thread).where(Thread.name == "sess-1")).scalar_one()
        assert t.title == "hello"
        assert t.thread_type == "conversation"
        assert t.workspace == "current"  # server_default applied
        assert t.archived is False
        assert t.source_metadata == {"branched_from": 3}

        events = (
            s.execute(select(Event).where(Event.thread_id == t.id).order_by(Event.id))
            .scalars()
            .all()
        )
        assert [e.payload["text"] for e in events] == ["hi", "hello back"]
        assert [e.dedup_key for e in events] == ["k1", "k2"]
        assert events[0].recorded_at is not None  # server_default timestamp
    eng2.dispose()


def test_init_db_idempotent(tmp_path) -> None:
    eng = build_engine(_dsn(tmp_path))
    init_db(eng)
    init_db(eng)  # second call must not raise
    with use_engine(eng), get_session() as s:
        s.add(Thread(name="only"))
        s.commit()
    eng.dispose()


def test_init_db_concurrent_first_open(tmp_path) -> None:
    # Two threads opening a virgin store at once (the MCP server's warm-models
    # search racing the first tool call) must both succeed — neither may die on
    # "table already exists".
    import threading

    eng = build_engine(_dsn(tmp_path))
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def opener() -> None:
        try:
            barrier.wait()
            init_db(eng)
        except BaseException as exc:  # noqa: BLE001 — collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with use_engine(eng), get_session() as s:
        s.add(Thread(name="raced"))
        s.commit()
    eng.dispose()


def test_init_db_retries_cross_process_create_race(tmp_path, monkeypatch) -> None:
    # A racer in ANOTHER process can't be serialized by the in-process lock;
    # init_db must absorb its "already exists" and re-run create_all.
    from sqlalchemy.exc import OperationalError

    from thread_archive._store import schema as schema_mod

    eng = build_engine(_dsn(tmp_path))
    real_create_all = schema_mod.Base.metadata.create_all
    calls = {"n": 0}

    def flaky_create_all(bind, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            real_create_all(bind, **kw)  # the "other process" wins the race...
            raise OperationalError("CREATE TABLE threads", {}, Exception("table threads already exists"))
        return real_create_all(bind, **kw)

    monkeypatch.setattr(schema_mod.Base.metadata, "create_all", flaky_create_all)
    init_db(eng)  # must not raise
    assert calls["n"] == 2
    with use_engine(eng), get_session() as s:
        s.add(Thread(name="survived"))
        s.commit()
    eng.dispose()


def test_add_missing_column_backfills_and_is_idempotent(tmp_path) -> None:
    # A store that predates an added column gets it ALTERed in on open — the ORM
    # selects every mapped column, so a missing one fails every query on that table.
    from sqlalchemy import text

    from thread_archive._store import schema as schema_mod

    eng = build_engine(_dsn(tmp_path))
    init_db(eng)
    # Simulate a pre-column index by dropping the column init_db just created.
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE import_state DROP COLUMN last_content_hash"))
        have = {r[1] for r in conn.execute(text("PRAGMA table_info(import_state)"))}
    assert "last_content_hash" not in have

    schema_mod._add_missing_columns(eng)  # the on-open backfill
    with eng.begin() as conn:
        have = {r[1] for r in conn.execute(text("PRAGMA table_info(import_state)"))}
    assert "last_content_hash" in have
    schema_mod._add_missing_columns(eng)  # second pass is a no-op, not a re-ALTER
    eng.dispose()


def test_init_db_opens_a_store_predating_every_metrics_column(tmp_path) -> None:
    # Provisioning steps must not depend on each other's order. A store old enough to
    # be missing both metrics columns has to open on the first try — reaching across
    # tables to fix up data mid-ALTER made that a guaranteed crash on exactly the
    # oldest installs, the ones with the most to lose.
    from sqlalchemy import text

    eng = build_engine(_dsn(tmp_path))
    init_db(eng)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE thread_metrics DROP COLUMN cache_read_tokens"))
        conn.execute(text("ALTER TABLE metrics_cursor DROP COLUMN projection_version"))

    init_db(eng)  # must not raise

    with eng.begin() as conn:
        tm = {r[1] for r in conn.execute(text("PRAGMA table_info(thread_metrics)"))}
        mc = {r[1] for r in conn.execute(text("PRAGMA table_info(metrics_cursor)"))}
    assert "cache_read_tokens" in tm
    assert "projection_version" in mc
    eng.dispose()


def test_init_db_drops_superseded_projections(tmp_path) -> None:
    # The rollup's old shape is a disposable re-derivation, so it is dropped rather
    # than carried — a stale table left behind reads exactly like a live one.
    from sqlalchemy import text

    eng = build_engine(_dsn(tmp_path))
    init_db(eng)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE request_cache_metrics (thread_id TEXT)"))
        conn.execute(
            text("ALTER TABLE metrics_cursor ADD COLUMN cache_requests_ready BOOLEAN")
        )

    init_db(eng)

    with eng.begin() as conn:
        tables = {
            r[0]
            for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        mc = {r[1] for r in conn.execute(text("PRAGMA table_info(metrics_cursor)"))}
    assert "request_cache_metrics" not in tables
    assert "cache_requests_ready" not in mc
    eng.dispose()


def test_stale_projection_version_rebuilds_instead_of_accumulating(tmp_path) -> None:
    # Sums folded by an older definition of the projection cannot be added to by a
    # newer one. The refresh owns that call — schema provisioning never touches data.
    from sqlalchemy import text

    from thread_archive._store._metrics import PROJECTION_VERSION, refresh_metrics

    eng = build_engine(_dsn(tmp_path))
    init_db(eng)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO threads (id, name, thread_type, source, archived) "
                "VALUES ('t1', 'n1', 'conversation', 'demo-harness', 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at) "
                "VALUES ('t1', 's0', 'api_request_completed', :payload, :at)"
            ),
            {
                "payload": '{"model": "m1", "input_tokens": 7, "output_tokens": 3}',
                "at": "2026-06-01T10:00:00Z",
            },
        )
        # A rollup left behind by an older shape: wrong sums, cursor already advanced.
        conn.execute(
            text(
                "INSERT INTO thread_metrics (thread_id, model, requests, input_tokens) "
                "VALUES ('t1', 'm1', 1, 99)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO metrics_cursor (id, through_event_id, projection_version) "
                "VALUES (1, 999, 0)"
            )
        )

    refresh_metrics(eng)

    with eng.begin() as conn:
        row = conn.execute(
            text("SELECT requests, input_tokens, output_tokens FROM thread_metrics")
        ).all()
        version = conn.execute(
            text("SELECT projection_version FROM metrics_cursor WHERE id = 1")
        ).scalar()
    assert row == [(1, 7, 3)]  # rebuilt from the events, not added to the stale 99
    assert version == PROJECTION_VERSION
    eng.dispose()


def test_add_missing_column_absorbs_duplicate_column_but_not_other_faults(tmp_path) -> None:
    # The ALTER can find the column already there — a racer (the daemon and MCP
    # server open the store concurrently, so another process can win the ADD
    # between our PRAGMA read and our ALTER) or, as here, a declaration whose
    # spelling the PRAGMA comparison doesn't match though SQLite does. Either way
    # SQLite answers "duplicate column name" and the outcome is already what the
    # backfill wanted, so it is absorbed — but any OTHER OperationalError is a
    # real fault and must still propagate.
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from thread_archive._store import schema as schema_mod

    eng = build_engine(_dsn(tmp_path))
    init_db(eng)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE import_state DROP COLUMN last_content_hash"))
        conn.execute(text("ALTER TABLE import_state ADD COLUMN LAST_CONTENT_HASH TEXT"))
        have = {row[1] for row in conn.execute(text("PRAGMA table_info(import_state)"))}
    assert "last_content_hash" not in have, "the backfill will try to add it"

    schema_mod._add_missing_columns(eng)  # duplicate-column answer is absorbed, no raise
    with eng.begin() as conn:  # and the column that was already there is untouched
        have = {row[1] for row in conn.execute(text("PRAGMA table_info(import_state)"))}
    assert "LAST_CONTENT_HASH" in have and "last_content_hash" not in have
    eng.dispose()

    # A real non-duplicate fault: the index file cannot be written at all.
    other = tmp_path / "readonly.db"
    eng = build_engine(f"sqlite:///{other}")
    init_db(eng)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE import_state DROP COLUMN last_content_hash"))
    eng.dispose()
    other.chmod(0o400)
    eng = build_engine(f"sqlite:///{other}")
    try:
        with pytest.raises(OperationalError, match="readonly"):
            schema_mod._add_missing_columns(eng)
    finally:
        eng.dispose()
        other.chmod(0o600)


def test_thread_name_unique(tmp_path) -> None:
    eng = build_engine(_dsn(tmp_path))
    init_db(eng)
    with use_engine(eng), get_session() as s:
        s.add(Thread(name="dup"))
        s.commit()
    with use_engine(eng), get_session() as s:
        s.add(Thread(name="dup"))
        try:
            s.commit()
            raised = False
        except IntegrityError:
            raised = True
    assert raised, "duplicate thread name should violate the unique constraint"
    eng.dispose()


def test_import_state_watermark_unique(tmp_path) -> None:
    eng = build_engine(_dsn(tmp_path))
    init_db(eng)
    with use_engine(eng), get_session() as s:
        s.add(ImportState(source="claude-code", source_id="proj:uuid", last_line_count=10))
        s.commit()
    with use_engine(eng), get_session() as s:
        row = s.execute(
            select(ImportState).where(
                ImportState.source == "claude-code", ImportState.source_id == "proj:uuid"
            )
        ).scalar_one()
        assert row.last_line_count == 10
    eng.dispose()


def test_add_missing_columns_skips_absent_tables(tmp_path) -> None:
    # The backfill only ALTERs tables that exist: on a virgin engine (no tables
    # yet — init_db hasn't run) the pass is a silent no-op, never a CREATE.
    from sqlalchemy import text

    from thread_archive._store import schema as schema_mod

    eng = build_engine(_dsn(tmp_path))
    schema_mod._add_missing_columns(eng)  # must not raise or create anything
    with eng.begin() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert not tables & set(schema_mod._ADDED_COLUMNS)
    eng.dispose()


def test_init_db_raises_when_create_race_never_resolves(tmp_path, monkeypatch) -> None:
    # The "already exists" retry is bounded: a create_all that keeps failing the
    # same way (a wedged store, not a transient racer) propagates after the last
    # attempt instead of looping forever.
    import pytest
    from sqlalchemy.exc import OperationalError

    from thread_archive._store import schema as schema_mod

    eng = build_engine(_dsn(tmp_path))
    calls = {"n": 0}

    def always_races(bind, **kw):
        calls["n"] += 1
        raise OperationalError("CREATE TABLE threads", {}, Exception("table threads already exists"))

    monkeypatch.setattr(schema_mod.Base.metadata, "create_all", always_races)
    with pytest.raises(OperationalError):
        init_db(eng)
    assert calls["n"] == 3  # all attempts burned before raising
    eng.dispose()


def test_init_db_propagates_non_race_create_error(tmp_path, monkeypatch) -> None:
    # Only the "already exists" race is absorbed — any other OperationalError is
    # a real fault and raises on the first attempt, no retry.
    import pytest
    from sqlalchemy.exc import OperationalError

    from thread_archive._store import schema as schema_mod

    eng = build_engine(_dsn(tmp_path))
    calls = {"n": 0}

    def real_fault(bind, **kw):
        calls["n"] += 1
        raise OperationalError("CREATE TABLE threads", {}, Exception("disk I/O error"))

    monkeypatch.setattr(schema_mod.Base.metadata, "create_all", real_fault)
    with pytest.raises(OperationalError):
        init_db(eng)
    assert calls["n"] == 1
    eng.dispose()
