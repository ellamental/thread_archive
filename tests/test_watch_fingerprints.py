"""Poll fingerprints survive a restart — without losing the corpus re-verify.

The in-memory cache died with the process, so every restart re-read the whole
archive to rediscover what it already knew. Persisting it removes that cost; the
re-verify window is what keeps the integrity sweep the restart was accidentally
providing.
"""

from __future__ import annotations

import json

from thread_archive._watcher import fingerprints
from thread_archive._watcher.sources import ClaudeCodeWatcher

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "sessionId": "s1",
        "message": {"role": "user", "content": "hello fingerprints"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "sessionId": "s1",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi"}]}}


def _session(projects, name, lines):
    proj = projects / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    return p


def _fresh_watcher(projects):
    """A watcher as a restart would build it: no in-memory fingerprints."""
    return ClaudeCodeWatcher(projects_dirs=[projects])


def test_a_restart_does_not_re_import_an_unchanged_store(archive_home, tmp_path) -> None:
    from thread_archive._store import init_db

    init_db()
    projects = tmp_path / "projects"
    _session(projects, "s1", [USER, ASSISTANT])

    first = _fresh_watcher(projects).poll()
    assert first.events_created > 0

    # A new process: same store, no in-memory state. It must recognise the file
    # from the persisted fingerprint rather than reading it again.
    again = _fresh_watcher(projects)
    result = again.poll()
    assert result.events_created == 0
    assert result.sources_checked == 1, "the file was seen and skipped, not re-read"


def test_a_changed_file_is_still_picked_up_across_a_restart(archive_home, tmp_path) -> None:
    from thread_archive._store import init_db

    init_db()
    projects = tmp_path / "projects"
    path = _session(projects, "s1", [USER, ASSISTANT])
    _fresh_watcher(projects).poll()

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(USER, uuid="u2")) + "\n")
        fh.write(json.dumps(dict(ASSISTANT, uuid="a2")) + "\n")

    result = _fresh_watcher(projects).poll()
    assert result.events_created > 0, (
        "a persisted fingerprint must never hide a real append — that is capture loss"
    )


def test_a_stale_cache_forces_a_full_re_verify(archive_home, tmp_path, monkeypatch) -> None:
    from thread_archive._store import init_db

    init_db()
    projects = tmp_path / "projects"
    _session(projects, "s1", [USER, ASSISTANT])
    _fresh_watcher(projects).poll()

    # Past the window, the cache is ignored and the file is read again — the
    # integrity sweep a restart used to provide by accident.
    monkeypatch.setenv("THREAD_ARCHIVE_FINGERPRINT_TTL_S", "0.000001")
    assert fingerprints.load("claude-code") == {}


def test_persistence_can_be_turned_off(archive_home, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_FINGERPRINT_TTL_S", "0")
    fingerprints.save("claude-code", {"/a": (1, 2)}, force=True)
    assert fingerprints.load("claude-code") == {}, "ttl=0 is the pre-persistence behavior"


def test_a_corrupt_cache_degrades_to_a_full_scan(archive_home) -> None:
    (archive_home / fingerprints.STATE_FILE).write_text("{not json", encoding="utf-8")
    assert fingerprints.load("claude-code") == {}


def test_malformed_entries_are_dropped_individually(archive_home) -> None:
    from datetime import datetime, timezone

    (archive_home / fingerprints.STATE_FILE).write_text(json.dumps({
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "sources": {"claude-code": {
            "/good": [123, 45],
            "/bad-shape": ["x", "y"],
            "/short": [1],
            "/not-a-list": 7,
        }},
    }), encoding="utf-8")
    # A hand-edited or truncated entry must degrade to a re-read, never to a skip
    # on a fingerprint nobody can vouch for.
    assert fingerprints.load("claude-code") == {"/good": (123, 45)}


def test_sources_do_not_clobber_each_other(archive_home) -> None:
    fingerprints.save("claude-code", {"/a": (1, 2)}, force=True)
    fingerprints.save("codex", {"/b": (3, 4)}, force=True)
    assert fingerprints.load("claude-code") == {"/a": (1, 2)}
    assert fingerprints.load("codex") == {"/b": (3, 4)}


def test_saves_are_throttled(archive_home) -> None:
    assert fingerprints.save("cloth", {"/a": (1, 2)}, force=True) is True
    assert fingerprints.save("cloth", {"/a": (9, 9)}) is False, (
        "a per-poll write would cost more than the reads this saves"
    )


def test_clear_forces_the_next_poll_to_re_verify(archive_home) -> None:
    fingerprints.save("claude-code", {"/a": (1, 2)}, force=True)
    fingerprints.clear()
    assert fingerprints.load("claude-code") == {}


def test_stamp_age_reports_when_the_corpus_was_last_observed(archive_home) -> None:
    assert fingerprints.stamp_age_s() is None
    fingerprints.save("claude-code", {"/a": (1, 2)}, force=True)
    age = fingerprints.stamp_age_s()
    assert age is not None and 0 <= age < 60


def test_an_unparseable_ttl_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_FINGERPRINT_TTL_S", "soon")
    assert fingerprints.reverify_after_s() == 6 * 3600.0


def test_a_stamp_past_the_window_yields_no_fingerprints(archive_home) -> None:
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    (archive_home / fingerprints.STATE_FILE).write_text(json.dumps({
        "verified_at": stale, "sources": {"claude-code": {"/a": [1, 2]}},
    }), encoding="utf-8")
    assert fingerprints.load("claude-code") == {}


def test_a_stamp_from_the_future_yields_no_fingerprints(archive_home) -> None:
    """A clock that moved backwards: the observation cannot be aged, so re-verify."""
    from datetime import datetime, timedelta, timezone

    ahead = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    (archive_home / fingerprints.STATE_FILE).write_text(json.dumps({
        "verified_at": ahead, "sources": {"claude-code": {"/a": [1, 2]}},
    }), encoding="utf-8")
    assert fingerprints.load("claude-code") == {}


def test_an_unparseable_stamp_degrades_to_a_full_scan(archive_home) -> None:
    (archive_home / fingerprints.STATE_FILE).write_text(json.dumps({
        "verified_at": "not-a-date", "sources": {"claude-code": {"/a": [1, 2]}},
    }), encoding="utf-8")
    assert fingerprints.load("claude-code") == {}
    assert fingerprints.stamp_age_s() is None


def test_a_failed_save_reports_false_and_never_raises(archive_home) -> None:
    # The state file's name is occupied by a non-empty directory, so the atomic
    # replace cannot land. The loop must shrug, not die.
    blocked = archive_home / fingerprints.STATE_FILE
    (blocked / "occupied").mkdir(parents=True)
    assert fingerprints.save("claude-code", {"/a": (1, 2)}, force=True) is False


def test_clear_is_a_no_op_when_nothing_was_persisted(archive_home) -> None:
    fingerprints.clear()
    assert fingerprints.load("claude-code") == {}
