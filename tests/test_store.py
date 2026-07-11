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
