"""The shared plumbing every corpus-building harness runs through.

The guard is the one with teeth: these harnesses ``rmtree`` the home they build
into, so a wrong answer here is data loss rather than a wrong number. The rest —
arm pinning, cache keying — is what makes two harnesses' ``lexical`` rows and two
runs' cached corpora mean the same thing.
"""

from __future__ import annotations

import pytest

from search_lab import eval_home


def test_guard_refuses_the_archive_home_itself(tmp_path) -> None:
    archive = tmp_path / "archive"

    with pytest.raises(SystemExit, match="overlaps the archive home"):
        eval_home.guard_home(archive, protected=[archive])


def test_guard_refuses_a_parent_of_the_archive_home(tmp_path) -> None:
    # The failure equality alone misses: ~/.thread is not ~/.thread/archive, and
    # wiping it destroys the archive anyway.
    archive = tmp_path / "thread" / "archive"

    with pytest.raises(SystemExit, match="overlaps"):
        eval_home.guard_home(tmp_path / "thread", protected=[archive])


def test_guard_refuses_a_home_inside_the_archive_home(tmp_path) -> None:
    archive = tmp_path / "archive"

    with pytest.raises(SystemExit, match="overlaps"):
        eval_home.guard_home(archive / "bench", protected=[archive])


def test_guard_returns_the_resolved_path_for_a_home_elsewhere(tmp_path) -> None:
    got = eval_home.guard_home(tmp_path / "bench" / ".." / "bench" / "scifact",
                               protected=[tmp_path / "archive"])
    assert got == (tmp_path / "bench" / "scifact").resolve()


def test_protected_homes_covers_the_default_and_the_one_in_the_environment(
        tmp_path, monkeypatch) -> None:
    # A session pointed at a snapshot is pointed at a fixture that took hours to
    # build; the default home is not the only thing worth keeping. Read at call
    # time, because a harness pins its own home into the environment moments later.
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path / "snap"))
    assert eval_home.protected_homes() == [
        eval_home.DEFAULT_ARCHIVE_HOME.expanduser().resolve(),
        (tmp_path / "snap").resolve()]

    monkeypatch.delenv("THREAD_ARCHIVE_HOME")
    assert eval_home.protected_homes() == [
        eval_home.DEFAULT_ARCHIVE_HOME.expanduser().resolve()]


def test_guard_defends_the_real_archive_home_by_default(monkeypatch) -> None:
    # The default path, with nothing injected: the live archive and its parent are
    # both refused. This is the call every harness actually makes.
    monkeypatch.delenv("THREAD_ARCHIVE_HOME", raising=False)
    real = eval_home.DEFAULT_ARCHIVE_HOME

    with pytest.raises(SystemExit, match="overlaps"):
        eval_home.guard_home(real)
    with pytest.raises(SystemExit, match="overlaps"):
        eval_home.guard_home(real.parent)


def test_pin_arms_stands_coherence_down_for_a_lexical_run(monkeypatch) -> None:
    # Coherence reads event_vectors; with embeddings off that table is never
    # populated, so leaving it on measures a stack with a swallowed exception in it.
    for var in ("THREAD_ARCHIVE_EMBED", "THREAD_ARCHIVE_COHERENCE",
                "THREAD_ARCHIVE_NO_THROTTLE"):
        monkeypatch.delenv(var, raising=False)

    eval_home.pin_arms(vectors=False)
    import os

    assert os.environ["THREAD_ARCHIVE_EMBED"] == "off"
    assert os.environ["THREAD_ARCHIVE_COHERENCE"] == "off"
    assert os.environ["THREAD_ARCHIVE_NO_THROTTLE"] == "1"


def test_pin_arms_leaves_coherence_alone_when_the_vector_arm_is_on(monkeypatch) -> None:
    for var in ("THREAD_ARCHIVE_EMBED", "THREAD_ARCHIVE_COHERENCE"):
        monkeypatch.delenv(var, raising=False)

    eval_home.pin_arms(vectors=True)
    import os

    assert os.environ["THREAD_ARCHIVE_EMBED"] == "on"
    assert "THREAD_ARCHIVE_COHERENCE" not in os.environ


def test_arm_labels_name_the_configuration_that_ran() -> None:
    assert eval_home.arm_labels(vectors=False) == ["lexical"]
    assert eval_home.arm_labels(vectors=True) == ["lexical", "vectors"]


def test_marker_stale_catches_a_capped_build_read_back_as_the_full_corpus() -> None:
    full = {"dataset": "scifact", "max_docs": None}
    assert not eval_home.marker_stale({"dataset": "scifact", "max_docs": None}, full)
    assert eval_home.marker_stale({"dataset": "scifact", "max_docs": 100}, full)
    assert eval_home.marker_stale({"dataset": "nfcorpus", "max_docs": None}, full)
    assert eval_home.marker_stale(None, full)


def test_marker_stale_does_not_invalidate_a_build_made_before_a_key_existed() -> None:
    # An older marker lacking the key compares as None, which is what an uncapped
    # run asks for — tightening the rule must not force a re-embed of a full corpus.
    assert not eval_home.marker_stale(
        {"dataset": "scifact", "corpus_docs": 5183}, {"dataset": "scifact", "max_docs": None})


def test_home_name_gives_a_capped_corpus_its_own_home() -> None:
    assert eval_home.home_name("scifact", max_docs=None) == "scifact"
    assert eval_home.home_name("scifact", max_docs=0) == "scifact"
    assert eval_home.home_name("scifact", max_docs=200) == "scifact-max200"
