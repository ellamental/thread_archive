"""The grace window: a provider *addition* asks to be fixed, not to be feared.

Format drift comes in two shapes and the archive has always treated them as one.
A provider that grows a field, block type, line kind or role loses the reader
nothing — the parser preserves the value under ``annotations['unmodeled']`` and
the validator names it — so the finding is a maintenance to-do for whoever
maintains the parser. A finding that says content is *missing* is a hole.
Posting both the day they land makes the health page yellow over routine
housekeeping, and a page that cries over housekeeping stops being read on the
day it has something to say.

So: additive drift is held for ``ADDITIVE_GRACE_DAYS`` from the finding's **first
sighting**, long enough for a release or a patch to close it before anyone is
asked to look. Lossy drift is due immediately. A ``dev_mode`` install is due
immediately either way — there, the to-do is the point.

These pin the four properties that make the hold trustworthy rather than a mute
button: it never covers a loss, it is measured per finding (so a new addition
can't inherit an old one's age), it is measured from first sighting (so drift
that recurs daily can't reset its own clock), and the evidence stays in the
report the whole time.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from thread_archive._config import save_config
from thread_archive._importers._validation_ledger import (
    ADDITIVE_GRACE_DAYS,
    LEDGER_FILE,
    RESOLUTION_KIND,
    summarize_drift,
)
from thread_archive._ops.coverage import check_coverage

ADDITION = "Unmodeled source line field 'user.toolEndsTurn'"
OTHER_ADDITION = "Unknown content block type 'wobble'"
LOSS = "Message a-1 lacks created_at timestamp"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(**kw) -> str:
    return (_now() - timedelta(**kw)).isoformat()


def _rec(source_id: str, *, at: str, findings: list[str], additive: bool) -> dict:
    return {
        "at": at,
        "provider": "claude-code",
        "source_id": source_id,
        "batch_safe": True,
        "advisory": False,
        "additive": additive,
        "count": len(findings),
        "findings": findings,
    }


def _ledger(home, records: list[dict]) -> None:
    (home / LEDGER_FILE).write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def _drift_warnings(report: dict) -> list[str]:
    return [w for w in report["warnings"] if "format drift" in w]


# ── the hold ─────────────────────────────────────────────────────────────────


def test_a_fresh_addition_is_held_and_still_reported(archive_home):
    # Held, not hidden: no warning, but the record is in the drift totals and the
    # held count says so out loud. A grace window nobody can see is
    # indistinguishable from a check that stopped running.
    _ledger(archive_home, [_rec("s1", at=_ago(hours=1), findings=[ADDITION],
                                additive=True)])
    r = check_coverage(watchers=[])
    assert _drift_warnings(r) == []
    assert r["drift_held"] == 1
    assert r["drift"]["recent"] == 1
    assert r["drift"]["recent_substantive"] == 1  # still evidence, just not yet due
    assert r["drift"]["recent_due"] == 0


def test_an_addition_outstanding_past_the_window_is_due(archive_home):
    # Same finding, first seen before the window opened: the fix has had its two
    # weeks and the warning is now the point.
    _ledger(archive_home, [
        _rec("old", at=_ago(days=ADDITIVE_GRACE_DAYS + 3), findings=[ADDITION],
             additive=True),
        _rec("new", at=_ago(hours=1), findings=[ADDITION], additive=True),
    ])
    r = check_coverage(watchers=[])
    assert _drift_warnings(r), "a matured addition must warn"
    # Only the record inside the recency window is counted — the anchor dates the
    # finding, it doesn't get counted twice.
    assert r["drift"]["recent_due"] == 1
    assert r["drift_held"] == 0


def test_the_clock_runs_from_first_sighting_not_the_latest(archive_home):
    # Drift recurs on every import of the drifting session. If maturity read the
    # newest record the window would reset daily and nothing would ever come due.
    _ledger(archive_home, [
        _rec("s0", at=_ago(days=ADDITIVE_GRACE_DAYS + 5), findings=[ADDITION],
             additive=True),
        *(_rec(f"s{i}", at=_ago(hours=i), findings=[ADDITION], additive=True)
          for i in range(1, 5)),
    ])
    r = check_coverage(watchers=[])
    assert _drift_warnings(r)
    assert r["drift"]["recent_due"] == 4 and r["drift_held"] == 0


# ── what the hold must never cover ───────────────────────────────────────────


def test_a_lossy_finding_warns_the_day_it_lands(archive_home):
    # The whole distinction: content that went missing is not housekeeping.
    _ledger(archive_home, [_rec("s1", at=_ago(hours=1), findings=[LOSS],
                                additive=False)])
    r = check_coverage(watchers=[])
    assert _drift_warnings(r)
    assert r["drift_held"] == 0


def test_a_record_mixing_an_addition_with_a_loss_is_not_held(archive_home):
    # The loss decides. A parser that grew a new field AND stopped finding
    # timestamps is not two weeks' worth of relaxed.
    _ledger(archive_home, [_rec("s1", at=_ago(hours=1), findings=[ADDITION, LOSS],
                                additive=False)])
    r = check_coverage(watchers=[])
    assert _drift_warnings(r)


def test_a_record_written_before_the_flag_existed_is_never_held(archive_home):
    # The ledger outlives its writers, and an unreadable fact has to fail toward
    # showing the warning — a silence inferred from prose is one nobody agreed to.
    rec = _rec("s1", at=_ago(hours=1), findings=[ADDITION], additive=True)
    del rec["additive"]
    _ledger(archive_home, [rec])
    r = check_coverage(watchers=[])
    assert _drift_warnings(r)
    assert r["drift_held"] == 0


def test_maturity_is_per_finding_not_per_provider(archive_home):
    # A field the provider grew this morning must not inherit the age of one it
    # grew last quarter — that would make every later addition born past due.
    _ledger(archive_home, [
        _rec("old", at=_ago(days=ADDITIVE_GRACE_DAYS + 3), findings=[ADDITION],
             additive=True),
        _rec("fresh", at=_ago(hours=1), findings=[OTHER_ADDITION], additive=True),
    ])
    per = summarize_drift()["by_provider"]["claude-code"]
    assert per["recent_deferred"] == 1 and per["recent_due"] == 0


def test_a_repair_restarts_the_clock(archive_home):
    # A closed record no longer evidences the finding it recorded, so it must not
    # go on aging it: drift that comes back after a repair gets its own window,
    # not a verdict inherited from the drift the repair fixed.
    _ledger(archive_home, [
        _rec("s1", at=_ago(days=ADDITIVE_GRACE_DAYS + 3), findings=[ADDITION],
             additive=True),
        {"at": _ago(days=ADDITIVE_GRACE_DAYS + 2), "kind": RESOLUTION_KIND,
         "provider": "claude-code", "source_ids": ["s1"],
         "through": _ago(days=ADDITIVE_GRACE_DAYS + 2), "by": "recheck", "closed": 1},
        _rec("s2", at=_ago(hours=1), findings=[ADDITION], additive=True),
    ])
    r = check_coverage(watchers=[])
    assert _drift_warnings(r) == []
    assert r["drift_held"] == 1


# ── the dev switch ───────────────────────────────────────────────────────────


def test_dev_mode_warns_immediately(archive_home):
    # On an install being developed on, an unmodeled field is the work, and
    # holding it two weeks hides the thing the developer opened the page for.
    _ledger(archive_home, [_rec("s1", at=_ago(hours=1), findings=[ADDITION],
                                additive=True)])
    save_config({"dev_mode": True}, home=archive_home)
    r = check_coverage(watchers=[])
    assert _drift_warnings(r)
    assert r["drift_held"] == 0


def test_dev_mode_is_strict_true(archive_home):
    # Same discipline as dev_panels: a key holding the string "false" — or "true"
    # — is not a switch, it is a config someone got wrong.
    _ledger(archive_home, [_rec("s1", at=_ago(hours=1), findings=[ADDITION],
                                additive=True)])
    for value in ("true", "false", 1, None):
        save_config({"dev_mode": value}, home=archive_home)
        assert _drift_warnings(check_coverage(watchers=[])) == []


# ── the surface this is actually about ───────────────────────────────────────


def _drift_notices(home) -> list[dict]:
    from thread_archive import _api as ta

    return [n for n in ta.notices(home=str(home))["active"]
            if n["key"].startswith("coverage-warning-format-drift")]


def test_the_health_page_posts_nothing_for_a_held_addition(archive_home):
    # End to end, through the surface the hold exists for: coverage records its
    # warnings into health.json and the notice board judges them, so a warning
    # that was never raised is a notice that never appears.
    _ledger(archive_home, [_rec("s1", at=_ago(hours=1), findings=[ADDITION],
                                additive=True)])
    check_coverage(watchers=[])
    assert _drift_notices(archive_home) == []


def test_the_health_page_posts_a_matured_addition(archive_home):
    _ledger(archive_home, [
        _rec("old", at=_ago(days=ADDITIVE_GRACE_DAYS + 3), findings=[ADDITION],
             additive=True),
        _rec("new", at=_ago(hours=1), findings=[ADDITION], additive=True),
    ])
    check_coverage(watchers=[])
    posted = _drift_notices(archive_home)
    assert len(posted) == 1 and posted[0]["tone"] == "warn"


# ── the report says what it is holding ───────────────────────────────────────


def test_the_coverage_report_names_the_held_records(archive_home, capsys):
    from thread_archive.cli import report_coverage

    _ledger(archive_home, [_rec("s1", at=_ago(hours=1), findings=[ADDITION],
                                additive=True)])
    report_coverage(check_coverage(watchers=[]))
    out = capsys.readouterr().out
    assert "held: 1 drift record(s)" in out
    assert "dev_mode" in out
    assert "validation drift: 1 ledger records" in out  # the evidence, undiminished
