"""A schema disagreement must be reported as itself, not as its symptoms.

A daemon holds its declared models for its whole lifetime, so an index migrated,
rebuilt, or restored underneath a running one leaves the two disagreeing until
something restarts the process. What an operator sees then is every import failing
on ``no such column``, once per session, once per poll, for as long as it takes
someone to notice — a symptom that names a column and never names the
disagreement that explains it. The condition is one PRAGMA sweep to detect.

Non-fatal by design: a mismatch is usually partial, and a daemon that refuses to
start captures nothing at all, which is strictly worse than one capturing what it
still can.
"""

from __future__ import annotations

import sqlite3

from thread_archive._ops.health import read_health
from thread_archive._ops.notices import build_notices
from thread_archive._store import get_session, init_db
from thread_archive._watcher.daemon import Watcher


def _drop_a_declared_column(home) -> str:
    """Remove a column the models declare, reproducing a migrated-underneath index.

    Rebuilding the table without it is how SQLite drops a column in older versions
    and, more to the point, is what a real migration gone half-applied leaves
    behind: a live table that no longer answers for something the models still ask
    about."""
    with get_session() as s:
        s.connection().connection.execute("SELECT 1")  # ensure the file exists
    conn = sqlite3.connect(home / "index.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(threads)")]
    assert "legacy_id" in cols, cols
    keep = [c for c in cols if c != "legacy_id"]
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(f"CREATE TABLE threads_tmp AS SELECT {', '.join(keep)} FROM threads")
    conn.execute("DROP TABLE threads")
    conn.execute("ALTER TABLE threads_tmp RENAME TO threads")
    conn.commit()
    conn.close()
    return "threads.legacy_id"


def test_mismatch_is_recorded_under_its_own_health_key(archive_home) -> None:
    """The condition gets a record of its own — ``watch_errors_last`` would carry
    the symptom, and only while the failures keep arriving."""
    init_db()
    missing = _drop_a_declared_column(archive_home)

    Watcher()._check_schema()

    record = read_health().get("schema_mismatch_last")
    assert record, "a schema mismatch left no health record"
    assert missing in record.get("missing_columns", []), record


def test_mismatch_raises_a_notice_naming_the_repair(archive_home) -> None:
    """An operator reading the health page should be told what disagrees and what
    fixes it, not handed the column name out of the failing query."""
    init_db()
    _drop_a_declared_column(archive_home)
    Watcher()._check_schema()

    notices = build_notices({"last_schema_mismatch": read_health()["schema_mismatch_last"]})
    schema = [n for n in notices if n["key"] == "schema-mismatch"]

    assert schema, [n["key"] for n in notices]
    assert "legacy_id" in schema[0]["detail"]
    assert schema[0]["command"] == "thread-archive index rebuild"
    assert schema[0]["tone"] == "bad"


def test_a_matching_schema_records_nothing(archive_home) -> None:
    """The check is silent when there is nothing to say — a health key that is
    always present is one nobody reads."""
    init_db()

    Watcher()._check_schema()

    assert "schema_mismatch_last" not in read_health()


def test_a_repaired_schema_retires_the_record(archive_home) -> None:
    """Failure-only records need retiring, or a fault that was fixed keeps painting
    the page red long after the index was rebuilt.

    The repair has to be a *rebuild*. ``create_all`` provisions missing tables and
    never retrofits a column onto one that already exists — which is the whole
    reason a live index can drift from the models in the first place."""
    init_db()
    _drop_a_declared_column(archive_home)
    Watcher()._check_schema()
    assert "schema_mismatch_last" in read_health()

    conn = sqlite3.connect(archive_home / "index.db")
    conn.execute("DROP TABLE threads")
    conn.commit()
    conn.close()
    init_db()  # the table is missing now, so the declared schema is provisioned whole

    Watcher()._check_schema()

    assert "schema_mismatch_last" not in read_health()


def test_the_check_never_stops_capture(archive_home) -> None:
    """Advisory: capture is the product. A check that cannot run must not be the
    reason a daemon fails to start.

    The unreadable index is the real condition, not a stand-in for one: an index
    that cannot be introspected is exactly the state a corrupt or half-written file
    leaves behind, and it is the state where refusing to start would cost the most —
    truth is intact and still accepting appends, so capture can continue while the
    projection gets rebuilt."""
    (archive_home / "index.db").write_bytes(b"this is not a SQLite database")

    Watcher()._check_schema()  # must not raise

    # And it stayed quiet rather than inventing a mismatch out of an unreadable file.
    assert "schema_mismatch_last" not in read_health()


def test_persistent_failures_read_differently_from_a_blip(archive_home) -> None:
    """One failed poll and a source failing every poll for a day are the same
    condition and very different news. The notice has to say which."""
    blip = build_notices({"last_watch_errors": {"count_since_start": 1, "errors": ["x: boom"]}})
    outage = build_notices({"last_watch_errors": {"count_since_start": 4832, "errors": ["x: boom"]}})

    blip_detail = [n for n in blip if n["key"] == "watch-errors"][0]["detail"]
    outage_detail = [n for n in outage if n["key"] == "watch-errors"][0]["detail"]

    assert "4,832 failures" in outage_detail
    assert "failures since" not in blip_detail


def test_a_rising_count_does_not_expire_a_silence(archive_home) -> None:
    """A silence is bound to the condition. If the magnitude in the text changed its
    identity, an operator's dismissal would evaporate on the next failed poll —
    exactly while the fault is at its loudest."""
    def fingerprint(count: int) -> str:
        notices = build_notices(
            {"last_watch_errors": {"count_since_start": count, "errors": ["x: boom"]}}
        )
        return [n for n in notices if n["key"] == "watch-errors"][0]["fingerprint"]

    assert fingerprint(12) == fingerprint(4832)
