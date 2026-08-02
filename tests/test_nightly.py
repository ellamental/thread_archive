"""The nightly protection pipeline (`thread-archive backup nightly`) and its instrumentation:
every stage runs (no short-circuit), each outcome lands in health.json, the
family-monitor heartbeat is stamped whatever the outcome, escalation is
age-gated not calendar-gated, failure notifies with stage names, the backup
flags a same-filesystem destination, and the restore drill smoke-checks that
the rebuilt archive actually reads and searches.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import thread_archive._ops.health as ops_health
from thread_archive import _api as ta

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello durability"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back"}]}}


def _seed(archive_home):
    f = archive_home / "sess.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in (USER, ASSISTANT)) + "\n",
                 encoding="utf-8")
    ta.import_path(f)
    ta.checkpoint()


def _health(archive_home) -> dict:
    return json.loads((archive_home / "health.json").read_text(encoding="utf-8"))


def test_nightly_green_run_records_everything(archive_home, tmp_path, monkeypatch):
    _seed(archive_home)
    hb_dir = tmp_path / "_family_logs"
    hb_dir.mkdir()
    monkeypatch.setenv("THREAD_ARCHIVE_HEARTBEAT_DIR", str(hb_dir))

    res = ta.nightly(str(tmp_path / "mirror"))

    assert res["ok"] is True and res["failed_stages"] == []
    # First-ever run: no verify_deep_last / verify_hashes_last / restore_drill_last
    # yet → all three due.
    assert res["escalations"] == {"deep": True, "hashes": True, "drill": True}
    assert res["drill"]["ok"] is True
    assert res["drill"]["smoke"]["ok"] is True

    health = _health(archive_home)
    for key in ("backup_last", "verify_last", "verify_deep_last",
                "verify_hashes_last", "restore_drill_last", "nightly_last"):
        assert health[key]["ok"] is True, key
    assert health["nightly_last"]["failed_stages"] == []

    beat = json.loads((hb_dir / "archive-nightly.heartbeat").read_text())
    assert beat["ok"] is True and beat["failed_stages"] == []


def test_every_scheduled_stage_records_what_it_cost(archive_home, tmp_path):
    """These run unattended and their cost grows with the corpus, so an ``ok``
    flag alone leaves "the nightly is taking longer" as an impression with no
    record that could settle it."""
    _seed(archive_home)
    res = ta.nightly(str(tmp_path / "mirror"))

    health = _health(archive_home)
    for key in ("backup_last", "verify_last", "restore_drill_last",
                "coverage_last", "nightly_last"):
        assert health[key]["duration_s"] >= 0.0, key

    # The night's total, and where it went. A total that grew says only that the
    # night got longer; the split says which stage did it.
    assert res["duration_s"] >= 0.0
    assert set(res["stage_s"]) == {
        "source-mirror", "backup", "verify", "restore-drill", "coverage",
    }
    assert health["nightly_last"]["stage_s"] == res["stage_s"]
    # The stage names are the ones failed_stages uses, so the two read together.
    assert set(res["stage_s"]) >= set(res["failed_stages"])


def test_nightly_coverage_stage_is_wired(archive_home, tmp_path, monkeypatch):
    # A failing stub over the coverage stage: on a sandboxed machine the real
    # check has no source to fail on, so a red one has to be injected to assert
    # the stage runs, fails the night, and records.
    import thread_archive._ops.nightly as ops_nightly

    _seed(archive_home)
    monkeypatch.setattr(
        ops_nightly, "check_coverage",
        lambda **kw: {"ok": False, "failed": ["stub went dark"]},
    )
    res = ta.nightly(str(tmp_path / "mirror"))
    assert "coverage" in res["failed_stages"]
    assert res["coverage"]["failed"] == ["stub went dark"]
    assert _health(archive_home)["nightly_last"]["failed_stages"] == ["coverage"]
    # A stage that fails is still costed. Its own health record may never be
    # written — the raising case writes nothing at all — so the night's row is the
    # only place a stage that ate the window can show up.
    assert "coverage" in res["stage_s"]


def test_nightly_source_mirror_stage_is_wired(archive_home, tmp_path, monkeypatch):
    # A failing stub over the source-mirror stage, for the same reason as the
    # coverage one above: assert the stage runs, fails the night, and records.
    import thread_archive._ops.nightly as ops_nightly

    _seed(archive_home)
    monkeypatch.setattr(
        ops_nightly, "mirror_sources",
        lambda **kw: {"ok": False, "providers": {}, "unsupported": []},
    )
    res = ta.nightly(str(tmp_path / "mirror"))
    assert "source-mirror" in res["failed_stages"]
    assert res["source_mirror"]["ok"] is False
    # The mirror failing must not short-circuit the stages after it.
    assert res["backup"]["verify_ok"] is True


def test_nightly_drift_alert_notifies(archive_home, tmp_path):
    # A validation-drift record written in the last 24h rides the nightly as a
    # push (independent of stage failures), and its text names the ledger.
    from thread_archive._importers._validation_ledger import record_drift
    from thread_archive._ops.nightly import _drift_alert

    _seed(archive_home)
    assert _drift_alert() is None
    record_drift("claude-code", "sess", findings=["field drifted"], batch_safe=True)
    alert = _drift_alert()
    assert alert is not None and "format drift active" in alert
    assert "1 validation-drift record(s)" in alert


def test_nightly_escalation_is_age_gated(archive_home, tmp_path):
    _seed(archive_home)
    dest = str(tmp_path / "mirror")
    assert ta.nightly(dest)["escalations"] == {"deep": True, "hashes": True, "drill": True}
    # Fresh, green deep/hashes/drill records → the next nightly stays shallow and
    # skips the drill, which is the stage the night's wall clock is made of.
    assert ta.nightly(dest)["escalations"] == {"deep": False, "hashes": False, "drill": False}


def test_nightly_drill_is_weekly_and_absent_on_the_nights_between(archive_home, tmp_path):
    """The drill runs on its gate, not every night. A skipped drill leaves no
    ``drill`` result and no ``restore-drill`` timing — absent, not failed — and
    the health record from the night it *did* run is left standing, which is what
    the stage's tolerance window then reads."""
    _seed(archive_home)
    dest = str(tmp_path / "mirror")
    first = ta.nightly(dest)
    assert first["escalations"]["drill"] is True
    assert first["drill"]["ok"] is True
    drilled_at = _health(archive_home)["restore_drill_last"]["at"]

    second = ta.nightly(dest)
    assert second["ok"] is True and second["failed_stages"] == []
    assert second["escalations"]["drill"] is False
    assert "drill" not in second
    assert "restore-drill" not in second["stage_s"]
    # The earlier drill's verdict survives the nights that skip it.
    assert _health(archive_home)["restore_drill_last"]["at"] == drilled_at
    assert _health(archive_home)["nightly_last"]["drill"] is False


