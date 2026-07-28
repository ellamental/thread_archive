"""The poll loop records what ingest cost, per source, and retains it.

The end-to-end shape: a real poll over a real transcript produces a ledger row
with a real stage split, and the quiet polls that make up most of the loop's life
produce nothing at all.
"""

from __future__ import annotations

import json

from thread_archive._ops import ledger
from thread_archive._watcher import Watcher, ingest_log

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "sessionId": "s1",
        "message": {"role": "user", "content": "hello watcher"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "sessionId": "s1",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi from watcher"}]}}


def _cc_watcher(projects_dir):
    from thread_archive._watcher import ClaudeCodeWatcher

    return ClaudeCodeWatcher(projects_dirs=[projects_dir])


def _session(projects_dir, name, lines):
    proj = projects_dir / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    return path


def _rows(home):
    return list(ledger.iter_rows(home / ingest_log.LEDGER_FILE))


def test_a_poll_that_imported_lands_a_row_with_its_stage_split(
    archive_home, tmp_path
) -> None:
    from thread_archive._store import init_db

    init_db()
    projects = tmp_path / "projects"
    _session(projects, "s1", [USER, ASSISTANT])

    watcher = Watcher(watchers=[_cc_watcher(projects)], home=str(archive_home))
    result = watcher.poll_once()

    assert result.events_created > 0
    (row,) = [r for r in _rows(archive_home) if r["kind"] == "ingest-pass"]
    assert row["source"] == "claude-code"
    assert row["events"] == result.events_created
    assert row["items"] == 1
    assert row["parse_ms"] >= 0.0
    assert row["pass_ms"] >= row["total_ms"], (
        "the pass includes the walking and statting the stages do not"
    )


def test_a_quiet_poll_writes_nothing(archive_home, tmp_path) -> None:
    from thread_archive._store import init_db

    init_db()
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)

    watcher = Watcher(watchers=[_cc_watcher(projects)], home=str(archive_home))
    watcher.poll_once()
    assert _rows(archive_home) == []


def test_the_second_poll_of_an_unchanged_store_is_still_charged_honestly(
    archive_home, tmp_path
) -> None:
    """A fingerprint-skipped file costs a stat and nothing else — so the loop's
    per-source total keeps climbing while `import_ms` does not."""
    from thread_archive._store import init_db

    init_db()
    projects = tmp_path / "projects"
    _session(projects, "s1", [USER, ASSISTANT])

    watcher = Watcher(watchers=[_cc_watcher(projects)], home=str(archive_home))
    watcher.poll_once()
    first = dict(watcher._source_totals["claude-code"])
    watcher.poll_once()
    second = watcher._source_totals["claude-code"]

    assert second["ms"] >= first["ms"], "wall time keeps accruing on every poll"
    assert second["import_ms"] == first["import_ms"], (
        "a skipped file does no import work, and the split has to say so"
    )


def test_per_source_totals_separate_importing_from_looking_for_work(
    archive_home, tmp_path
) -> None:
    from thread_archive._store import init_db

    init_db()
    projects = tmp_path / "projects"
    _session(projects, "s1", [USER, ASSISTANT])

    watcher = Watcher(watchers=[_cc_watcher(projects)], home=str(archive_home))
    watcher.poll_once()
    totals = watcher._source_totals["claude-code"]
    assert totals["import_ms"] > 0
    assert totals["ms"] >= totals["import_ms"]


def test_a_maintenance_pass_records_the_checkpoint_split(archive_home, tmp_path) -> None:
    from thread_archive._store import init_db

    init_db()
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)

    watcher = Watcher(watchers=[_cc_watcher(projects)], home=str(archive_home))
    watcher.maintain()

    (row,) = [r for r in _rows(archive_home) if r["kind"] == "maintenance"]
    assert "checkpoint_ms" in row
    assert "lock_ms" in row, "the checkpoint's own lock wait rides out to the ledger"
    assert "thread_meta_ms" in row and "code_ms" in row
