"""An ingest fault must outlive the condition that caused it.

The perishable records answer "is ingest healthy now": ``watch_errors_last`` holds
the last five messages and is cleared the moment a poll comes back green. Nothing
answered "was it ever unhealthy, for how long, and how badly" — and that is the
question that matters for an archive, because a fault means conversations aren't
being captured while the harness prunes its transcripts anyway. A fault that ran
for days and then cleared is the exact shape of silent permanent loss, and the
exact shape a cleared-on-green record erases.

The other half of the contract is volume. A broken source fails every poll for as
long as it stays broken, so the ledger folds by signature and writes at decade
counts — otherwise the record of an outage is itself an outage-sized file.
"""

from __future__ import annotations

import json

from thread_archive._ops import ingest_errors
from thread_archive._ops.ledger import iter_rows

# The real fault this ledger exists for: a file-descriptor exhaustion that fired
# tens of thousands of times across two days, varying only in which session it
# failed on.
_ERRNO24 = (
    "claude-code import error for -Users-ella-dev-thread:{uuid}: "
    "[Errno 24] Too many open files"
)
#: Two canonical and one reshaped — providers do not reliably hand out well-formed
#: uuids, and the ids that fold worst are the ones that need folding most.
_UUIDS = [
    "7d1d0dab-19a5-4f30-8ad9-a1fb35719c02",
    "fca50e91-4e72-4ecb-baac-90ab6f2d1e77",
    "3e4c1b25-c8-4c9-9b1a-5d2fa7bca1a4",
]


def _rows(home):
    return list(iter_rows(home / ingest_errors.LEDGER_FILE))


def _record(home, messages):
    ingest_errors.record(messages, home=home)


def test_recurring_fault_folds_onto_one_signature(archive_home) -> None:
    """The same fault on different sessions is one fault, not three."""
    ingest_errors.reset_tally()
    _record(archive_home, [_ERRNO24.format(uuid=u) for u in _UUIDS])

    rows = _rows(archive_home)
    assert len(rows) == 1, [r["signature"] for r in rows]
    assert rows[0]["source"] == "claude-code"
    assert "Too many open files" in rows[0]["sample"]
    # The varying session id is gone from the signature; the class is what's left.
    assert not any(u in rows[0]["signature"] for u in _UUIDS)


def test_first_sighting_is_never_delayed(archive_home) -> None:
    """A new fault is recorded the moment it appears — the row that matters most
    is the one that says 'this started happening'."""
    ingest_errors.reset_tally()
    _record(archive_home, ["cursor: scan failed: database is locked"])

    rows = _rows(archive_home)
    assert len(rows) == 1
    assert rows[0]["count"] == 1
    assert rows[0]["source"] == "cursor"


def test_an_outage_costs_a_handful_of_rows(archive_home) -> None:
    """Sixty-five thousand occurrences must not become sixty-five thousand rows —
    the ledger recording an outage cannot itself be the size of one."""
    ingest_errors.reset_tally()
    for i in range(65_000):
        _record(archive_home, [_ERRNO24.format(uuid=_UUIDS[i % len(_UUIDS)])])

    rows = _rows(archive_home)
    counts = [r["count"] for r in rows]
    assert counts == [1, 10, 100, 1000, 10000], counts
    # The magnitude is still legible even though the rows are few.
    assert rows[-1]["count"] == 10000
    assert all(r["since"] == rows[0]["at"] for r in rows), "every row dates the streak"


def test_distinct_faults_stay_distinct(archive_home) -> None:
    """Folding must not merge unrelated failures into one unreadable bucket."""
    ingest_errors.reset_tally()
    _record(archive_home, [
        _ERRNO24.format(uuid=_UUIDS[0]),
        "cursor: scan failed: database is locked",
        "codex: cannot stat db: [Errno 2] No such file or directory",
    ])

    rows = _rows(archive_home)
    assert len({r["signature"] for r in rows}) == 3
    assert {r["source"] for r in rows} == {"claude-code", "cursor", "codex"}


