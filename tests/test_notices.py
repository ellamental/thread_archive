"""The action queue and its silences.

Notices are the judgment over the health records; a silence is an operator's
statement that they read one and accept it. What these pin is the part that
makes silencing safe on a trust page: a silence is bound to the condition it was
made about, so it cannot outlive it, grow to cover a worse fault, or hide a
recurrence.
"""

from __future__ import annotations

import json

import pytest

from thread_archive import _api as ta
from thread_archive._ops.health import record_health
from thread_archive._ops.notices import (
    build_notices,
    notice_board,
    read_silences,
    silence,
    unsilence,
)


def _records(**overrides) -> dict:
    """A status mapping with nothing wrong in it, plus whatever a test breaks."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    records = {
        "last_watch_pass": {"at": now, "pid": 1, "passes": 3, "sources": {}},
        "last_watch_errors": None,
        "last_coverage": {"at": now, "ok": True, "warnings": []},
        "last_self_update": {"at": now, "ok": True, "action": "up-to-date"},
        "backup_same_device": False,
        "libraries": [],
        "pipeline": {"ran": True, "ok": True, "failed_stages": [], "nightly_at": now,
                     "dest": "/Volumes/backup/thread-archive"},
    }
    records.update(overrides)
    return records


def _keys(notices: list[dict]) -> list[str]:
    return [n["key"] for n in notices]


# ---- what a notice is ------------------------------------------------------
def test_a_clean_archive_asks_for_nothing():
    assert build_notices(_records()) == []


def test_each_fault_names_its_remedy():
    notices = build_notices(_records(
        backup_same_device=True,
        pipeline={"ran": True, "ok": False, "failed_stages": ["backup", "verify"],
                  "nightly_at": None, "dest": "/Volumes/back up/arc"},
    ))
    by_key = {n["key"]: n for n in notices}

    assert by_key["nightly-failed"]["tone"] == "bad"
    assert "backup, verify" in by_key["nightly-failed"]["title"]
    # A destination with a space is pasted, not retyped — the command quotes it.
    assert by_key["nightly-failed"]["command"] == "thread-archive nightly '/Volumes/back up/arc'"
    assert by_key["same-disk"]["tone"] == "warn"
    assert by_key["same-disk"]["command"].startswith("thread-archive daemon install")


def test_a_provider_failing_inside_a_fresh_pass_is_not_hidden_by_it():
    """The pass completed, so nothing else on a health page would go red — the
    provider that failed inside it still has to surface, with its repair verb."""
    notices = build_notices(_records(last_watch_pass={
        "at": _records()["last_watch_pass"]["at"], "sources": {
            "claude-code": {"checked": 5, "events": 20, "parse_errors": 0, "errors": 0},
            "codex": {"checked": 2, "events": 4, "parse_errors": 3, "errors": 0},
        },
    }))

    assert _keys(notices) == ["source-codex"]
    assert notices[0]["command"] == "thread-archive fix-import codex"
    assert "3 parse errors" in notices[0]["detail"]


def test_capture_that_stopped_checking_in_is_a_protection_gap():
    notices = build_notices(_records(last_watch_pass={
        "at": "2020-01-01T00:00:00+00:00", "sources": {},
    }))

    assert _keys(notices) == ["capture-stale"]
    assert notices[0]["tone"] == "bad"
    assert "ago" in notices[0]["title"]


def test_a_library_a_live_feature_needs_is_a_warning_and_a_choice_is_not():
    """The one fault nothing else can show: search keeps answering while ranking
    quality sits below the gated baseline. An extra this install has no use for
    ('off') is a shape of the product, not a fault."""
    library = {"name": "leidenalg + python-igraph", "tier": "extra",
               "capability": "Community detection", "detail": "Running on Louvain."}

    assert build_notices(_records(libraries=[{**library, "state": "off"}])) == []

    notices = build_notices(_records(libraries=[{**library, "state": "degraded"}]))
    assert _keys(notices) == ["library-leidenalg-python-igraph"]
    assert notices[0]["tone"] == "warn"


def test_an_available_release_asks_without_alarming():
    """Maintenance, not a fault: applying an update is explicit here, so the
    notice must not read like something is broken."""
    notices = build_notices(_records(last_self_update={
        "at": None, "ok": True, "action": "update", "tag": "v0.9.2",
        "reason": "past soak window",
    }))

    assert [(n["key"], n["tone"]) for n in notices] == [("update", "good")]


def test_a_partial_status_raises_only_what_it_can_see():
    """A half-configured install (no records at all) must read as unproven, not
    crash the page that is supposed to tell someone it is unproven."""
    keys = _keys(build_notices({}))

    assert keys == ["capture-missing", "nightly-missing"]


def test_coverage_warnings_are_keyed_by_subject_not_position():
    """Two stale exports each get their own address, so silencing one leaves the
    other showing — and reordering the coverage run's list moves neither."""
    warnings = [
        "chatgpt account export is stale: newest export-imported event is 138d old",
        "grok account export is stale: newest export-imported event is 47d old",
    ]
    forward = _keys(build_notices(_records(
        last_coverage={"at": None, "ok": True, "warnings": warnings})))
    reversed_ = _keys(build_notices(_records(
        last_coverage={"at": None, "ok": True, "warnings": list(reversed(warnings))})))

    assert forward == [
        "coverage-warning-chatgpt-account-export-is-stale",
        "coverage-warning-grok-account-export-is-stale",
    ]
    assert sorted(reversed_) == sorted(forward)


