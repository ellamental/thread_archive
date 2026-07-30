"""Parser-output validation is wired into the import path as a log-only signal.

The ``validators/`` package does nothing unless something drives it;
``log_parse_validation`` is that driver, called at the two parse seams — the
incremental claude-code slice (``batch_safe=True``) and the full account-export
conversation (``batch_safe=False``). These pin that the signal fires on format
drift, stays silent on clean input, never rejects an import, honours the
partial-slice contract, and is actually reached by the live incremental path.
"""

from __future__ import annotations

import json
import logging

from thread_archive._importers._events import log_parse_validation
from thread_archive._importers._validation_ledger import (
    ADDITIVE_GRACE_DAYS,
    LEDGER_FILE,
    VERSION_SIGHTING_LEAD,
    record_drift,
    summarize_drift,
)
from thread_archive._ops.coverage import DEGRADED_DRIFT_MIN

_EVENTS_LOGGER = "thread_archive._importers._events"


def _msg(role="user", *, text="hi", block_type="text", created_at="2026-01-01T10:00:00Z"):
    m = {
        "role": role,
        "content_text": text,
        "content_blocks": [{"type": block_type, "text": text}],
        "provider_conversation_id": "c1",
        "provider_message_id": f"{role}-1",
    }
    if created_at is not None:
        m["created_at"] = created_at
    return m


def _validation_logs(caplog):
    return [r.getMessage() for r in caplog.records if "parse-validation" in r.getMessage()]


def test_clean_conversation_logs_nothing(caplog):
    # A well-formed user+assistant exchange trips no validator, in either regime.
    msgs = [_msg("user"), _msg("assistant", text="yo")]
    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        log_parse_validation(msgs, provider="claude", conversation_id="c1", batch_safe=True)
        log_parse_validation(msgs, provider="claude", conversation_id="c1", batch_safe=False)
    assert _validation_logs(caplog) == []


def test_unknown_block_type_surfaces_as_drift(caplog, archive_home):
    # TypeValidator is batch-safe: a block type the parser has gone blind to surfaces
    # even on a partial slice — the whole point of the signal.
    msgs = [_msg("user", block_type="wobble")]
    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        log_parse_validation(msgs, provider="claude-code", conversation_id="c1", batch_safe=True)
    assert any("Unknown content block type 'wobble'" in m for m in _validation_logs(caplog))


def test_parser_preservation_block_types_are_not_drift(caplog, archive_home):
    # The claude-code parser deliberately emits attachment / model_change /
    # unknown_line blocks as preservation records. Its own output must not
    # saturate the drift ledger — that noise buried real drift for weeks.
    msgs = [
        _msg("system", block_type="attachment"),
        _msg("user", block_type="model_change"),
    ]
    msgs.append(_msg("system", block_type="unknown_line"))
    msgs[-1]["content_blocks"][0]["line_type"] = "ai-title"
    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        log_parse_validation(msgs, provider="claude-code", conversation_id="c1", batch_safe=True)
    assert _validation_logs(caplog) == []


def test_genuinely_new_line_type_is_drift_named_specifically(caplog, archive_home):
    # An unknown_line whose line_type the parser has never declared IS the
    # drift signal — and the finding names the new line kind, not the wrapper.
    msgs = [_msg("system", block_type="unknown_line")]
    msgs[0]["content_blocks"][0]["line_type"] = "holo-transcript"
    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        log_parse_validation(msgs, provider="claude-code", conversation_id="c1", batch_safe=True)
    assert any(
        "Unmodeled source line type 'holo-transcript'" in m for m in _validation_logs(caplog)
    )
    assert not any("'unknown_line'" in m for m in _validation_logs(caplog))


def test_empty_messages_is_a_noop(caplog):
    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        log_parse_validation([], provider="claude-code", conversation_id="c1", batch_safe=True)
    assert caplog.records == []


def test_batch_safe_defers_the_aggregate_and_content_checks(caplog, archive_home):
    # A missing created_at is a ContentValidator (non-batch-safe) finding. On a slice
    # it must stay quiet — the tail of a growing session is legitimately partial — but
    # a full-conversation import must surface it.
    msgs = [_msg("user", created_at=None)]
    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        log_parse_validation(msgs, provider="claude-code", conversation_id="c1", batch_safe=True)
    assert _validation_logs(caplog) == []

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        log_parse_validation(msgs, provider="claude-code", conversation_id="c1", batch_safe=False)
    assert any("lacks created_at" in m for m in _validation_logs(caplog))


def test_unknown_provider_falls_back_permissively_without_raising(caplog, archive_home):
    # An unrecognised provider must never crash an import; the universal block-type
    # check still runs under the permissive fallback config.
    msgs = [_msg("user", block_type="wobble")]
    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        log_parse_validation(msgs, provider="who-dis", conversation_id="c1", batch_safe=True)
    assert any("wobble" in m for m in _validation_logs(caplog))


