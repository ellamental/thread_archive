"""Closing a ledger record: how a repaired drift stops asking to be repaired.

The ledgers are append-only, so a repair can't erase the drift it fixed — and
mustn't, since that trail is what a later regression is read against. Instead a
re-import appends a *resolution*, and the closed observations drop out of
``recent_substantive`` (the count coverage degrades on) while staying in the
file, in ``total``, and in ``recent``.

The property that makes this trustworthy rather than a mute button: the stamp is
taken *before* the re-read, so findings the re-parse itself writes land after it
and stay open. Half these tests are that negative — a repair that didn't work
must close nothing, and the source must stay degraded.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from thread_archive import _api as ta
from thread_archive import _repair
from thread_archive._importers import _skip_ledger as skips
from thread_archive._importers import _validation_ledger as drift
from thread_archive._ops.coverage import check_coverage, remedy_for

from .helpers import cc_assistant, cc_user, write_jsonl

DRIFT = "validation-drift.jsonl"
SKIPS = "capture-skips.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(**kw) -> str:
    return (_now() - timedelta(**kw)).isoformat()


def _write(home, filename: str, records: list[dict]) -> None:
    (home / filename).write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def _drift_rec(source_id: str, *, at: str, provider: str = "claude-code", **kw) -> dict:
    return {"at": at, "provider": provider, "source_id": source_id,
            "count": 1, "findings": ["Unmodeled source line type 'pr-link'"], **kw}


def _skip_rec(source_id: str, *, at: str, source: str = "claude-code", **kw) -> dict:
    return {"at": at, "source": source, "source_id": source_id, "lines_skipped": 3,
            "lines_total": 9, "reason": "empty_import_discarded", **kw}


def _lines(home, filename: str) -> list[dict]:
    return [json.loads(ln) for ln in (home / filename).read_text().splitlines() if ln.strip()]


# ── the drift ledger ─────────────────────────────────────────────────────────


def test_a_resolution_closes_the_records_it_covers(archive_home):
    _write(archive_home, DRIFT, [_drift_rec("s1", at=_ago(hours=n)) for n in (5, 4, 3)])
    assert drift.summarize_drift()["recent_substantive"] == 3

    closed = drift.record_resolution("claude-code", {"s1"}, through=_now(), by="reimport")

    assert closed == 3
    after = drift.summarize_drift()
    assert after["recent_substantive"] == 0   # the verdict's input is clear
    assert after["recent_resolved"] == 3      # and says so, rather than going quiet
    assert after["recent"] == 3 and after["total"] == 3  # the trail is undiminished
    assert after["by_provider"]["claude-code"]["recent_substantive"] == 0
    assert after["by_provider"]["claude-code"]["since"] is None


def test_a_resolution_cannot_close_a_finding_that_came_after_it(archive_home):
    """The whole safety property. A repair stamps `through` before re-reading, so a
    finding its own re-parse records is newer than the stamp and stays open."""
    through = _now() - timedelta(minutes=5)
    _write(archive_home, DRIFT, [
        _drift_rec("s1", at=_ago(hours=2)),          # before the repair
        _drift_rec("s1", at=_ago(minutes=1)),        # the repair re-broke it
    ])

    closed = drift.record_resolution("claude-code", {"s1"}, through=through, by="reimport")

    assert closed == 1
    summary = drift.summarize_drift()
    assert summary["recent_substantive"] == 1  # still degraded, and rightly
    assert summary["recent_resolved"] == 1


def test_closing_one_file_leaves_another_files_records_open(archive_home):
    _write(archive_home, DRIFT, [
        _drift_rec("s1", at=_ago(hours=2)),
        _drift_rec("s2", at=_ago(hours=2)),
    ])
    assert drift.record_resolution("claude-code", {"s1"}, through=_now(), by="x") == 1
    per = drift.summarize_drift()["by_provider"]["claude-code"]
    assert per["recent_substantive"] == 1 and per["recent_resolved"] == 1


def test_closing_one_provider_leaves_another_alone(archive_home):
    _write(archive_home, DRIFT, [
        _drift_rec("s1", at=_ago(hours=2)),
        _drift_rec("s1", at=_ago(hours=2), provider="codex"),
    ])
    drift.record_resolution("claude-code", {"s1"}, through=_now(), by="x")
    by = drift.summarize_drift()["by_provider"]
    assert by["claude-code"]["recent_substantive"] == 0
    assert by["codex"]["recent_substantive"] == 1


def test_a_repeat_recheck_with_nothing_open_writes_no_record(archive_home):
    """Otherwise every scheduled recheck grows the ledger with paperwork."""
    _write(archive_home, DRIFT, [_drift_rec("s1", at=_ago(hours=2))])
    assert drift.record_resolution("claude-code", {"s1"}, through=_now(), by="x") == 1
    assert drift.record_resolution("claude-code", {"s1"}, through=_now(), by="x") == 0
    assert len(_lines(archive_home, DRIFT)) == 2  # the observation and one closure


def test_closing_an_id_with_no_records_writes_nothing(archive_home):
    _write(archive_home, DRIFT, [_drift_rec("s1", at=_ago(hours=2))])
    assert drift.record_resolution("claude-code", {"unrelated"}, through=_now(), by="x") == 0
    assert len(_lines(archive_home, DRIFT)) == 1


def test_advisory_records_are_not_closable_because_they_never_counted(archive_home):
    _write(archive_home, DRIFT, [
        {"at": _ago(hours=2), "provider": "claude-code", "source_id": "s1", "count": 1,
         "advisory": True, "findings": [f"{drift.VERSION_SIGHTING_LEAD} claude-code 2.1"]},
    ])
    assert drift.record_resolution("claude-code", {"s1"}, through=_now(), by="x") == 0
    assert drift.summarize_drift()["recent_resolved"] == 0


def test_a_resolution_is_not_itself_an_observation(archive_home):
    """It must not inflate `total`/`recent`, or a repair would read as more drift."""
    _write(archive_home, DRIFT, [_drift_rec("s1", at=_ago(hours=2))])
    drift.record_resolution("claude-code", {"s1"}, through=_now(), by="x")
    summary = drift.summarize_drift()
    assert summary["total"] == 1 and summary["recent"] == 1


def test_the_later_of_two_closures_wins(archive_home):
    """An id closed twice is closed as of the later repair — a second, wider
    re-read must not be narrowed by the first one's stamp."""
    first = _now() - timedelta(hours=3)
    _write(archive_home, DRIFT, [
        _drift_rec("s1", at=_ago(hours=4)),
        _drift_rec("s1", at=_ago(hours=2)),
    ])
    assert drift.record_resolution("claude-code", {"s1"}, through=first, by="x") == 1
    assert drift.record_resolution("claude-code", {"s1"}, through=_now(), by="x") == 1
    assert drift.summarize_drift()["recent_substantive"] == 0


