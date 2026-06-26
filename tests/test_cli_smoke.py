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
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    assert set(sub.choices) == {
        "import", "import-export", "watch", "search", "read", "reindex", "status",
        "backup", "verify", "web",
    }


def test_status_runs_on_empty_home(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["status", "--home", str(tmp_path / "arc")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "home:" in out and "truth:" in out and "index:" in out


def test_reindex_cli_runs_in_isolated_home(tmp_path, monkeypatch, capsys) -> None:
    """`archive reindex` wires to the truth-log reindex. Always pass --home so a
    CLI test never touches the real ~/.thread_archive."""
    from thread_archive import config
    from thread_archive.store import _base
    from thread_archive.truth import jsonl_log

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
