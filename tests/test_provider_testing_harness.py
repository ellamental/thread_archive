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
from pathlib import Path

import pytest

from thread_archive.provider import (
    ExportSpec,
    Provider,
    SourceDiscovery,
    SourceWatcher,
    WatchResult,
)
from thread_archive.provider.testing import (
    assert_golden,
    assert_provider_contract,
    assert_reimport_adds_nothing,
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


# ── the conformance kit, made to fail ────────────────────────────────────────
# tests/test_provider_contract.py runs these over the built-ins, which pass — so
# on their own those tests cannot tell a working check from one that asserts
# nothing. This is the other half: one deliberately broken provider per rule.


class _Watcher(SourceWatcher):
    """A minimal conforming watcher, with each conformance property as a knob so
    a test can break exactly one of them."""

    def __init__(self, name="myharness", *, available=False, discovers=None,
                 paths=(), items=()):
        self._name, self._available = name, available
        self._discovers = available if discovers is None else discovers
        self._paths, self._items = list(paths), list(items)

    @property
    def source_name(self):
        return self._name

    def is_available(self):
        return self._available

    def poll(self, on_item=None):
        return WatchResult()

    def discover(self):
        return SourceDiscovery(name=self._name, available=self._discovers)

    def store_paths(self):
        return iter(self._paths)

    def store_items(self):
        return iter(self._items)


def _provider(**overrides) -> Provider:
    base = {"name": "myharness", "label": "My Harness", "watcher": _Watcher}
    return Provider(**{**base, **overrides})


def test_a_conforming_plugin_passes() -> None:
    """The baseline the negative cases are read against."""
    assert_provider_contract(_provider())


def test_a_factory_is_accepted_like_a_descriptor() -> None:
    """Entry points may resolve to either, so the check takes either."""
    assert_provider_contract(lambda: _provider())


@pytest.mark.parametrize("name", ["My_Harness", "my harness", "myHarness", "my--harness"])
def test_an_unstable_name_is_rejected(name: str) -> None:
    with pytest.raises(AssertionError, match="not a stable identifier"):
        assert_provider_contract(_provider(name=name))


def test_a_dangling_follows_is_rejected() -> None:
    with pytest.raises(AssertionError, match="follows='nope' names no known provider"):
        assert_provider_contract(_provider(follows="nope"))


def test_a_sibling_plugin_may_be_referenced() -> None:
    """A plugin shipping several providers references its own set, which the
    built-in registry has never heard of."""
    assert_provider_contract(_provider(follows="my-other"), siblings=["my-other"])


def test_a_provider_nothing_can_feed_is_rejected() -> None:
    with pytest.raises(AssertionError, match="neither a watcher nor an export"):
        assert_provider_contract(_provider(watcher=None))


def test_an_export_detect_that_raises_is_rejected() -> None:
    """The drop zone offers every bundle to every spec, so one raising detect
    stops the queue for every other provider."""
    def boom(path):
        raise OSError("no such thing")

    spec = ExportSpec(detect=boom, importer=lambda p, **k: None,
                      label="My Export", kind="myharness")
    with pytest.raises(AssertionError, match="export.detect raised OSError"):
        assert_provider_contract(_provider(export=spec))


def test_an_export_detect_that_claims_everything_is_rejected() -> None:
    spec = ExportSpec(detect=lambda path: True, importer=lambda p, **k: None,
                      label="My Export", kind="myharness")
    with pytest.raises(AssertionError, match="claims an empty directory"):
        assert_provider_contract(_provider(export=spec))


def test_a_watcher_under_another_name_is_rejected() -> None:
    """Threads land under the watcher's name and the descriptor is looked up by
    its own — disagreeing splits one source in half."""
    with pytest.raises(AssertionError, match="calls itself 'other'"):
        assert_provider_contract(_provider(watcher=lambda: _Watcher("other")))


def test_a_watcher_that_disagrees_with_its_own_discovery_is_rejected() -> None:
    """Setup shows one answer and the poll loop obeys the other."""
    with pytest.raises(AssertionError, match="discover.. says available"):
        assert_provider_contract(
            _provider(watcher=lambda: _Watcher(available=True, discovers=False))
        )


def test_an_absent_store_that_still_enumerates_files_is_rejected() -> None:
    with pytest.raises(AssertionError, match="unavailable store still enumerates"):
        assert_provider_contract(
            _provider(watcher=lambda: _Watcher(paths=[Path("/nope/a.jsonl")]))
        )


def test_store_items_naming_an_unlisted_file_is_rejected() -> None:
    """The capture-coverage check pairs the two; an item outside store_paths can
    never be reconciled against what the archive holds."""
    listed, stray = Path("/store/a.jsonl"), Path("/store/b.jsonl")
    with pytest.raises(AssertionError, match="store_paths.. does not"):
        assert_provider_contract(_provider(watcher=lambda: _Watcher(
            available=True, paths=[listed], items=[(stray, "s1")]
        )))


def test_reimport_helper_catches_an_importer_that_doubles(archive_home) -> None:
    """The failure the helper exists for: a session re-read on the next watcher
    pass lands twice. Driven with a real importer over a file that grows, which
    is exactly how a live transcript is re-offered."""
    from thread_archive._importers import import_session_incremental

    init_archive()
    f = archive_home / "growing.jsonl"
    turns = list(SESSION)

    def run_and_grow():
        write_jsonl(f, turns)
        result = import_session_incremental(f, "proj:grow")
        turns.append({"type": "user", "uuid": f"u{len(turns)}",
                      "timestamp": "2026-01-01T10:01:00Z", "sessionId": "s1",
                      "message": {"role": "user", "content": f"turn {len(turns)}"}})
        return result

    with pytest.raises(AssertionError, match="created 1 more event"):
        assert_reimport_adds_nothing(run_and_grow)


def test_reimport_helper_rejects_a_fixture_that_imports_nothing(archive_home) -> None:
    """A first run that creates nothing makes the second one prove nothing —
    the shape a passing-but-empty conformance test would take."""
    from thread_archive._importers import import_session_incremental

    init_archive()
    empty = archive_home / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(AssertionError, match="first import created no events"):
        assert_reimport_adds_nothing(lambda: import_session_incremental(empty, "proj:empty"))


def test_reimport_helper_requires_the_watermark_it_was_told_to_check(archive_home) -> None:
    """A source name nothing imported under has no watermark, which is what the
    check is for — an importer relying on dedup alone re-reads the whole store
    every pass."""
    from thread_archive._importers import import_session_incremental

    init_archive()
    f = archive_home / "sess.jsonl"
    write_jsonl(f, SESSION)
    with pytest.raises(AssertionError, match="no import_state row for 'myharness'"):
        assert_reimport_adds_nothing(
            lambda: import_session_incremental(f, "proj:s1"), source="myharness"
        )