def test_records_outside_the_window_are_neither_open_nor_closed(archive_home):
    _write(archive_home, DRIFT, [_drift_rec("s1", at=_ago(days=30))])
    summary = drift.summarize_drift()
    assert summary["total"] == 1 and summary["recent"] == 0


def test_a_torn_resolution_line_is_ignored_not_fatal(archive_home):
    (archive_home / DRIFT).write_text(
        json.dumps(_drift_rec("s1", at=_ago(hours=2))) + "\n"
        + '{"kind": "resolution", "provider": "claude-code", "source_ids"\n'
        + json.dumps({"at": _ago(hours=1), "kind": "resolution",
                      "provider": "claude-code", "source_ids": "not-a-list",
                      "through": _now().isoformat()}) + "\n"
    )
    summary = drift.summarize_drift()
    assert summary["recent_substantive"] == 1  # neither malformed closure took effect


def test_substantive_since_names_the_files_still_breaking(archive_home):
    mark = _now() - timedelta(minutes=10)
    _write(archive_home, DRIFT, [
        _drift_rec("old", at=_ago(hours=2)),
        _drift_rec("fresh", at=_ago(minutes=1)),
        {"at": _ago(minutes=1), "provider": "claude-code", "source_id": "advisory-only",
         "advisory": True, "count": 1, "findings": ["x"]},
    ])
    assert drift.substantive_since("claude-code", mark) == {"fresh"}


# ── the skip ledger ──────────────────────────────────────────────────────────