def test_nightly_failed_drill_reruns_the_next_night(archive_home, tmp_path):
    """Weekly while healthy, nightly while broken. A restore-path regression is
    the reason the drill was nightly at all, so a not-ok record has to read as
    due — otherwise a break found on Monday goes unretested until the following
    Monday."""
    _seed(archive_home)
    dest = str(tmp_path / "mirror")
    ta.nightly(dest)
    assert ta.nightly(dest)["escalations"]["drill"] is False  # green → not due
    ops_health.record_health("restore_drill_last", {"ok": False})
    assert ta.nightly(dest)["escalations"]["drill"] is True


def test_nightly_hashes_pass_scans_the_mirror_without_a_deep_pass(archive_home, tmp_path):
    """The mirror's content-hash scan needs both ``hashes`` and a non-None
    ``backup``. On a night where only the hashes gate is due, the mirror must
    still be pulled in — otherwise the scan runs only when the two age gates
    coincide, and the fallback copy goes unchecked for rot at rest."""
    _seed(archive_home)
    dest = str(tmp_path / "mirror")
    ta.nightly(dest)  # first run escalates both, leaving fresh green records
    ops_health.record_health("verify_hashes_last", {"ok": False})  # hashes only

    res = ta.nightly(dest)
    assert res["escalations"] == {"deep": False, "hashes": True, "drill": False}
    assert "hashes" in res["verify"]["backup"]


def test_nightly_failed_deep_pass_reruns_next_night(archive_home, tmp_path):
    _seed(archive_home)
    dest = str(tmp_path / "mirror")
    ta.nightly(dest)
    ops_health.record_health("verify_deep_last", {"ok": False})
    assert ta.nightly(dest)["escalations"]["deep"] is True