def test_folding_does_not_eat_the_words_that_name_the_fault(archive_home) -> None:
    """Over-collapsing is worse than splitting: it merges unrelated faults into a
    bucket nobody can take apart. The words that distinguish two failures must
    survive normalization even when they look id-ish."""
    locked = ingest_errors.signature("cursor: scan failed: database is locked")
    missing = ingest_errors.signature("cursor: scan failed: no such column: thread_id")

    assert locked != missing
    for word in ("database", "locked", "column", "thread_id", "scan", "failed"):
        assert word in locked + missing, f"normalization ate {word!r}"


def test_bare_hex_ids_fold_like_dashed_ones(archive_home) -> None:
    """Not every id arrives punctuated. A bare hex token varies exactly as a uuid
    does, and splitting on it is how one fault becomes ten thousand rows."""
    a = ingest_errors.signature("Subagent thread:agent-aa8de9c1: parent resolves to nothing")
    b = ingest_errors.signature("Subagent thread:agent-3f0b71ed: parent resolves to nothing")

    assert a == b, (a, b)


def test_summary_does_not_double_count_a_rising_series(archive_home) -> None:
    """A signature's rows are cumulative within one run, so a total that added them
    up would report 1111 for a fault that happened 1000 times."""
    ingest_errors.reset_tally()
    for _ in range(1000):
        _record(archive_home, [_ERRNO24.format(uuid=_UUIDS[0])])

    summary = ingest_errors.summarize(archive_home)
    assert len(summary) == 1
    assert summary[0]["count"] == 1000
    assert summary[0]["source"] == "claude-code"


def test_summary_count_is_a_floor_between_thresholds(archive_home) -> None:
    """Occurrences past the last decade are real but unwritten, so the summary
    undercounts by design — anything displaying it must not imply an exact tally."""
    ingest_errors.reset_tally()
    for _ in range(1200):
        _record(archive_home, [_ERRNO24.format(uuid=_UUIDS[0])])

    count = ingest_errors.summarize(archive_home)[0]["count"]
    assert count == 1000, "the floor is the last threshold crossed"
    assert count < 1200, "and it is genuinely below the true number"


def test_counts_restart_with_the_process_and_say_so(archive_home) -> None:
    """Counts are per process. A restart's tally is a new streak, and ``since``
    is what lets a reader add two runs instead of mistaking one for the other."""
    ingest_errors.reset_tally()
    for _ in range(10):
        _record(archive_home, [_ERRNO24.format(uuid=_UUIDS[0])])
    ingest_errors.reset_tally()  # the daemon restarts
    for _ in range(10):
        _record(archive_home, [_ERRNO24.format(uuid=_UUIDS[0])])

    assert ingest_errors.summarize(archive_home)[0]["count"] == 20


def test_a_write_failure_never_breaks_the_poll(archive_home) -> None:
    """Telemetry is advisory: the ledger's own failure must not reach the caller."""
    ingest_errors.reset_tally()
    # A directory where the ledger file belongs makes every append fail.
    (archive_home / ingest_errors.LEDGER_FILE).mkdir()

    _record(archive_home, [_ERRNO24.format(uuid=_UUIDS[0])])  # must not raise


def test_disabled_by_env(archive_home, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_INGEST_ERRORS_LOG", "0")
    ingest_errors.reset_tally()
    _record(archive_home, [_ERRNO24.format(uuid=_UUIDS[0])])

    assert not (archive_home / ingest_errors.LEDGER_FILE).exists()


def test_rows_are_one_json_object_per_line(archive_home) -> None:
    """The ledger is readable by anything that reads JSONL, not just its own reader."""
    ingest_errors.reset_tally()
    _record(archive_home, [_ERRNO24.format(uuid=_UUIDS[0])])

    text = (archive_home / ingest_errors.LEDGER_FILE).read_text(encoding="utf-8")
    for line in text.splitlines():
        assert json.loads(line)["kind"] == "ingest-error"