def test_a_resolution_closes_skip_records_too(archive_home):
    _write(archive_home, SKIPS, [_skip_rec("s1", at=_ago(hours=n)) for n in (3, 2)])
    assert skips.summarize_skips()["recent_substantive"] == 2

    assert skips.record_resolution("claude-code", {"s1"}, through=_now(), by="x") == 2

    after = skips.summarize_skips()
    assert after["recent_substantive"] == 0
    assert after["recent_resolved"] == 2
    assert after["recent"] == 2 and after["total"] == 2


def test_routine_empty_session_skips_are_not_closable(archive_home):
    """They never counted toward a verdict, so there is nothing to close."""
    _write(archive_home, SKIPS, [
        _skip_rec("s1", at=_ago(hours=2), reason="no_importable_content"),
    ])
    assert skips.record_resolution("claude-code", {"s1"}, through=_now(), by="x") == 0


def test_a_closure_does_not_disqualify_a_settled_empty_session(archive_home):
    """`settled_empty_ids` counts consumptions, and a second one means the file kept
    growing under a blind parser. A closing record is not a consumption — counted as
    one it would silently un-settle every id a repair touched, and coverage would
    start reading their store activity as unaccounted-for again."""
    _write(archive_home, SKIPS, [
        _skip_rec("settled", at=_ago(hours=2), reason="no_importable_content"),
        _skip_rec("drifty", at=_ago(hours=2)),
    ])
    assert skips.settled_empty_ids("claude-code") == {"settled"}

    skips.record_resolution("claude-code", {"settled", "drifty"}, through=_now(), by="x")

    assert skips.settled_empty_ids("claude-code") == {"settled"}


# ── the verdict ──────────────────────────────────────────────────────────────


def test_a_closed_drift_retires_the_degradation_verdict(archive_home):
    """The end-to-end reason this exists: past the threshold, a source is degraded
    and every search says so; closing the records has to actually retire that."""
    _write(archive_home, DRIFT, [_drift_rec("s1", at=_ago(hours=n)) for n in (5, 4, 3)])
    ta.open_archive()

    assert "claude-code" in check_coverage(watchers=[], snapshot=False)["degraded"]

    drift.record_resolution("claude-code", {"s1"}, through=_now(), by="reimport")

    assert check_coverage(watchers=[], snapshot=False)["degraded"] == {}


def test_a_closed_skip_run_retires_the_verdict(archive_home):
    _write(archive_home, SKIPS, [_skip_rec("s1", at=_ago(hours=n)) for n in (5, 4, 3)])
    ta.open_archive()
    assert "claude-code" in check_coverage(watchers=[], snapshot=False)["degraded"]

    skips.record_resolution("claude-code", {"s1"}, through=_now(), by="reimport")

    assert check_coverage(watchers=[], snapshot=False)["degraded"] == {}


def test_partial_closure_below_the_threshold_still_retires_it(archive_home):
    _write(archive_home, DRIFT, [
        _drift_rec("s1", at=_ago(hours=5)),
        _drift_rec("s2", at=_ago(hours=4)),
        _drift_rec("s2", at=_ago(hours=3)),
    ])
    ta.open_archive()
    drift.record_resolution("claude-code", {"s2"}, through=_now(), by="x")
    # one open record left — below DEGRADED_DRIFT_MIN, so a warning but no verdict
    r = check_coverage(watchers=[], snapshot=False)
    assert r["degraded"] == {}
    assert any("format drift" in w for w in r["warnings"])


@pytest.mark.parametrize("reason,expected", [
    ("validation_drift", "thread-archive source recheck claude-code"),
    ("capture_skips", "thread-archive source recheck claude-code"),
    ("stale_ingest", "thread-archive source recheck claude-code"),
    ("went_dark", "thread-archive source coverage"),
    ("something-new", "thread-archive source recheck claude-code"),
])
def test_each_verdict_names_a_remedy_that_can_act_on_it(reason, expected):
    """A missing store has no files to re-read; everything else does."""
    assert remedy_for(reason, "claude-code") == expected