def test_nightly_stage_failure_runs_remaining_stages_and_notifies(
    archive_home, tmp_path, monkeypatch,
):
    _seed(archive_home)
    hb_dir = tmp_path / "_family_logs"
    hb_dir.mkdir()
    monkeypatch.setenv("THREAD_ARCHIVE_HEARTBEAT_DIR", str(hb_dir))

    # A destination the backup genuinely cannot create: its parent is a file,
    # so the first mkdir raises — the shape of a dest on a gone/renamed volume.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    dest = blocker / "mirror"

    # A real loopback /api/notify records what the push actually sends.
    class _NotifyHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.server.posts.append(json.loads(self.rfile.read(n)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    notify_srv = ThreadingHTTPServer(("127.0.0.1", 0), _NotifyHandler)
    notify_srv.posts = []
    threading.Thread(target=lambda: notify_srv.serve_forever(poll_interval=0.02),
                     daemon=True).start()
    try:
        res = ta.nightly(str(dest),
                         notify_url=f"http://127.0.0.1:{notify_srv.server_port}/api/notify")
    finally:
        notify_srv.shutdown()
        notify_srv.server_close()
    sent = notify_srv.posts

    # backup exploded but the later stages still ran: verify executed (and
    # failed on its mirror scan — first night, so the deep tier folded in and
    # pointed at the never-created mirror), and the drill ran and reported its
    # own failure. Every failed stage is named in the one notification.
    assert res["failed_stages"] == ["backup", "verify", "restore-drill"]
    assert "NotADirectoryError" in res["backup"]["error"]
    assert "error" not in res["verify"]  # verify ran to completion
    assert sent and "backup, verify, restore-drill" in sent[0]["message"]
    assert sent[0]["title"] == "thread-archive"
    beat = json.loads((hb_dir / "archive-nightly.heartbeat").read_text())
    assert beat["ok"] is False and beat["failed_stages"] == res["failed_stages"]


def test_restore_drill_smoke_reads_and_searches_the_rebuilt_archive(
    archive_home, tmp_path,
):
    _seed(archive_home)
    ta.backup(str(tmp_path / "mirror"))
    res = ta.restore_drill(str(tmp_path / "mirror"))
    assert res["ok"] is True
    sm = res["smoke"]
    assert sm["read_ok"] is True and sm["search_ok"] is True
    # The drill reopened the live archive on its way out.
    assert ta.search("durability")


def test_status_surfaces_the_drill_and_nightly_records(archive_home, tmp_path):
    _seed(archive_home)
    ta.nightly(str(tmp_path / "mirror"))
    st = ta.status()
    assert st["last_restore_drill"]["ok"] is True
    assert st["last_nightly"]["ok"] is True
    assert st["pipeline"]["ok"] is True
    assert st["pipeline"]["ran"] is True
    assert st["backup_same_device"] is True


def test_nightly_skips_heartbeat_without_family_logs_dir(
    archive_home, tmp_path, monkeypatch,
):
    _seed(archive_home)
    hb_dir = tmp_path / "_family_logs"  # never created
    monkeypatch.setenv("THREAD_ARCHIVE_HEARTBEAT_DIR", str(hb_dir))
    assert ta.nightly(str(tmp_path / "mirror"))["ok"] is True
    assert not hb_dir.exists()


# ── Stage retirement: a failed stage re-proven out of band ───────────────────
# The pipeline is ~1h (drill-dominated), so if only another full nightly could
# retire a fault, an archive that was fixed AND proven fixed would keep telling
# the operator their memory is unprotected until 04:00. `_pipeline_verdict` retires a
# failed stage on a later, at-least-as-strong green run — with the strength
# comparison as the guard that keeps a cheap check from laundering an expensive
# red.

def _nightly_rec(failed, *, at="2026-01-02T04:00:00+00:00", deep=False, hashes=False):
    return {"at": at, "ok": not failed, "failed_stages": list(failed),
            "deep": deep, "hashes": hashes, "drill": True}


LATER = "2026-01-02T09:00:00+00:00"
EARLIER = "2026-01-02T03:00:00+00:00"


def test_verdict_retires_a_verify_reproven_at_equal_tier():
    health = {
        "nightly_last": _nightly_rec(["verify"], deep=True, hashes=True),
        "verify_last": {"at": LATER, "ok": True,
                        "deep": True, "hashes": True, "backup": True},
    }
    v = ops_health.pipeline_verdict(health)
    assert v["ok"] is True
    assert v["failed_stages"] == [] and v["recovered_stages"] == ["verify"]


def test_verdict_keeps_a_deep_failure_a_basic_verify_cannot_speak_to():
    # The false-green this whole mechanism has to refuse: the nightly's deep +
    # hashes + mirror-scan verify failed; a bare `thread-archive index verify` passed after
    # it. The cheap run tested none of the tiers that broke.
    health = {
        "nightly_last": _nightly_rec(["verify"], deep=True, hashes=True),
        "verify_last": {"at": LATER, "ok": True,
                        "deep": False, "hashes": False, "backup": False},
    }
    v = ops_health.pipeline_verdict(health)
    assert v["ok"] is False and v["failed_stages"] == ["verify"]


def test_verdict_keeps_a_deep_failure_when_the_mirror_went_unscanned():
    # --deep --hashes but no --backup: the nightly parse-scans the mirror
    # whenever deep is due, so a rerun that skipped it is NOT equal-strength.
    health = {
        "nightly_last": _nightly_rec(["verify"], deep=True, hashes=True),
        "verify_last": {"at": LATER, "ok": True,
                        "deep": True, "hashes": True, "backup": False},
    }
    assert ops_health.pipeline_verdict(health)["failed_stages"] == ["verify"]


def test_verdict_ignores_a_green_run_that_predates_the_failure():
    health = {
        "nightly_last": _nightly_rec(["verify"], deep=True, hashes=True),
        "verify_last": {"at": EARLIER, "ok": True,
                        "deep": True, "hashes": True, "backup": True},
    }
    assert ops_health.pipeline_verdict(health)["failed_stages"] == ["verify"]


def test_verdict_retires_drill_and_backup_on_a_later_green_run():
    health = {
        "nightly_last": _nightly_rec(["backup", "restore-drill"]),
        "backup_last": {"at": LATER, "ok": True},
        "restore_drill_last": {"at": LATER, "ok": True},
    }
    v = ops_health.pipeline_verdict(health)
    assert v["ok"] is True
    assert sorted(v["recovered_stages"]) == ["backup", "restore-drill"]


def test_verdict_retires_only_the_stages_actually_reproven():
    health = {
        "nightly_last": _nightly_rec(["verify", "restore-drill"]),
        "verify_last": {"at": LATER, "ok": True,
                        "deep": False, "hashes": False, "backup": False},
    }
    v = ops_health.pipeline_verdict(health)
    assert v["ok"] is False
    assert v["failed_stages"] == ["restore-drill"]
    assert v["recovered_stages"] == ["verify"]


def test_verdict_never_retires_on_a_red_rerun():
    health = {
        "nightly_last": _nightly_rec(["verify"]),
        "verify_last": {"at": LATER, "ok": False, "deep": True,
                        "hashes": True, "backup": True},
    }
    assert ops_health.pipeline_verdict(health)["failed_stages"] == ["verify"]


# The restore-drill grace: an expensive drill over a flaky backup mirror is
# forgiven while a recent GREEN drill still stands (see _STAGE_GRACE_DAYS). Unlike
# recovery, that good drill may PREDATE the failed nightly — a prior success is what
# makes a single failed drill a transient blip, not an unprotected archive. These use
# now-relative stamps because the grace is measured against the wall clock.

def _ago(days: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_verdict_tolerates_a_recent_drill_failure_that_predates_the_nightly():
    health = {
        "nightly_last": _nightly_rec(["restore-drill"], at=_ago(1)),
        "restore_drill_last": {"at": _ago(3), "ok": True},  # green, predates, days old
    }
    v = ops_health.pipeline_verdict(health)
    assert v["ok"] is True
    assert v["failed_stages"] == []
    assert v["tolerated_stages"] == ["restore-drill"]
    assert v["recovered_stages"] == []  # tolerated, NOT re-proven


def test_verdict_stops_tolerating_once_the_last_good_drill_is_stale():
    health = {
        "nightly_last": _nightly_rec(["restore-drill"], at=_ago(1)),
        "restore_drill_last": {"at": _ago(20), "ok": True},  # older than the 14d grace
    }
    v = ops_health.pipeline_verdict(health)
    assert v["ok"] is False
    assert v["failed_stages"] == ["restore-drill"]
    assert v["tolerated_stages"] == []


def test_verdict_does_not_tolerate_a_drill_failure_on_a_recent_red_drill():
    health = {
        "nightly_last": _nightly_rec(["restore-drill"], at=_ago(1)),
        "restore_drill_last": {"at": _ago(2), "ok": False},  # recent, but not green
    }
    v = ops_health.pipeline_verdict(health)
    assert v["ok"] is False
    assert v["failed_stages"] == ["restore-drill"]


def test_verdict_gives_verify_no_grace():
    # Grace is the restore-drill's alone; a cheap local stage must show its failure
    # even with a recent green record — only a postdating equal-tier rerun retires it.
    health = {
        "nightly_last": _nightly_rec(["verify"], at=_ago(1)),
        "verify_last": {"at": _ago(2), "ok": True,
                        "deep": False, "hashes": False, "backup": False},
    }
    v = ops_health.pipeline_verdict(health)
    assert v["ok"] is False
    assert v["failed_stages"] == ["verify"]
    assert v["tolerated_stages"] == []


def test_a_passing_verify_clears_the_heartbeat_a_failed_nightly_left(
    archive_home, tmp_path, monkeypatch,
):
    # End to end, the case that motivates all of the above: the nightly failed
    # at verify, an operator re-ran verify at full strength, it passed — the
    # monitor's heartbeat must say so without waiting out another pipeline.
    _seed(archive_home)
    hb_dir = tmp_path / "_family_logs"
    hb_dir.mkdir()
    monkeypatch.setenv("THREAD_ARCHIVE_HEARTBEAT_DIR", str(hb_dir))
    dest = str(tmp_path / "mirror")
    ta.backup(dest)  # a real mirror for the deep tier's scan to find

    beat_path = hb_dir / "archive-nightly.heartbeat"
    nightly_at = "2026-01-02T04:00:00+00:00"
    ops_health.record_health("nightly_last", {
        "dest": dest, "ok": False, "failed_stages": ["verify"],
        "deep": True, "hashes": True, "drill": True,
    })
    # _record_health stamps `at` itself; pin the nightly into the past so the
    # verify below unambiguously postdates it.
    health = json.loads((archive_home / "health.json").read_text())
    health["nightly_last"]["at"] = nightly_at
    (archive_home / "health.json").write_text(json.dumps(health))
    ops_health.stamp_heartbeat()
    assert json.loads(beat_path.read_text())["ok"] is False

    res = ta.verify(deep=True, hashes=True, backup=dest)
    assert res["ok"] is True

    beat = json.loads(beat_path.read_text())
    assert beat["ok"] is True
    assert beat["failed_stages"] == [] and beat["recovered_stages"] == ["verify"]
    # ...and the freshness anchor still points at the NIGHTLY, not at the verify
    # that just rewrote the file — else a rerun would mask a dead 04:00 job.
    assert beat["nightly_at"] == nightly_at


def test_stage_error_names_tcc_on_darwin_eperm(monkeypatch):
    """EPERM from a file op is reported with the macOS-TCC hint; EACCES (a
    plain unix permission denial) and non-permission errors stay unadorned —
    the hint must not fire where the diagnosis doesn't apply.

    Both arms are pinned on every host, so the platform is supplied: the hint
    is a property of the machine the stage ran on, which no caller passes and a
    test cannot be. The hint reaching a real recorded stage error is
    ``test_nightly_backup_stage_reports_tcc_hint``, over a real EPERM."""
    import errno as errno_mod

    from thread_archive._ops.nightly import _stage_error

    monkeypatch.setattr("thread_archive._ops.nightly.sys.platform", "darwin")
    eperm = PermissionError(errno_mod.EPERM, "Operation not permitted", "/Volumes/NAS/x")
    assert "TCC" in _stage_error(eperm)
    eacces = PermissionError(errno_mod.EACCES, "Permission denied", "/tmp/x")
    assert "TCC" not in _stage_error(eacces)
    assert "TCC" not in _stage_error(RuntimeError("boom"))
    monkeypatch.setattr("thread_archive._ops.nightly.sys.platform", "linux")
    assert "TCC" not in _stage_error(eperm)


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="the TCC hint, and the immutable-flag EPERM that provokes it, are macOS")
def test_nightly_backup_stage_reports_tcc_hint(archive_home, tmp_path):
    """A backup stage dying on EPERM (the launchd-without-grant shape) carries
    the TCC hint into the stage's recorded error, where notify/health readers
    see it.

    The denial is real: a directory carrying the user-immutable flag answers
    every write inside it with EPERM — the same errno, from the same syscalls,
    that a TCC-ungranted background job gets on a protected volume."""
    import errno as errno_mod
    import stat

    from thread_archive._ops.nightly import nightly

    _seed(archive_home)
    dest = tmp_path / "dest"
    dest.mkdir()
    os.chflags(dest, stat.UF_IMMUTABLE)
    try:
        with pytest.raises(PermissionError) as raised:  # the premise, spelled out
            ta.backup(str(dest))
        assert raised.value.errno == errno_mod.EPERM

        result = nightly(str(dest), drill=False)
    finally:
        os.chflags(dest, 0)  # else even the tmp_path cleanup cannot remove it
    assert "backup" in result["failed_stages"]
    assert "TCC" in result["backup"]["error"]
