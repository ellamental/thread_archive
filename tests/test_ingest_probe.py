"""The ingest probe: where one import's time actually went.

Ingest's cost used to be one cumulative number per source. These cover the split
that replaced it — that it is measured from the shared helpers (so every source,
including a plugin's, is covered without knowing about it), that it is free when
nobody is listening, and that it never breaks an import.
"""

from __future__ import annotations

import json
from time import perf_counter

import pytest

from thread_archive._importers import _probe

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "sessionId": "s1",
        "message": {"role": "user", "content": "hello ingest"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "sessionId": "s1",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi from ingest"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def test_no_probe_installed_is_none_and_every_point_is_a_no_op() -> None:
    assert _probe.current() is None
    _probe.record("read_ms", perf_counter())  # must not raise
    _probe.count("events", 3)
    with _probe.timed("commit_ms"):
        pass


def test_times_and_counts_accumulate() -> None:
    with _probe.install() as probe:
        _probe.record("read_ms", perf_counter())
        _probe.record("read_ms", perf_counter())
        _probe.count("events", 2)
        _probe.count("events", 3)
    assert probe.read_ms >= 0.0
    assert probe.events == 5


def test_an_unknown_stage_or_counter_never_breaks_an_import() -> None:
    with _probe.install() as probe:
        _probe.record("no_such_ms", perf_counter())
        _probe.count("no_such_counter", 1)
    assert probe.ran is False


def test_timed_charges_a_stage_that_raised() -> None:
    # Work that fails slowly is the case most worth seeing.
    with _probe.install() as probe:
        with pytest.raises(ValueError):
            with _probe.timed("commit_ms"):
                raise ValueError("boom")
    assert probe.commit_ms > 0.0


def test_probes_nest_without_leaking() -> None:
    with _probe.install() as outer:
        with _probe.install() as inner:
            _probe.count("events", 1)
        assert inner.events == 1
        assert outer.events == 0, "an inner probe must not fill its parent"
        _probe.count("events", 4)
    assert outer.events == 4


def test_an_untouched_probe_is_not_an_import_that_took_no_time() -> None:
    with _probe.install() as probe:
        pass
    assert probe.ran is False


def test_as_record_omits_stages_that_did_not_run_but_keeps_counters() -> None:
    with _probe.install() as probe:
        _probe.record("read_ms", perf_counter())
        _probe.count("items", 1)
    rec = probe.as_record()
    assert "read_ms" in rec
    assert "fts_ms" not in rec, "a stage that never ran must not read as instant"
    for counter in _probe.COUNTERS:
        assert counter in rec, "counters are the denominators; always present"
    assert rec["total_ms"] == pytest.approx(round(probe.total_ms(), 1))


def test_an_import_fills_the_stages_it_actually_ran(archive_home, tmp_path) -> None:
    from thread_archive._importers.claude_code import import_session_incremental
    from thread_archive._store import init_db

    init_db()
    src = tmp_path / "s1.jsonl"
    _write_cc(src, [USER, ASSISTANT])

    with _probe.install() as probe:
        result = import_session_incremental(src, "proj:s1")

    assert result.events_created > 0
    rec = probe.as_record()
    # The whole pipeline ran: bytes off disk, through the parser and the builder,
    # into the store and the search index.
    for stage in ("read_ms", "parse_ms", "cursor_ms", "normalize_ms",
                  "build_ms", "write_ms", "fts_ms", "commit_ms"):
        assert stage in rec, f"{stage} should have been measured"
    assert probe.items == 1
    assert probe.lines == 2
    assert probe.events == result.events_created
    assert probe.bytes == src.stat().st_size


def test_an_unchanged_file_costs_a_read_and_the_proof_but_no_parse(
    archive_home, tmp_path
) -> None:
    """The common poll: nothing changed, so nothing is parsed.

    The watermark resolves from the file's *bytes*, so an unchanged transcript is
    answered by the read and the digest alone. Parsing it would be a full JSON
    pass over the whole file to learn nothing — and on the first poll after a
    restart, when the fingerprint cache is empty, that is every file in the
    archive at once. This is the guard against the parse creeping back in front of
    the watermark check."""
    from thread_archive._importers.claude_code import import_session_incremental
    from thread_archive._store import init_db

    init_db()
    src = tmp_path / "s1.jsonl"
    _write_cc(src, [USER, ASSISTANT])
    import_session_incremental(src, "proj:s1")

    with _probe.install() as probe:
        again = import_session_incremental(src, "proj:s1")

    assert again.events_created == 0
    rec = probe.as_record()
    assert {"read_ms", "cursor_ms"} <= set(rec), "the bytes and the proof are owed"
    assert "parse_ms" not in rec, "an unchanged file must not be parsed"
    assert probe.lines == 0, "and so no lines are counted for it"
    assert "build_ms" not in rec, "an unchanged file never reaches the builder"
    assert "fts_ms" not in rec, "and never touches the search index"
    assert probe.events == 0


def test_a_grown_file_is_parsed(archive_home, tmp_path) -> None:
    """The other half of the contract: when the file *did* change, the parse
    happens. Laziness that skipped a real append would be silent capture loss."""
    from thread_archive._importers.claude_code import import_session_incremental
    from thread_archive._store import init_db

    init_db()
    src = tmp_path / "s1.jsonl"
    _write_cc(src, [USER, ASSISTANT])
    import_session_incremental(src, "proj:s1")

    more = dict(ASSISTANT, uuid="a2")
    with open(src, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(USER, uuid="u2")) + "\n")
        fh.write(json.dumps(more) + "\n")

    with _probe.install() as probe:
        grown = import_session_incremental(src, "proj:s1")

    assert grown.events_created > 0
    rec = probe.as_record()
    assert "parse_ms" in rec and probe.lines == 4
    assert {"build_ms", "write_ms", "fts_ms"} <= set(rec)
