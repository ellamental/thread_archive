"""The public plugin harness (:mod:`thread_archive.provider.testing`) as a
provider plugin author experiences it.

``test_provider_goldens.py`` dogfoods the happy path (every internal golden
runs through ``assert_golden``); this file covers the harness's own contract:
the documented conftest wiring hands out an isolated ``archive_home``, the
``UPDATE_GOLDENS`` flow writes-and-skips rather than passing, a missing golden
names the fix, a diverged golden fails with a reviewable message, and
``write_jsonl`` really produces the torn tail it promises.
"""

from __future__ import annotations

import json

import pytest

from thread_archive.provider.testing import (
    assert_golden,
    init_archive,
    normalized_truth,
    write_jsonl,
)

pytest_plugins = ["pytester"]

SESSION = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
     "cwd": "/p", "message": {"role": "user", "content": "harness probe"}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-01-01T10:00:05Z",
     "sessionId": "s1", "message": {"role": "assistant", "model": "m",
                                    "content": [{"type": "text", "text": "harness answer"}]}},
]


def _import_fixture(home) -> None:
    from thread_archive._importers import import_session_incremental

    init_archive()
    f = home / "sess.jsonl"
    write_jsonl(f, SESSION)
    import_session_incremental(f, "proj:s1")


# ── the documented conftest wiring, end to end ───────────────────────────────

@pytest.mark.integration
def test_plugin_conftest_flow_update_then_pass_then_diverge(pytester, monkeypatch) -> None:
    """The exact flow the module docstring sells: enable the plugin in
    conftest.py, use its ``archive_home``, generate the golden under
    ``UPDATE_GOLDENS=1`` (a *skip*, not a pass), rerun green, and see a
    divergence fail with the review message."""
    pytester.makeconftest('pytest_plugins = ["thread_archive.provider.testing"]')
    pytester.makepyfile(
        test_plugin=f"""
        import json
        from pathlib import Path
        from thread_archive._importers import import_session_incremental
        from thread_archive.provider.testing import assert_golden, init_archive, write_jsonl

        SESSION = {SESSION!r}
        GOLDEN_DIR = Path(__file__).parent / "goldens"

        def test_my_provider_golden(archive_home):
            init_archive()
            path = archive_home / "sess.jsonl"
            write_jsonl(path, SESSION)
            import_session_incremental(path, "proj:s1")
            assert_golden("myprovider", archive_home, GOLDEN_DIR)
        """
    )

    monkeypatch.setenv("UPDATE_GOLDENS", "1")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:xdist")
    result.assert_outcomes(skipped=1)
    golden = pytester.path / "goldens" / "myprovider.json"
    assert golden.is_file(), "UPDATE_GOLDENS run must write the golden"

    monkeypatch.delenv("UPDATE_GOLDENS")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:xdist")
    result.assert_outcomes(passed=1)

    # a quiet importer change: hand-shift what the golden pins
    records = json.loads(golden.read_text(encoding="utf-8"))
    records[0]["title"] = "renamed by format drift"
    golden.write_text(json.dumps(records), encoding="utf-8")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:xdist")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*diverged from the reviewed golden*"])


@pytest.mark.integration
def test_plugin_archive_home_is_isolated_from_the_real_home(pytester) -> None:
    """The fixture's whole promise: an import inside a plugin's test can never
    reach the machine's actual conversation store."""
    pytester.makeconftest('pytest_plugins = ["thread_archive.provider.testing"]')
    pytester.makepyfile(
        test_iso="""
        import os
        from pathlib import Path
        from thread_archive._config import ENV_HOME, resolve_paths
        from thread_archive.provider.testing import init_archive

        def test_home_is_the_tmp_archive(archive_home, tmp_path):
            assert os.environ[ENV_HOME] == str(archive_home)
            assert Path(archive_home).is_relative_to(tmp_path)
            init_archive()
            assert resolve_paths(None).index_path.is_relative_to(archive_home)
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:xdist")
    result.assert_outcomes(passed=1)


# ── the harness functions' own edges ─────────────────────────────────────────

def test_missing_golden_fails_and_names_the_fix(archive_home) -> None:
    _import_fixture(archive_home)
    with pytest.raises(AssertionError, match="UPDATE_GOLDENS=1"):
        assert_golden("never-generated", archive_home, archive_home / "goldens")


def test_update_env_regenerates_and_skips(archive_home, monkeypatch, tmp_path) -> None:
    """Regeneration is a skip, never a pass — an unread diff is not evidence."""
    _import_fixture(archive_home)
    golden_dir = tmp_path / "goldens"
    monkeypatch.setenv("UPDATE_GOLDENS", "1")
    with pytest.raises(pytest.skip.Exception, match="review the diff"):
        assert_golden("fresh", archive_home, golden_dir)
    written = json.loads((golden_dir / "fresh.json").read_text(encoding="utf-8"))
    assert any(r.get("type") == "thread" for r in written)

    # the regenerated golden immediately verifies once the env flag is gone
    monkeypatch.delenv("UPDATE_GOLDENS")
    assert_golden("fresh", archive_home, golden_dir)


def test_custom_update_env_name(archive_home, monkeypatch, tmp_path) -> None:
    _import_fixture(archive_home)
    monkeypatch.setenv("MY_GOLDENS", "1")
    with pytest.raises(pytest.skip.Exception):
        assert_golden("named", archive_home, tmp_path, update_env="MY_GOLDENS")
    assert (tmp_path / "named.json").is_file()


def test_divergence_fails_with_review_message(archive_home, tmp_path) -> None:
    _import_fixture(archive_home)
    golden_dir = tmp_path / "goldens"
    golden_dir.mkdir()
    (golden_dir / "drift.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="diverged from the reviewed golden"):
        assert_golden("drift", archive_home, golden_dir)


def test_normalized_truth_scrubs_home_and_ordinalizes_ids(archive_home) -> None:
    _import_fixture(archive_home)
    records = normalized_truth(archive_home)
    text = json.dumps(records)
    assert str(archive_home) not in text, "tmp path must be scrubbed to a placeholder"
    streams = {r["stream"] for r in records if r.get("type") == "event" and r.get("stream")}
    assert streams and all(s.startswith("s") for s in streams), "stream ids become ordinals"


def test_normalized_truth_on_an_empty_home_is_empty(archive_home) -> None:
    assert normalized_truth(archive_home) == []


def test_write_jsonl_torn_tail_ends_mid_line(tmp_path) -> None:
    path = tmp_path / "torn.jsonl"
    write_jsonl(path, [{"a": 1}, {"b": 2}], torn_tail='{"c": 3, "trunc')
    text = path.read_text(encoding="utf-8")
    assert text.endswith('{"c": 3, "trunc') and not text.endswith("\n")
    complete = text.splitlines()[:-1]
    assert [json.loads(ln) for ln in complete[:2]] == [{"a": 1}, {"b": 2}]
