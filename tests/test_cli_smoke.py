"""The CLI surface is real and `archive --help` works."""

from __future__ import annotations

import pytest

from thread_archive.cli import build_parser, main


def test_help_runs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "archive" in out


def test_all_subcommands_present() -> None:
    parser = build_parser()
    # Reach into the subparsers action to assert the full command surface is wired.
    # (test_public_api.py owns the boundary ratchet; this is the wiring smoke.)
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    assert set(sub.choices) == {
        "import", "import-export", "watch", "reindex", "embed",
        "status", "backup", "verify", "repair", "restore-drill", "restore",
        "nightly", "coverage", "redact", "unredact", "daemon",
    }


def test_embed_cli_dispatches(monkeypatch, capsys) -> None:
    """`archive embed` wires to api.embed (the incremental vector catch-up). The
    embed backend is stubbed so the smoke test never loads torch."""
    from thread_archive import _api as api

    seen = {}
    monkeypatch.setattr(api, "embed", lambda **kw: seen.update(kw) or {"embedded": 4})
    rc = main(["embed", "--rebuild", "--limit", "100", "--home", "/unused-by-stub"])
    assert rc == 0
    assert seen["rebuild"] is True and seen["max_events"] == 100
    assert "embedded 4" in capsys.readouterr().out


# ── verb → api dispatch (stubbed: arg mapping + exit codes, no real work) ─────
# The verbs are wired into LaunchAgent plists, cron, and the monitor's heartbeat
# contract, so a silent arg-mapping regression hurts operationally. Each test
# stubs the api function and asserts the CLI passes exactly what it parsed.

_BACKUP_OK = {
    "truth_dir": "t", "dest": "d", "files_copied": 3, "bytes_copied": 1024,
    "verify_ok": True, "deletions_skipped": 0, "shrinks_skipped": 0,
    "mirror_complete": True,
}


def test_backup_cli_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    seen = {}
    monkeypatch.setattr(
        api, "backup", lambda dest, **kw: seen.update(dest=dest, **kw) or dict(_BACKUP_OK)
    )
    rc = main(["backup", "/dest", "--allow-shrink", "--no-verify", "--home", "/h"])
    assert rc == 0
    assert seen == {"dest": "/dest", "home": "/h", "allow_shrink": True, "verify_first": False}
    assert "backed up" in capsys.readouterr().out


