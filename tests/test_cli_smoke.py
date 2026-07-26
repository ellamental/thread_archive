"""The CLI surface is real: `thread_archive --help` works, every verb is wired, and the
verbs that touch the archive are driven end-to-end over a real seeded home —
argv in, real work, and the effect the flag asked for read back off disk.

Companion to ``test_cov_cli.py``, which drives the report functions' formatting
branches directly with the result shapes a real run can't produce.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from thread_archive import _api as ta
from thread_archive._truth import jsonl_log
from thread_archive.cli import build_parser, main

from .helpers import corrupt_event_line, import_cc_session, one_thread_file


def test_help_runs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "archive" in out


def test_mine_lists_miners(capsys: pytest.CaptureFixture[str]) -> None:
    # Bare `mine` is the registry list view — no archive access, no token spend —
    # so it drives cmd_mine's dispatch into the `_mine` package end to end.
    assert main(["mine"]) == 0
    out = capsys.readouterr().out
    assert "Gold miners" in out
    for name in ("query", "topic", "rerank", "querygen"):
        assert name in out


def test_mine_points_at_the_repo_when_the_package_is_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `_mine` is dev machinery excluded from the wheel, so this is the path every
    # install takes: a pointer at the repo, never an ImportError traceback.
    # Reproduced without patching, by building the wheel's actual shape — a package
    # dir symlinking every module EXCEPT `_mine` — and importing the real CLI
    # through it. `cmd_mine` probes `f"{__package__}._mine"`, so under this package
    # name the miners are genuinely absent, exactly as in an install.
    pkg = Path(ta.__file__).resolve().parent
    root = tmp_path / "wheel-shape"
    shadow = root / "ta_without_mine"
    shadow.mkdir(parents=True)
    for child in pkg.iterdir():
        if child.name not in {"_mine", "__pycache__"}:
            (shadow / child.name).symlink_to(child)

    sys.path.insert(0, str(root))
    try:
        rc = importlib.import_module("ta_without_mine.cli").main(["mine"])
    finally:
        # The shadow package points into tmp_path; don't leave it importable.
        sys.path.remove(str(root))
        for name in [n for n in sys.modules if n.startswith("ta_without_mine")]:
            del sys.modules[name]

    assert rc == 2
    err = capsys.readouterr().err
    assert "development machinery" in err, err
    assert "github.com" in err, err


def test_mine_stays_off_the_public_help(capsys: pytest.CaptureFixture[str]) -> None:
    # Every verb `--help` lists should be one an install can actually run, and
    # `mine` is not. It stays registered (below) — just unadvertised.
    with pytest.raises(SystemExit):
        main(["--help"])
    assert not re.search(r"^\s+mine\b", capsys.readouterr().out, re.M)


def test_all_subcommands_present() -> None:
    parser = build_parser()
    # Reach into the subparsers action to assert the full command surface is wired.
    # (test_public_api.py owns the boundary ratchet; this is the wiring smoke.)
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    assert set(sub.choices) == {
        "setup", "import", "import-export", "providers", "watch", "reindex", "snapshot",
        "migrate", "embed",
        "status", "loads", "archives",
        "eval", "mine", "backup", "verify", "repair", "restore-drill", "restore",
        "nightly", "coverage", "mirror", "daemon",
        "fix-import", "self-update",
    }


# ── verb → real work (arg mapping + exit codes, driven over a seeded store) ───
# The verbs are wired into LaunchAgent plists, cron, and the monitor's heartbeat
# contract, so a silent arg-mapping regression hurts operationally. Each test
# runs the verb for real against a throwaway archive home and reads the effect
# the flag it passed is supposed to have. (The output-formatting branches a real
# run cannot produce — an incomplete mirror, a shrunken backup — are driven as
# report functions in test_cov_cli.py.)


@pytest.fixture
def seeded(archive_home, tmp_path):
    """A real one-thread archive at ``archive_home``, checkpointed to truth."""
    import_cc_session(tmp_path)
    ta.checkpoint()
    return archive_home


def test_embed_cli_dispatches(seeded, monkeypatch, capsys) -> None:
    """`thread_archive embed` wires to api.embed. The suite runs model-free (conftest),
    so a real run embeds nothing and the cap can only be read at the api seam —
    the one verb whose effect is invisible without the [embeddings] extra."""
    seen = {}
    monkeypatch.setattr(ta, "embed", lambda **kw: seen.update(kw) or {"embedded": 4})
    rc = main(["embed", "--rebuild", "--limit", "100", "--newest-first",
               "--home", str(seeded)])
    assert rc == 0
    # progress is None here: capsys makes stdout a non-tty, so the CLI stays quiet.
    assert seen == {"home": str(seeded), "rebuild": True, "max_events": 100,
                    "newest_first": True, "progress": None}
    assert "embedded 4" in capsys.readouterr().out


def test_embed_cli_runs_for_real_model_free(seeded, capsys) -> None:
    """Model-free the verb is a clean no-op, not an error: the real path runs."""
    assert main(["embed", "--home", str(seeded)]) == 0
    out = capsys.readouterr().out
    assert "rebuild=False" in out and "embedded 0" in out


def test_eval_behavior_cli_runs_over_a_real_home(seeded, capsys) -> None:
    """The `eval` verb opens the real archive and reports; --behavior needs no model
    arms, so it drives cmd_eval end-to-end (open, thread count, trail read) fast.
    A seeded home has no thread_search trail, so the no-searches branch is exercised."""
    assert main(["eval", "--behavior", "--home", str(seeded)]) == 0
    out = capsys.readouterr().out
    assert "Search health" in out
    assert "No searches recorded" in out


def test_snapshot_cli_builds_a_frozen_home(seeded, tmp_path, capsys) -> None:
    """The `snapshot` verb copies truth, materializes the index, and reports —
    the dest is a real, verified home the operator can point an eval at."""
    dest = tmp_path / "frozen"
    rc = main(["snapshot", str(dest), "--home", str(seeded)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "done:" in out and str(dest) in out
    assert (dest / "truth" / "manifest.json").is_file()
    assert (dest / "index.db").is_file()
    assert (dest / "snapshot.json").is_file()

    # A non-empty stranger dir is refused without --force, taken with it.
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "x").write_text("nope")
    assert main(["snapshot", str(stranger), "--home", str(seeded)]) == 1
    assert "snapshot refused" in capsys.readouterr().err
    assert main(["snapshot", str(stranger), "--force", "--home", str(seeded)]) == 0


def test_backup_cli_mirrors_the_real_truth(seeded, tmp_path, capsys) -> None:
    dest = tmp_path / "mirror"
    rc = main(["backup", str(dest), "--home", str(seeded)])
    assert rc == 0
    assert "backed up" in capsys.readouterr().out
    # a real mirror: the thread file and the manifest are actually at the dest
    assert (dest / "manifest.json").is_file()
    assert list(dest.rglob("*.jsonl"))


def test_backup_cli_no_verify_skips_the_pre_backup_check(seeded, tmp_path, capsys) -> None:
    """--no-verify reaches api.backup(verify_first=...): with the check on, a
    damaged source is flagged and the run exits nonzero; with it off it is not."""
    corrupt_event_line(one_thread_file(seeded))

    assert main(["backup", str(tmp_path / "m1"), "--home", str(seeded)]) == 1
    assert "pre-backup verify FAILED" in capsys.readouterr().out

    assert main(["backup", str(tmp_path / "m2"), "--no-verify", "--home", str(seeded)]) == 0
    assert "pre-backup verify FAILED" not in capsys.readouterr().out


def test_backup_cli_allow_shrink_overrides_the_guard(seeded, tmp_path, capsys) -> None:
    """--allow-shrink reaches api.backup(allow_shrink=...): a source truth file
    that lost bytes is refused by default and copied when overridden."""
    dest = tmp_path / "mirror"
    assert main(["backup", str(dest), "--home", str(seeded)]) == 0
    capsys.readouterr()

    tf = one_thread_file(seeded)
    mirrored = next(dest.rglob(tf.name))
    tf.write_text(tf.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    jsonl_log.reset_handles()
    shrunk = tf.read_text(encoding="utf-8")

    # refused: the guard keeps the last good copy, so the mirror reads incomplete
    assert main(["backup", str(dest), "--no-verify", "--home", str(seeded)]) == 1
    out = capsys.readouterr().out
    assert "SHRINK GUARD: 1" in out and "MIRROR INCOMPLETE" in out
    assert mirrored.read_text(encoding="utf-8") != shrunk  # last good copy kept

    assert main(["backup", str(dest), "--allow-shrink", "--no-verify",
                 "--home", str(seeded)]) == 0
    assert "SHRINK GUARD" not in capsys.readouterr().out
    assert mirrored.read_text(encoding="utf-8") == shrunk


def test_verify_cli_tiers_and_exit_codes(seeded, tmp_path, capsys) -> None:
    """--deep / --hashes / --backup each reach api.verify and add their section;
    a clean archive is OK/0 and a damaged one is FAILED/1."""
    dest = tmp_path / "mirror"
    assert main(["backup", str(dest), "--home", str(seeded)]) == 0
    capsys.readouterr()

    assert main(["verify", "--home", str(seeded)]) == 0
    shallow = capsys.readouterr().out
    assert "OK" in shallow
    assert "deep:" not in shallow and "hashes[cross]" not in shallow

    rc = main(["verify", "--deep", "--hashes", "--backup", str(dest), "--home", str(seeded)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "deep:" in out and "hashes[cross]" in out and f"backup[{dest}]" in out

    corrupt_event_line(one_thread_file(seeded))
    assert main(["verify", "--home", str(seeded)]) == 1
    assert "FAILED:" in capsys.readouterr().out


def test_restore_drill_cli_rebuilds_from_the_mirror(seeded, tmp_path, capsys) -> None:
    dest = tmp_path / "mirror"
    assert main(["backup", str(dest), "--home", str(seeded)]) == 0
    capsys.readouterr()

    rc = main(["restore-drill", str(dest), "--keep-home", "--home", str(seeded)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out
    # --keep-home reached api.restore_drill: the throwaway home survived the run
    kept = out.split("drill home kept: ")[1].splitlines()[0]
    assert (Path(kept) / "index.db").is_file()


def test_restore_cli_names_the_generation_it_was_asked_for(seeded, tmp_path, capsys) -> None:
    """--generation reaches api.restore: the run is qualified by it in the source
    line and in the error when that restore point does not exist."""
    rc = main(["restore", str(tmp_path / "mirror"), "--to", str(tmp_path / "new"),
               "--generation", "2026-07-14T00-00-00"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "(generation 2026-07-14T00-00-00)" in out
    assert "2026-07-14T00-00-00" in out.split("FAILED: ")[1]
    assert "RESTORE FAILED" in out


def test_restore_cli_requires_a_target_home(tmp_path, capsys) -> None:
    assert main(["restore", str(tmp_path / "mirror")]) == 2
    assert "--to <home> is required" in capsys.readouterr().out


def test_restore_cli_lists_generations(seeded, tmp_path, capsys) -> None:
    dest = tmp_path / "mirror"
    assert main(["backup", str(dest), "--home", str(seeded)]) == 0
    assert main(["backup", str(dest), "--home", str(seeded)]) == 0  # 2nd run keeps a generation
    capsys.readouterr()

    assert main(["restore", str(dest), "--list-generations"]) == 0
    listed = capsys.readouterr().out.split()
    assert listed and all((dest / ".generations" / g).is_dir() for g in listed)

    assert main(["restore", str(tmp_path / "never-backed-up"), "--list-generations"]) == 0
    assert "no generations retained" in capsys.readouterr().out


def test_nightly_cli_runs_every_stage(seeded, tmp_path, capsys) -> None:
    rc = main(["nightly", str(tmp_path / "mirror"), "--home", str(seeded)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "backup: 1 files" in out or "backup: " in out
    assert "verify [" in out and "restore drill: ok" in out
    assert "NIGHTLY OK" in out


def test_nightly_cli_no_drill_skips_the_drill_stage(seeded, tmp_path, capsys) -> None:
    """--no-drill reaches api.nightly(drill=False): the stage is absent, not failed."""
    rc = main(["nightly", str(tmp_path / "mirror"), "--no-drill", "--home", str(seeded)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "restore drill:" not in out  # the stage line, not the header's blurb
    assert "NIGHTLY OK" in out


def test_nightly_cli_notify_url_reaches_the_pipeline(seeded, tmp_path, capsys) -> None:
    """--notify-url reaches api.nightly: a failing night really POSTs the stage
    names at the URL argv named. Proved against a loopback server, not a stub."""
    class _Notify(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.server.posts.append(json.loads(self.rfile.read(n)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Notify)
    srv.posts = []
    threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.02),
                     daemon=True).start()
    corrupt_event_line(one_thread_file(seeded))
    try:
        rc = main(["nightly", str(tmp_path / "mirror"), "--home", str(seeded),
                   "--notify-url", f"http://127.0.0.1:{srv.server_port}/api/notify"])
    finally:
        srv.shutdown()
        srv.server_close()

    assert rc == 1
    assert "notify:" not in capsys.readouterr().out  # delivered, so no failure note
    assert srv.posts and "verify" in srv.posts[0]["message"]


def test_nightly_cli_fails_with_stage_names(seeded, tmp_path, capsys) -> None:
    corrupt_event_line(one_thread_file(seeded))
    rc = main(["nightly", str(tmp_path / "mirror"), "--home", str(seeded)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "verify [" in out and "FAILED" in out
    stages = out.rsplit("NIGHTLY FAILED: ", 1)[1].strip().split(", ")
    assert "verify" in stages  # the damage is named by the stage that found it


def test_repair_cli_dry_run_then_applies(seeded, capsys) -> None:
    """--dry-run reaches api.repair(dry_run=...): the plan reports the damage and
    leaves it, the real run quarantines it."""
    tf = one_thread_file(seeded)
    corrupt_event_line(tf)
    damaged = tf.read_text(encoding="utf-8")

    assert main(["repair", "--dry-run", "--home", str(seeded)]) == 0
    out = capsys.readouterr().out
    assert "would quarantine 1 unparseable line(s)" in out
    assert tf.read_text(encoding="utf-8") == damaged  # planned only

    assert main(["repair", "--home", str(seeded)]) == 0
    out = capsys.readouterr().out
    assert "quarantined 1 unparseable line(s)" in out
    assert "run `thread_archive verify`" in out
    assert tf.read_text(encoding="utf-8") != damaged


def test_daemon_backup_install_dispatches(monkeypatch, capsys) -> None:
    from thread_archive import _service

    seen = {}
    monkeypatch.setattr(
        _service, "install_backup",
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


def test_import_rejects_unknown_provider() -> None:
    with pytest.raises(SystemExit):
        main(["import", "/nonexistent", "--provider", "not-a-provider"])


def test_status_runs_on_empty_home(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["status", "--home", str(tmp_path / "arc")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "home:" in out and "truth:" in out and "index:" in out


def test_coverage_cli_checks_the_real_sources(seeded, capsys) -> None:
    """`thread_archive coverage` reconciles the home's configured sources against the
    archive: a fresh home with nothing captured yet is green and lists no gaps."""
    rc = main(["coverage", "--home", str(seeded)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-code" in out  # a configured source really got a row
    assert "OK" in out and "FAILED:" not in out


def test_reindex_cli_runs_in_isolated_home(tmp_path, monkeypatch, capsys) -> None:
    """`thread_archive reindex` wires to the truth-log reindex. Always pass --home so a
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