def test_the_search_notice_carries_the_verdicts_own_remedy(archive_home):
    from thread_archive import _tools

    _write(archive_home, DRIFT, [_drift_rec("s1", at=_ago(hours=n)) for n in (5, 4, 3)])
    ta.open_archive()
    check_coverage(watchers=[], snapshot=False)

    assert "remedy: thread-archive source recheck claude-code" in _tools._degradation_notices()

    drift.record_resolution("claude-code", {"s1"}, through=_now(), by="reimport")
    check_coverage(watchers=[], snapshot=False)

    assert _tools._degradation_notices() == ""


# ── the re-import that closes them ───────────────────────────────────────────


def _live_store(tmp_path, monkeypatch, name="led", lines=None):
    """A real claude-code store under a per-test $HOME, so a repair runs end to end
    through the registry's own provider and discovery."""
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / ".claude" / "projects" / "proj" / f"{name}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(f, lines or [cc_user("first"), cc_assistant("second")])
    return f


def test_a_clean_re_read_closes_the_records_that_called_for_it(archive_home, tmp_path, monkeypatch):
    _live_store(tmp_path, monkeypatch)
    _write(archive_home, DRIFT, [_drift_rec("proj:led", at=_ago(hours=n)) for n in (5, 4, 3)])
    _write(archive_home, SKIPS, [_skip_rec("proj:led", at=_ago(hours=4))])
    ta.open_archive()
    assert "claude-code" in check_coverage(watchers=[], snapshot=False)["degraded"]

    summary = _repair.reimport_source("claude-code")

    assert summary["files_reread"] == 1
    assert summary["drift_closed"] == 3
    assert summary["skips_closed"] == 1
    assert summary["still_drifting"] == []
    assert drift.summarize_drift()["recent_substantive"] == 0
    assert check_coverage(watchers=[], snapshot=False)["degraded"] == {}


def test_a_re_read_that_drifts_again_closes_nothing(archive_home, tmp_path, monkeypatch):
    """The negative that makes the positive mean something. The store holds a line
    type no parser models, so the re-read re-records the finding — after the stamp,
    so it stays open and the source stays degraded."""
    unmodeled = [{"type": "no-such-line-type-at-all", "uuid": "u1",
                  "sessionId": "led", "timestamp": _ago(hours=1)}]
    _live_store(tmp_path, monkeypatch, lines=[cc_user("first"), *unmodeled])
    _write(archive_home, DRIFT, [_drift_rec("proj:led", at=_ago(hours=n)) for n in (5, 4, 3)])
    ta.open_archive()

    summary = _repair.reimport_source("claude-code")

    assert summary["files_reread"] == 1
    assert summary["still_drifting"] == ["proj:led"]
    assert summary["drift_closed"] == 0
    assert drift.summarize_drift()["recent_substantive"] >= 3
    assert "claude-code" in check_coverage(watchers=[], snapshot=False)["degraded"]


def test_a_file_the_provider_pruned_is_reported_not_closed(archive_home, tmp_path, monkeypatch):
    """Nothing re-read it, so nothing was proven about it. Silently closing would
    make an unfalsifiable record look repaired."""
    _live_store(tmp_path, monkeypatch)
    _write(archive_home, DRIFT, [
        _drift_rec("proj:led", at=_ago(hours=3)),
        _drift_rec("proj:vanished", at=_ago(hours=3)),
    ])
    ta.open_archive()

    summary = _repair.reimport_source("claude-code")

    assert summary["files_reread"] == 1
    assert summary["unreachable"] == 1
    assert summary["drift_closed"] == 1
    assert drift.summarize_drift()["recent_substantive"] == 1


def test_the_re_import_refreshes_the_verdict_it_just_invalidated(archive_home, tmp_path, monkeypatch):
    """health.json holds the verdict every surface reads. A repair that closes the
    records but leaves that cache standing has fixed the archive and not the thing
    telling everyone it is broken."""
    from thread_archive._ops.health import read_health

    _live_store(tmp_path, monkeypatch)
    _write(archive_home, DRIFT, [_drift_rec("proj:led", at=_ago(hours=n)) for n in (5, 4, 3)])
    ta.open_archive()
    check_coverage(watchers=[], snapshot=False)
    assert read_health()["coverage_last"]["degraded"]["claude-code"]["reason"] == (
        "validation_drift"
    )

    summary = _repair.reimport_source("claude-code")

    assert summary["coverage_refreshed"] is True
    # The refresh runs against the real machine's watchers, which read this store's
    # fixture timestamps as stale — a verdict for a different reason is the fixture
    # showing through. What matters is that the drift verdict is gone from the cache.
    after = read_health()["coverage_last"]["degraded"].get("claude-code") or {}
    assert after.get("reason") != "validation_drift"