# ── the wiring itself: the live incremental path drives the validator ──────────

_USER_LINE = {
    "type": "user",
    "uuid": "u1",
    "timestamp": "2026-01-01T10:00:00Z",
    "sessionId": "s1",
    "cwd": "/proj",
    "message": {"role": "user", "content": "hello world"},
}
_ASSISTANT_LINE = {
    "type": "assistant",
    "uuid": "a1",
    "timestamp": "2026-01-01T10:00:05Z",
    "sessionId": "s1",
    "message": {
        "role": "assistant",
        "model": "claude-opus-4",
        "content": [{"type": "text", "text": "hi there"}],
    },
}


def test_incremental_import_drives_validation(archive_home, caplog):
    """A real incremental claude-code import reaches the validator with the parsed
    slice and the partial-slice contract — proving the seam is live, not just the
    helper in isolation.

    The session carries three findings' worth of material: a block type the parser
    has gone blind to (``TypeValidator``, batch-safe), a line with no timestamp
    (``ContentValidator``) and no thinking anywhere (``ThinkingBlockValidator``) —
    the last two aggregate/whole-conversation checks. Only the batch-safe one may
    surface, or the incremental path is judging a growing session as if it were
    finished.
    """
    from thread_archive._importers import import_session_incremental
    from thread_archive._store import init_db

    init_db()
    lines = [
        _USER_LINE,
        # a block type the parser has never declared → batch-safe drift
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "timestamp": "2026-01-01T10:00:05Z", "sessionId": "s1",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "wobble", "text": "???"}]}},
        # no timestamp: a ContentValidator finding, and the turn-less thread is a
        # ThinkingBlockValidator one — both deferred on a slice
        {"type": "user", "uuid": "u2", "sessionId": "s1",
         "message": {"role": "user", "content": "no clock"}},
    ]
    path = archive_home / "s1.jsonl"
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        result = import_session_incremental(path, "s1")

    assert result.events_created > 0  # the import really ran
    found = _validation_logs(caplog)
    assert any("Unknown content block type 'wobble'" in m for m in found), \
        "the validator was never reached by the import path"
    assert all("claude-code" in m for m in found), "the source provider was not carried in"
    # the partial-slice contract: the aggregate checks stayed out of it
    assert not any("lacks created_at" in m for m in found)
    assert not any("thinking blocks" in m for m in found)


# ── the durable surface: findings land on the validation-drift ledger ──────────


def _drift_records(home):
    path = home / LEDGER_FILE
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_finding_appends_a_drift_ledger_record(archive_home):
    log_parse_validation(
        [_msg("user", block_type="wobble")],
        provider="claude-code", conversation_id="proj:s1", batch_safe=True,
    )
    records = _drift_records(archive_home)
    assert len(records) == 1
    rec = records[0]
    assert rec["provider"] == "claude-code"
    assert rec["source_id"] == "proj:s1"
    assert rec["batch_safe"] is True
    assert rec["count"] == 1
    assert any("wobble" in f for f in rec["findings"])
    assert summarize_drift()["recent"] == 1


def test_clean_import_writes_no_drift_record(archive_home):
    log_parse_validation(
        [_msg("user"), _msg("assistant", text="yo")],
        provider="claude", conversation_id="s1", batch_safe=True,
    )
    assert _drift_records(archive_home) == []
    assert summarize_drift() == {"total": 0, "recent": 0, "recent_substantive": 0,
                                 "recent_substantive_findings": 0, "recent_due": 0,
                                 "recent_due_findings": 0, "recent_deferred": 0,
                                 "recent_findings": 0, "recent_resolved": 0,
                                 "days": 7.0, "grace_days": ADDITIVE_GRACE_DAYS,
                                 "by_provider": {}}


def test_the_seam_flags_a_preserved_addition_as_additive(archive_home):
    # An unknown block type is carried through, not dropped, so the record says so
    # — the ledger can't re-derive that later, when the finding is just prose.
    log_parse_validation(
        [_msg("user", block_type="wobble")],
        provider="claude-code", conversation_id="proj:s1", batch_safe=True,
    )
    assert _drift_records(archive_home)[0]["additive"] is True


def test_a_record_that_also_reports_a_loss_is_not_additive(archive_home):
    # A missing timestamp is content the archive does not have. Mixed with an
    # addition, the loss decides — the record gets no grace window.
    log_parse_validation(
        [_msg("user", block_type="wobble", created_at=None)],
        provider="claude-code", conversation_id="proj:s1", batch_safe=False,
    )
    rec = _drift_records(archive_home)[0]
    assert rec["additive"] is False
    assert any("wobble" in f for f in rec["findings"])
    assert any("lacks created_at" in f for f in rec["findings"])