def test_migrate_cli_is_a_noop_on_current_truth(seeded, capsys) -> None:
    rc = main(["migrate", "--home", str(seeded)])
    assert rc == 0
    assert "already at truth format v2" in capsys.readouterr().out


def test_providers_cli_renders_patch_traits(archive_home, capsys) -> None:
    """`thread_archive providers` labels providers carrying a fix-import patch: active
    (pinned or not) and retired — the operator's view of the patch lifecycle.
    Read from the home's own config.json, the file `thread_archive fix-import` writes."""
    (archive_home / "config.json").write_text(json.dumps({"providers": {
        "claude-code": {"enabled": True, "patch": {"pinned": True}},
        "cursor": {"enabled": True, "patch": {}},
        "codex": {"patch": {"retired": True}},
        "grok": {"enabled": False, "patch": {}},  # inactive, not retired → no trait
    }}), encoding="utf-8")
    rc = main(["providers", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    lines = {ln.split()[0]: ln for ln in out.splitlines() if ln.strip()}
    assert "patched (pinned)" in lines["claude-code"]
    assert "patched" in lines["cursor"] and "(pinned)" not in lines["cursor"]
    assert "patch retired" in lines["codex"]
    assert "patch" not in lines["grok"]  # disabled + unretired patch entry says nothing


def test_fix_import_activate_dispatches_and_prints_reimport(monkeypatch, capsys) -> None:
    from thread_archive import _repair

    seen = {}
    summary = {"reimport": {"watermarks_reset": 2, "poll_events": 5, "snapshot_events": 1}}
    monkeypatch.setattr(
        _repair, "activate",
        lambda provider, home, reimport: seen.update(
            provider=provider, home=home, reimport=reimport) or summary,
    )
    rc = main(["fix-import", "claude-code", "--activate", "--home", "/h"])
    assert rc == 0
    assert seen == {"provider": "claude-code", "home": "/h", "reimport": True}
    out = capsys.readouterr().out
    assert "re-import: 2 watermark(s) reset, 5 event(s) from the live store, 1 from quarantine snapshots" in out
    assert "patch active" in out


def test_fix_import_scaffolds_and_names_the_next_step(archive_home, capsys) -> None:
    """`thread_archive fix-import <provider>` really writes the patch scaffold into the
    home and points at the protocol the operator (or their agent) reads next."""
    rc = main(["fix-import", "cursor", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    target = archive_home / "plugins" / "cursor"
    assert str(target) in out
    assert (target / "PROTOCOL.md").is_file()
    assert "thread_archive fix-import cursor --activate" in out


def test_fix_import_rejects_an_unknown_provider(archive_home, capsys) -> None:
    """A name the registry doesn't know is operator guidance and exit 1, not a
    traceback out of the scaffold."""
    assert main(["fix-import", "not-a-provider", "--home", str(archive_home)]) == 1
    assert "unknown provider" in capsys.readouterr().out


def test_module_is_runnable_as_a_script(tmp_path) -> None:
    """``python -m thread_archive.cli`` dispatches to main() and exits with its
    return code — no args prints help and exits 0, a bad verb exits nonzero."""
    def run(*argv):
        return subprocess.run(
            [sys.executable, "-m", "thread_archive.cli", *argv],
            capture_output=True, text=True,
        )

    ok = run()
    assert ok.returncode == 0, ok.stderr
    assert "archive" in ok.stdout and "usage" in ok.stdout.lower()

    bad = run("not-a-verb")
    assert bad.returncode != 0
