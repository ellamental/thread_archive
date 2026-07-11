"""The nightly protection pipeline (`archive nightly`) and its instrumentation:
every stage runs (no short-circuit), each outcome lands in health.json, the
family-monitor heartbeat is stamped whatever the outcome, escalation is
age-gated not calendar-gated, failure notifies with stage names, the backup
flags a same-filesystem destination, and the restore drill smoke-checks that
the rebuilt archive actually reads and searches.
"""

from __future__ import annotations

import json

from thread_archive import _api as api
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
    # First-ever run: no verify_deep_last / verify_hashes_last yet → both due.
    assert res["escalations"] == {"deep": True, "hashes": True}
    assert res["drill"]["ok"] is True
    assert res["drill"]["smoke"]["ok"] is True

    health = _health(archive_home)
    for key in ("backup_last", "verify_last", "verify_deep_last",
                "verify_hashes_last", "restore_drill_last", "nightly_last"):
        assert health[key]["ok"] is True, key
    assert health["nightly_last"]["failed_stages"] == []

    beat = json.loads((hb_dir / "archive-nightly.heartbeat").read_text())
    assert beat["ok"] is True and beat["failed_stages"] == []


def test_nightly_escalation_is_age_gated(archive_home, tmp_path):
    _seed(archive_home)
    dest = str(tmp_path / "mirror")
    assert ta.nightly(dest)["escalations"] == {"deep": True, "hashes": True}
    # Fresh, green deep/hashes records → the next nightly stays shallow.
    assert ta.nightly(dest)["escalations"] == {"deep": False, "hashes": False}


def test_nightly_failed_deep_pass_reruns_next_night(archive_home, tmp_path):
    _seed(archive_home)
    dest = str(tmp_path / "mirror")
    ta.nightly(dest)
    api._record_health("verify_deep_last", {"ok": False})
    assert ta.nightly(dest)["escalations"]["deep"] is True


def test_nightly_stage_failure_runs_remaining_stages_and_notifies(
    archive_home, tmp_path, monkeypatch,
):
    _seed(archive_home)
    hb_dir = tmp_path / "_family_logs"
    hb_dir.mkdir()
    monkeypatch.setenv("THREAD_ARCHIVE_HEARTBEAT_DIR", str(hb_dir))

    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(api, "backup", boom)
    sent: list[str] = []
    monkeypatch.setattr(api, "_notify", lambda url, msg: sent.append((url, msg)) and None)

    res = ta.nightly(str(tmp_path / "mirror"), notify_url="http://x/api/notify")

    # backup exploded but the later stages still ran: verify executed (and
    # failed on its mirror scan — first night, so the deep tier folded in and
    # pointed at the never-created mirror), and the drill ran and reported its
    # own failure. Every failed stage is named in the one notification.
    assert res["failed_stages"] == ["backup", "verify", "restore-drill"]
    assert "error" not in res["verify"]  # verify ran to completion
    assert sent and "backup, verify, restore-drill" in sent[0][1]
    beat = json.loads((hb_dir / "archive-nightly.heartbeat").read_text())
    assert beat["ok"] is False and beat["failed_stages"] == res["failed_stages"]


def test_backup_flags_same_filesystem_destination(archive_home, tmp_path):
    _seed(archive_home)
    # tmp destination shares the tmp filesystem with the archive home.
    res = ta.backup(str(tmp_path / "mirror"))
    assert res["same_device"] is True
    assert _health(archive_home)["backup_last"]["same_device"] is True


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
# Ella her memory is unprotected until 04:00. `_pipeline_verdict` retires a
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
    v = api._pipeline_verdict(health)
    assert v["ok"] is True
    assert v["failed_stages"] == [] and v["recovered_stages"] == ["verify"]


def test_verdict_keeps_a_deep_failure_a_basic_verify_cannot_speak_to():
    # The false-green this whole mechanism has to refuse: the nightly's deep +
    # hashes + mirror-scan verify failed; a bare `archive verify` passed after
    # it. The cheap run tested none of the tiers that broke.
    health = {
        "nightly_last": _nightly_rec(["verify"], deep=True, hashes=True),
        "verify_last": {"at": LATER, "ok": True,
                        "deep": False, "hashes": False, "backup": False},
    }
    v = api._pipeline_verdict(health)
    assert v["ok"] is False and v["failed_stages"] == ["verify"]


def test_verdict_keeps_a_deep_failure_when_the_mirror_went_unscanned():
    # --deep --hashes but no --backup: the nightly parse-scans the mirror
    # whenever deep is due, so a rerun that skipped it is NOT equal-strength.
    health = {
        "nightly_last": _nightly_rec(["verify"], deep=True, hashes=True),
        "verify_last": {"at": LATER, "ok": True,
                        "deep": True, "hashes": True, "backup": False},
    }
    assert api._pipeline_verdict(health)["failed_stages"] == ["verify"]


def test_verdict_ignores_a_green_run_that_predates_the_failure():
    health = {
        "nightly_last": _nightly_rec(["verify"], deep=True, hashes=True),
        "verify_last": {"at": EARLIER, "ok": True,
                        "deep": True, "hashes": True, "backup": True},
    }
    assert api._pipeline_verdict(health)["failed_stages"] == ["verify"]


def test_verdict_retires_drill_and_backup_on_a_later_green_run():
    health = {
        "nightly_last": _nightly_rec(["backup", "restore-drill"]),
        "backup_last": {"at": LATER, "ok": True},
        "restore_drill_last": {"at": LATER, "ok": True},
    }
    v = api._pipeline_verdict(health)
    assert v["ok"] is True
    assert sorted(v["recovered_stages"]) == ["backup", "restore-drill"]


def test_verdict_retires_only_the_stages_actually_reproven():
    health = {
        "nightly_last": _nightly_rec(["verify", "restore-drill"]),
        "verify_last": {"at": LATER, "ok": True,
                        "deep": False, "hashes": False, "backup": False},
    }
    v = api._pipeline_verdict(health)
    assert v["ok"] is False
    assert v["failed_stages"] == ["restore-drill"]
    assert v["recovered_stages"] == ["verify"]


def test_verdict_never_retires_on_a_red_rerun():
    health = {
        "nightly_last": _nightly_rec(["verify"]),
        "verify_last": {"at": LATER, "ok": False, "deep": True,
                        "hashes": True, "backup": True},
    }
    assert api._pipeline_verdict(health)["failed_stages"] == ["verify"]


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
    api._record_health("nightly_last", {
        "dest": dest, "ok": False, "failed_stages": ["verify"],
        "deep": True, "hashes": True, "drill": True,
    })
    # _record_health stamps `at` itself; pin the nightly into the past so the
    # verify below unambiguously postdates it.
    health = json.loads((archive_home / "health.json").read_text())
    health["nightly_last"]["at"] = nightly_at
    (archive_home / "health.json").write_text(json.dumps(health))
    api._stamp_heartbeat()
    assert json.loads(beat_path.read_text())["ok"] is False

    res = ta.verify(deep=True, hashes=True, backup=dest)
    assert res["ok"] is True

    beat = json.loads(beat_path.read_text())
    assert beat["ok"] is True
    assert beat["failed_stages"] == [] and beat["recovered_stages"] == ["verify"]
    # ...and the freshness anchor still points at the NIGHTLY, not at the verify
    # that just rewrote the file — else a rerun would mask a dead 04:00 job.
    assert beat["nightly_at"] == nightly_at