def test_a_second_recheck_is_a_clean_no_op(archive_home, tmp_path, monkeypatch):
    _live_store(tmp_path, monkeypatch)
    _write(archive_home, DRIFT, [_drift_rec("proj:led", at=_ago(hours=3))])
    ta.open_archive()
    assert _repair.reimport_source("claude-code")["drift_closed"] == 1

    again = _repair.reimport_source("claude-code")

    assert again["drift_closed"] == 0
    assert again["still_drifting"] == []
    assert again["files_reread"] == 1  # re-read, found nothing to close


def test_nothing_ledgered_closes_nothing_and_does_not_crash(archive_home, tmp_path, monkeypatch):
    _live_store(tmp_path, monkeypatch)
    ta.open_archive()
    summary = _repair.reimport_source("claude-code")
    assert summary["drift_closed"] == summary["skips_closed"] == 0
    assert summary["unreachable"] == 0


# ── the verb ─────────────────────────────────────────────────────────────────


def test_recheck_reports_a_clean_re_read(archive_home, tmp_path, monkeypatch, capsys):
    from thread_archive import cli

    _live_store(tmp_path, monkeypatch)
    _write(archive_home, DRIFT, [_drift_rec("proj:led", at=_ago(hours=n)) for n in (5, 4)])
    ta.open_archive()

    assert cli.main(["source", "recheck", "claude-code"]) == 0

    out = capsys.readouterr().out
    assert "re-read 1 file(s) from the live store" in out
    assert "repaired: 2 ledger record(s) closed" in out


def test_recheck_exits_nonzero_when_the_drift_is_live(archive_home, tmp_path, monkeypatch, capsys):
    from thread_archive import cli

    unmodeled = [{"type": "no-such-line-type-at-all", "uuid": "u1",
                  "sessionId": "led", "timestamp": _ago(hours=1)}]
    _live_store(tmp_path, monkeypatch, lines=[cc_user("first"), *unmodeled])
    _write(archive_home, DRIFT, [_drift_rec("proj:led", at=_ago(hours=3))])
    ta.open_archive()

    assert cli.main(["source", "recheck", "claude-code"]) == 1

    out = capsys.readouterr().out
    assert "still drifting: 1 file(s)" in out
    assert "thread-archive source fix claude-code" in out


def test_recheck_names_what_it_could_not_reach(archive_home, tmp_path, monkeypatch, capsys):
    from thread_archive import cli

    _live_store(tmp_path, monkeypatch)
    _write(archive_home, DRIFT, [_drift_rec("proj:gone", at=_ago(hours=3))])
    ta.open_archive()

    assert cli.main(["source", "recheck", "claude-code"]) == 0

    out = capsys.readouterr().out
    assert "unreachable: 1 ledgered file(s)" in out


def test_recheck_refuses_an_unknown_provider(archive_home, capsys):
    from thread_archive import cli

    ta.open_archive()
    assert cli.main(["source", "recheck", "not-a-provider"]) == 1
    assert "unknown provider" in capsys.readouterr().out


def test_the_coverage_report_says_when_records_were_repaired(archive_home, capsys):
    """A repaired source and an untouched one differ only by the absent verdict
    otherwise — the recent counts read identically."""
    from thread_archive import cli

    _write(archive_home, DRIFT, [_drift_rec("s1", at=_ago(hours=n)) for n in (5, 4, 3)])
    ta.open_archive()
    drift.record_resolution("claude-code", {"s1"}, through=_now(), by="reimport")

    cli.report_coverage(check_coverage(watchers=[], snapshot=False))

    out = capsys.readouterr().out
    assert "repaired: 3 recent ledger record(s) closed by a re-import" in out
    assert "degraded:" not in out