def test_two_notices_never_share_an_address():
    """A key is a silence's address; a duplicate would let one silence hide two
    conditions."""
    same = ["thing: one of them", "thing: the other one"]
    keys = _keys(build_notices(_records(
        last_coverage={"at": None, "ok": True, "warnings": same})))

    assert len(set(keys)) == len(keys)


# ---- silencing -------------------------------------------------------------
def test_silencing_moves_a_notice_aside_without_dropping_it(archive_home):
    records = _records(backup_same_device=True)

    board = silence("same-disk", records)

    assert _keys(board["active"]) == []
    assert _keys(board["silenced"]) == ["same-disk"]
    # The count is only honest if the notice is still readable in full.
    assert board["silenced"][0]["detail"].startswith("This protects against")
    assert board["silenced"][0]["silenced_at"]


def test_a_silence_survives_a_rebuild_of_the_page(archive_home):
    records = _records(backup_same_device=True)
    silence("same-disk", records)

    assert _keys(notice_board(records)["active"]) == []
    assert _keys(notice_board(records)["silenced"]) == ["same-disk"]


def test_unsilencing_brings_it_back(archive_home):
    records = _records(backup_same_device=True)
    silence("same-disk", records)

    board = unsilence("same-disk", records)

    assert _keys(board["active"]) == ["same-disk"]
    assert board["silenced"] == []
    assert read_silences() == {}


def test_unsilencing_something_that_is_not_silenced_is_not_an_error(archive_home):
    """Two tabs, one notice: the second click asks for the state it is already in."""
    records = _records(backup_same_device=True)

    board = unsilence("same-disk", records)

    assert _keys(board["active"]) == ["same-disk"]


def test_only_a_firing_notice_can_be_silenced(archive_home):
    """A silence with no condition behind it would sit in the store waiting to
    hide the first occurrence of something nobody has read."""
    with pytest.raises(KeyError):
        silence("same-disk", _records())

    assert read_silences() == {}


def test_a_silence_retires_when_its_condition_clears(archive_home):
    """The backup moves to another disk, so the warning stops firing — and the
    silence goes with it. A later regression is a fault nobody has seen yet."""
    silence("same-disk", _records(backup_same_device=True))

    notice_board(_records(backup_same_device=False))  # the page, once it's fixed

    assert read_silences() == {}
    assert _keys(notice_board(_records(backup_same_device=True))["active"]) == ["same-disk"]


def test_a_silenced_warning_that_only_ages_stays_silent(archive_home):
    """A stale-export warning rewrites its own day count every night. That is the
    same condition, and re-asking about it daily is how a page trains someone to
    stop reading it."""
    def coverage(days: int) -> dict:
        return _records(last_coverage={
            "at": None, "ok": True,
            "warnings": [f"grok account export is stale: newest export-imported "
                         f"event is {days}d old — conversations since then exist "
                         f"only on xAI/Grok's servers"],
        })

    silence("coverage-warning-grok-account-export-is-stale", coverage(47))

    assert notice_board(coverage(61))["active"] == []
    assert len(notice_board(coverage(61))["silenced"]) == 1


def test_a_condition_that_changes_shape_speaks_up_again(archive_home):
    """Silencing "the backup stage failed" is not silencing "the backup and the
    restore drill failed" — the second is a fault the operator has not read."""
    def pipeline(*stages: str) -> dict:
        return _records(pipeline={"ran": True, "ok": False, "failed_stages": list(stages),
                                  "nightly_at": None, "dest": "/Volumes/backup"})

    silence("nightly-failed", pipeline("backup"))
    assert notice_board(pipeline("backup"))["active"] == []

    board = notice_board(pipeline("backup", "restore-drill"))

    assert _keys(board["active"]) == ["nightly-failed"]
    assert board["silenced"] == []
    assert read_silences() == {}


def test_the_store_is_a_readable_file_beside_the_health_records(archive_home):
    silence("same-disk", _records(backup_same_device=True))

    written = json.loads((archive_home / "silenced-notices.json").read_text())

    assert set(written) == {"same-disk"}
    assert written["same-disk"]["title"].startswith("Backup is on the same filesystem")


def test_an_unreadable_store_shows_the_warnings_rather_than_hiding_them(archive_home):
    """Failing open is the only safe direction: a corrupt silence file must cost
    the silences, never the notices."""
    (archive_home / "silenced-notices.json").write_text("{not json", encoding="utf-8")

    board = notice_board(_records(backup_same_device=True))

    assert _keys(board["active"]) == ["same-disk"]


# ---- the api layer over a real archive -------------------------------------
def test_the_api_builds_the_board_from_the_live_records(archive_home):
    """End to end over a real home: a coverage run's warning becomes a notice the
    api serves, and silencing it through the api holds it aside."""
    ta.open_archive(str(archive_home))
    record_health("coverage_last", {
        "ok": True, "warnings": ["chatgpt account export is stale: 138d old"],
        "sources_checked": 1,
    })
    key = "coverage-warning-chatgpt-account-export-is-stale"

    assert key in _keys(ta.notices()["active"])

    board = ta.silence_notice(key)
    assert key not in _keys(board["active"])
    assert key in _keys(ta.notices()["silenced"])

    assert key in _keys(ta.unsilence_notice(key)["active"])
