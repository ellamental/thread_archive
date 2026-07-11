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