def test_record_drift_empty_findings_is_a_noop(archive_home):
    record_drift("claude-code", "s1", findings=[], batch_safe=True)
    assert not (archive_home / LEDGER_FILE).exists()


def test_drift_surfaces_in_the_coverage_check(archive_home):
    # The whole point of the ledger: drift shows up in `thread-archive source coverage` / the
    # nightly, not just in daemon logs.
    from thread_archive._ops.coverage import check_coverage
    from thread_archive._ops.health import read_health

    record_drift(
        "claude-code", "s1",
        findings=["Unknown content block type 'wobble'"], batch_safe=True,
    )
    r = check_coverage(watchers=[])
    drift = r["drift"]
    assert drift["by_provider"]["claude-code"].pop("since")  # volatile timestamp
    assert drift == {"total": 1, "recent": 1, "recent_substantive": 1,
                     "recent_substantive_findings": 1, "recent_due": 1,
                     "recent_due_findings": 1, "recent_deferred": 0,
                     "recent_findings": 1, "recent_resolved": 0,
                     "days": 7.0, "grace_days": ADDITIVE_GRACE_DAYS,
                     "by_provider": {"claude-code": {"recent": 1, "recent_substantive": 1,
                                                     "recent_due": 1, "recent_deferred": 0,
                                                     "recent_findings": 1,
                                                     "recent_resolved": 0}}}
    assert read_health()["coverage_last"]["drift_recent"] == 1


# ── advisory records are trail, not evidence ──────────────────────────────────


def _sighting(version: str) -> str:
    return (
        f"{VERSION_SIGHTING_LEAD} claude-code version '{version}' - format changes "
        f"ride version bumps; if field/line-type warnings follow, this is the "
        f"release that grew them (advisory)"
    )


def test_version_sightings_never_degrade_a_source(archive_home):
    # A harness that ships a release most days trips the tripwire most days. Those
    # are heads-ups, not findings: they stay in the trail, but they must not push a
    # perfectly healthy parser over the degradation threshold or raise the warning.
    from thread_archive._ops.coverage import check_coverage

    for v in ("2.1.218", "2.1.219", "2.1.220", "2.1.221"):
        record_drift("claude-code", f"proj:{v}", findings=[_sighting(v)],
                     batch_safe=True, advisory=True)
    r = check_coverage(watchers=[])
    assert r["drift"]["recent"] == 4  # the trail keeps every record
    assert r["drift"]["recent_substantive"] == 0
    assert r["drift"]["by_provider"]["claude-code"]["since"] is None
    assert r["degraded"] == {}
    assert not [w for w in r["warnings"] if "format drift" in w]


def test_real_drift_still_degrades_past_the_threshold(archive_home):
    # The flip side: advisories must not mask the signal they precede.
    from thread_archive._ops.coverage import check_coverage

    for i in range(2):
        record_drift("claude-code", f"proj:v{i}", findings=[_sighting(f"9.9.{i}")],
                     batch_safe=True, advisory=True)
    for i in range(DEGRADED_DRIFT_MIN):
        record_drift("claude-code", f"proj:s{i}",
                     findings=["Unmodeled source line field 'user.toolEndsTurn'"],
                     batch_safe=True)
    r = check_coverage(watchers=[])
    assert r["drift"]["recent_substantive"] == DEGRADED_DRIFT_MIN
    # The warning counts findings the same way it counts records — advisories out.
    assert r["drift"]["recent_findings"] == DEGRADED_DRIFT_MIN + 2
    assert r["drift"]["recent_substantive_findings"] == DEGRADED_DRIFT_MIN
    assert r["degraded"]["claude-code"]["reason"] == "validation_drift"
    # ``since`` dates the drift, not the advisory that happened to precede it.
    assert r["degraded"]["claude-code"]["since"] == _drift_records(archive_home)[2]["at"]
    assert [w for w in r["warnings"] if "format drift" in w]


def test_unflagged_records_are_classified_by_their_findings(archive_home):
    # The ledger is append-only and outlives any one writer, so a record carrying
    # no ``advisory`` flag is read by what it says: all-sightings is advisory,
    # anything else is drift.
    path = archive_home / LEDGER_FILE
    path.write_text("".join(
        json.dumps({"at": "2026-07-23T13:02:10.676181+00:00", "provider": "claude-code",
                    "source_id": f"proj:{i}", "batch_safe": True,
                    "count": len(findings), "findings": findings}) + "\n"
        for i, findings in enumerate([
            [_sighting("2.1.218")],
            [_sighting("2.1.219")],
            [_sighting("2.1.220")],
            [_sighting("2.1.221"), "Unknown content block type 'attachment'"],
        ])
    ), encoding="utf-8")
    drift = summarize_drift(days=36500.0)
    assert drift["recent"] == 4
    assert drift["recent_substantive"] == 1  # only the mixed record counts