def test_backup_cli_fails_on_incomplete_mirror(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    bad = {**_BACKUP_OK, "mirror_complete": False,
           "dest_missing_files": 2, "dest_divergent_files": 0}
    monkeypatch.setattr(api, "backup", lambda dest, **kw: bad)
    rc = main(["backup", "/dest"])
    assert rc == 1
    assert "MIRROR INCOMPLETE" in capsys.readouterr().out


_VERIFY_OK = {
    "ok": True,
    "truth": {"threads": 1, "events": 2, "events_effective": 2,
              "duplicate_id_lines": 0, "duplicate_content_lines": 0, "parse_errors": 0},
    "index": {"threads": 1, "events": 2, "kg_events": 0,
              "quick_check": "ok", "check": "quick_check"},
    "drift": {"threads": 0, "events": 0, "kg_events": 0},
    "fts": {"shadow_rows": 2, "fts5_rows": 2, "orphan_rows": 0},
}


def test_verify_cli_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    tiers = {
        **_VERIFY_OK,
        "deep": {
            "ok": True, "watermark": 2, "events_index_only": 0, "index_only_sample": [],
            "events_missing_from_index": 0, "missing_sample": [],
            "events_key_mismatch": 0, "key_mismatch_sample": [],
            "events_superseded_twins": 0, "thread_meta_mismatch": 0, "thread_meta_sample": [],
            "kg": {"index_only": 0, "truth_only": 0, "content_mismatch": 0, "watermark": 0},
            "dangling": {"link_endpoints": 0, "citation_events": 0,
                         "citation_thread_mismatch": 0, "event_threads": 0},
            "duplicate_content_pairs_index": 0,
            "fts": {"orphan_rows": 0, "shadow_rows": 2, "fts5_rows": 2,
                    "unindexed_events": 0, "unindexed_sample": [], "empty_extract_events": 0},
        },
        "hashes": {
            "truth": {"checked": 2, "mismatched": 0, "unhashed_keys": 0, "no_key": 0,
                      "mismatch_sample": []},
            "index": {"checked": 2, "mismatched": 0, "unhashed_keys": 0, "no_key": 0,
                      "mismatch_sample": []},
            "cross": {"compared": 2, "mismatched": 0, "mismatch_sample": []},
        },
        "backup": {"dest": "/mirror", "ok": True, "coverage": 1.0,
                   "scan": {"threads": 1, "events_effective": 2, "parse_errors": 0}},
    }
    seen = {}
    monkeypatch.setattr(api, "verify", lambda **kw: seen.update(kw) or tiers)
    rc = main(["verify", "--deep", "--hashes", "--backup", "/mirror", "--home", "/h"])
    assert rc == 0
    assert seen == {"home": "/h", "deep": True, "hashes": True, "backup": "/mirror"}
    out = capsys.readouterr().out
    assert "deep:" in out and "hashes[cross]" in out and "backup[/mirror]" in out


def test_verify_cli_exit_codes(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    monkeypatch.setattr(api, "verify", lambda **kw: dict(_VERIFY_OK))
    assert main(["verify"]) == 0
    assert "OK" in capsys.readouterr().out

    failed = {**_VERIFY_OK, "ok": False, "failed_components": ["drift_events"]}
    monkeypatch.setattr(api, "verify", lambda **kw: failed)
    assert main(["verify"]) == 1
    assert "FAILED: drift_events" in capsys.readouterr().out


def test_restore_drill_cli_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    seen = {}
    monkeypatch.setattr(
        api, "restore_drill",
        lambda dest, **kw: seen.update(dest=dest, **kw) or {"ok": True, "seconds": 1.0},
    )
    rc = main(["restore-drill", "/mirror", "--keep-home", "--home", "/h"])
    assert rc == 0
    assert seen == {"dest": "/mirror", "home": "/h", "keep_home": True}
    assert "OK" in capsys.readouterr().out


_NIGHTLY_OK = {
    "backup": {"files_copied": 1, "bytes_copied": 0},
    "escalations": {"deep": False, "hashes": False},
    "verify": {"ok": True, "drift": {"events": 0}, "truth": {"parse_errors": 0}},
    "ok": True,
    "failed_stages": [],
}


def test_nightly_cli_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    seen = {}
    monkeypatch.setattr(
        api, "nightly", lambda dest, **kw: seen.update(dest=dest, **kw) or dict(_NIGHTLY_OK)
    )
    rc = main(["nightly", "/dest", "--notify-url", "http://n", "--allow-shrink",
               "--no-drill", "--home", "/h"])
    assert rc == 0
    assert seen == {"dest": "/dest", "home": "/h", "notify_url": "http://n",
                    "allow_shrink": True, "drill": False}
    assert "NIGHTLY OK" in capsys.readouterr().out


def test_nightly_cli_fails_with_stage_names(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    failed = {**_NIGHTLY_OK, "ok": False, "failed_stages": ["backup", "verify"],
              "verify": {"error": "boom"}, "backup": {"error": "boom"}}
    monkeypatch.setattr(api, "nightly", lambda dest, **kw: failed)
    rc = main(["nightly", "/dest"])
    assert rc == 1
    assert "NIGHTLY FAILED: backup, verify" in capsys.readouterr().out


def test_repair_cli_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    seen = {}
    res = {"dry_run": True, "fragments_quarantined": 0, "files_damaged": 0,
           "events_restored_from_index": 0, "kg_events_restored": 0,
           "thread_records_restored": 0}
    monkeypatch.setattr(api, "repair", lambda **kw: seen.update(kw) or res)
    rc = main(["repair", "--dry-run", "--home", "/h"])
    assert rc == 0
    assert seen == {"home": "/h", "dry_run": True}
    assert "would quarantine" in capsys.readouterr().out


def test_daemon_backup_install_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _launchd

    seen = {}
    monkeypatch.setattr(
        _launchd, "install_backup",
        lambda dest, home=None, **kw: seen.update(dest=dest, home=home, **kw)
        or "/plist/com.thread-archive.backup.plist",
    )
    rc = main(["daemon", "install", "--backup", "--dest", "/Volumes/Backup/arc",
               "--at", "02:30", "--home", "/h"])
    assert rc == 0
    assert seen == {"dest": "/Volumes/Backup/arc", "home": "/h",
                    "hour": 2, "minute": 30, "notify_url": None}
    assert "nightly at 02:30" in capsys.readouterr().out


def test_daemon_backup_install_requires_dest(capsys) -> None:
    rc = main(["daemon", "install", "--backup"])
    assert rc == 2
    assert "needs --dest" in capsys.readouterr().err


def test_daemon_backup_rejects_bad_at() -> None:
    with pytest.raises(SystemExit):
        main(["daemon", "install", "--backup", "--dest", "/d", "--at", "9pm"])


def test_redact_cli_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    seen = {}
    res = {"events_redacted": 2, "thread_id": 7, "key_id": "k1",
           "topic_quotes_scrubbed": 1, "kg_quotes_scrubbed": 0,
           "notes": ["provider store keeps its plaintext"]}
    monkeypatch.setattr(
        api, "redact",
        lambda thread_id, event_ids, **kw: seen.update(
            thread_id=thread_id, event_ids=event_ids, **kw) or res,
    )
    rc = main(["redact", "7", "--events", "3,5", "--reason", "pii", "--home", "/h"])
    assert rc == 0
    assert seen == {"thread_id": 7, "event_ids": [3, 5], "reason": "pii", "home": "/h"}
    out = capsys.readouterr().out
    assert "redacted 2 event(s) in thread 7 under key k1" in out
    assert "scrubbed 1 topic quote(s)" in out
    assert "note: provider store keeps its plaintext" in out
    assert "unredact k1" in out


def test_redact_cli_without_thread_prints_usage() -> None:
    assert main(["redact"]) == 2


def test_redact_list_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    monkeypatch.setattr(api, "redactions", lambda **kw: [])
    assert main(["redact", "--list"]) == 0
    assert "no redactions" in capsys.readouterr().out

    row = {"key_id": "k1", "thread_id": 7, "event_ids": [3, 5], "status": "redacted",
           "key": "held", "redacted_at": "2026-07-15T00:00:00Z", "reason": "pii"}
    monkeypatch.setattr(api, "redactions", lambda **kw: [row])
    assert main(["redact", "--list"]) == 0
    out = capsys.readouterr().out
    assert "k1  thread 7  2 event(s)" in out and "reason: pii" in out


def test_redact_key_lifecycle_dispatches(monkeypatch, capsys) -> None:
    """--show-key / --forget / --restore-key each map onto their api function;
    --forget without --yes refuses, since an unescrowed key is the content."""
    from thread_archive import _api as api

    seen = {}
    monkeypatch.setattr(api, "redact_show_key", lambda kid, **kw: seen.update(show=kid) or "b64==")
    assert main(["redact", "--show-key", "k1"]) == 0
    assert seen["show"] == "k1" and "b64==" in capsys.readouterr().out

    assert main(["redact", "--forget", "k1"]) == 2
    assert "refusing" in capsys.readouterr().out

    monkeypatch.setattr(api, "redact_forget_key", lambda kid, **kw: seen.update(forget=kid))
    assert main(["redact", "--forget", "k1", "--yes"]) == 0
    assert seen["forget"] == "k1"

    monkeypatch.setattr(
        api, "redact_restore_key", lambda kid, key, **kw: seen.update(restore=(kid, key))
    )
    assert main(["redact", "--restore-key", "k1", "b64=="]) == 0
    assert seen["restore"] == ("k1", "b64==")


def test_unredact_cli_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _api as api

    seen = {}
    res = {"events_restored": 2, "thread_id": 7, "notes": []}
    monkeypatch.setattr(api, "unredact", lambda kid, **kw: seen.update(kid=kid, **kw) or res)
    rc = main(["unredact", "k1", "--home", "/h"])
    assert rc == 0
    assert seen == {"kid": "k1", "home": "/h"}
    assert "restored 2 event(s) in thread 7" in capsys.readouterr().out


def test_import_rejects_unknown_provider() -> None:
    with pytest.raises(SystemExit):
        main(["import", "/nonexistent", "--provider", "not-a-provider"])


def test_status_runs_on_empty_home(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["status", "--home", str(tmp_path / "arc")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "home:" in out and "truth:" in out and "index:" in out


def test_coverage_cli_renders_validation_drift(monkeypatch, capsys) -> None:
    """`archive coverage` surfaces the validation-drift ledger volume — the durable
    operator surface for parser format drift, not just daemon logs."""
    from thread_archive import _api as api

    result = {
        "ok": True, "failed": [], "warnings": [],
        "sources": {}, "disabled": {}, "unwatched": {},
        "skips": {"total": 0, "recent": 0, "recent_lines": 0, "days": 7.0},
        "drift": {"total": 3, "recent": 2, "recent_findings": 5, "days": 7.0},
    }
    monkeypatch.setattr(api, "check_coverage", lambda **kw: result)
    rc = main(["coverage", "--home", "/h"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "validation drift: 3 ledger records, 2 in last 7d (5 findings)" in out
    assert "validation-drift.jsonl" in out


def test_reindex_cli_runs_in_isolated_home(tmp_path, monkeypatch, capsys) -> None:
    """`archive reindex` wires to the truth-log reindex. Always pass --home so a
    CLI test never touches the real ~/.thread/archive."""
    from thread_archive import _config as config
    from thread_archive._store import _base
    from thread_archive._truth import jsonl_log

    monkeypatch.delenv(config.ENV_HOME, raising=False)
    monkeypatch.delenv(config.ENV_TRUTH, raising=False)
    monkeypatch.delenv(config.ENV_INDEX, raising=False)
    _base.close_engine()
    jsonl_log.reset_handles()
    try:
        rc = main(["reindex", "--home", str(tmp_path / "arc")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "reindexing" in out and "done" in out
    finally:
        jsonl_log.reset_handles()
        _base.close_engine()
