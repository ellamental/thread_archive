"""The known-archives registry: an archive becomes known by being opened."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thread_archive._ops import archives, load_runs


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway file and clear the per-process throttle
    memo so each test's registrations actually write."""
    path = tmp_path / "archives.json"
    monkeypatch.setenv("THREAD_ARCHIVE_REGISTRY", str(path))
    archives._last_registered.clear()
    return path


def test_archive_id_is_stable_and_path_derived(tmp_path):
    a = tmp_path / "one"
    assert archives.archive_id(a) == archives.archive_id(a)  # stable
    assert archives.archive_id(a) != archives.archive_id(tmp_path / "two")


def test_default_label_qualifies_the_generic_name(tmp_path):
    # A bare basename for a distinctive dir…
    assert archives.default_label(tmp_path / "swe-chat") == "swe-chat"
    # …but the generic '~/.thread/archive' name gets no parent qualifier,
    # while a differently-parented 'archive' is qualified so two don't collide.
    assert archives.default_label(Path.home() / ".thread" / "archive") == "archive"
    assert archives.default_label(Path("/data/projectX/archive")) == "projectX/archive"


def test_register_then_read(registry):
    home = Path("/data/arc-one")
    archives.register(home, force=True)
    entries = archives.read_registry()
    assert len(entries) == 1
    assert entries[0]["home"] == str(home)
    assert entries[0]["id"] == archives.archive_id(home)
    assert "first_seen" in entries[0] and "last_opened" in entries[0]


def test_merge_preserves_first_seen_and_does_not_clobber_a_custom_label():
    rows = archives.merge_entry([], Path("/a"), at="T1", label="Mine")
    rows = archives.merge_entry(rows, Path("/a"), at="T2")  # re-open, no label
    assert len(rows) == 1
    assert rows[0]["first_seen"] == "T1"     # first sighting kept
    assert rows[0]["last_opened"] == "T2"    # advanced
    assert rows[0]["label"] == "Mine"        # default doesn't overwrite a set label


def test_register_is_idempotent_on_the_same_home(registry):
    archives.register(Path("/a"), force=True)
    archives.register(Path("/a"), force=True)
    assert len(archives.read_registry()) == 1  # one entry, not two


def test_forget_removes_only_the_named_home(registry):
    archives.register(Path("/a"), force=True)
    archives.register(Path("/b"), force=True)
    assert archives.forget(Path("/a")) is True
    homes = [e["home"] for e in archives.read_registry()]
    assert homes == ["/b"]
    assert archives.forget(Path("/a")) is False  # already gone


def test_throttle_never_swallows_a_first_registration(registry):
    """The throttle skips a *re*-registration, never the first one.

    ``time.monotonic``'s epoch is unspecified — on Linux it counts from boot, so a
    just-booted box reports single-digit seconds. A throttle that reads "no entry"
    as "registered at 0.0" then drops every first registration until the box has
    been up longer than the interval. Asserted at three seconds of uptime, which a
    long-running dev machine can never reach on its own clock."""
    assert archives._throttled("/data/known", 3.0) is False  # never seen → register
    archives.register(Path("/data/known"))  # unforced, as the real open path calls it
    assert [e["home"] for e in archives.read_registry()] == ["/data/known"]

    # Only a real prior registration throttles, and only within the interval.
    archives._last_registered["/data/known"] = 1.0
    assert archives._throttled("/data/known", 3.0) is True
    assert archives._throttled("/data/known", 1.0 + archives._REGISTER_INTERVAL_S) is False


def test_disabled_registry_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "archives.json"
    monkeypatch.setenv("THREAD_ARCHIVE_REGISTRY", "0")
    archives._last_registered.clear()
    archives.register(tmp_path / "arc", force=True)
    assert not path.exists()
    assert archives.read_registry() == []


def test_describe_reads_live_load_state_and_existence(registry, tmp_path):
    home = tmp_path / "arc"
    home.mkdir()
    (home / "index.db").write_bytes(b"x" * 100)
    load_runs._write_atomic(
        load_runs.state_path(home),
        {"kind": "embed", "status": "ok", "pid": 1, "phases": []},
    )
    archives.register(home, force=True)
    described = archives.list_archives(active_home=home)
    assert len(described) == 1
    d = described[0]
    assert d["exists"] is True
    assert d["active"] is True
    assert d["index_bytes"] == 100
    assert d["load"]["kind"] == "embed"


def test_describe_carries_the_recent_load_history(registry, tmp_path):
    """The health view asks 'which archives are loading, which are loaded, and what
    did past loads cost' in one call — so each entry carries its own recent runs."""
    home = tmp_path / "arc"
    home.mkdir()
    with load_runs.load_run("import", home=home):
        pass
    with load_runs.load_run("embed", home=home):
        pass
    archives.register(home, force=True)

    entry = archives.list_archives()[0]
    assert [r["kind"] for r in entry["runs"]] == ["embed", "import"]  # newest first
    assert all(r["status"] == "ok" for r in entry["runs"])
    # Bounded: a ledger grows forever, a health render must not scale with it.
    assert archives.describe(entry, runs=1)["runs"] == entry["runs"][:1]
    assert archives.describe(entry, runs=0)["runs"] == []


def test_describe_of_a_missing_home_has_no_runs(registry, tmp_path):
    archives.register(tmp_path / "never-made", force=True)
    assert archives.list_archives()[0]["runs"] == []


def test_describe_marks_a_missing_home(registry, tmp_path):
    gone = tmp_path / "deleted"
    archives.register(gone, force=True)  # never created
    d = archives.list_archives()[0]
    assert d["exists"] is False
    assert d["active"] is False
    assert d["load"] == {}


def test_open_archive_registers_the_home(registry, tmp_path, monkeypatch):
    # The integration wiring: an archive becomes known by being opened.
    from thread_archive import _api as api

    home = tmp_path / "arc"
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(home))
    api.open_archive(str(home))
    homes = [e["home"] for e in archives.read_registry()]
    assert str(home) in homes


def test_set_role_by_label_id_and_home(registry):
    archives.register(Path("/data/swe-chat"), force=True)
    archives.register(Path("/data/other"), force=True)

    entry = archives.set_role("swe-chat", "benchmark")  # by label
    assert entry["role"] == "benchmark"
    aid = archives.archive_id(Path("/data/other"))
    assert archives.set_role(aid, "snapshot")["role"] == "snapshot"  # by id
    assert archives.set_role("/data/other", "live")["role"] == "live"  # by home path

    roles = {e["label"]: e.get("role") for e in archives.read_registry()}
    assert roles == {"swe-chat": "benchmark", "other": "live"}


def test_set_role_survives_reregistration_and_clears(registry):
    home = Path("/data/arc")
    archives.register(home, force=True)
    archives.set_role("arc", "benchmark")
    archives._last_registered.clear()
    archives.register(home, force=True)  # a re-open must not shed the role
    assert archives.read_registry()[0]["role"] == "benchmark"

    cleared = archives.set_role("arc", None)
    assert "role" not in cleared
    assert "role" not in archives.read_registry()[0]


def test_set_role_rejects_unknown_ambiguous_and_malformed(registry):
    archives.register(Path("/data/arc"), force=True)
    with pytest.raises(KeyError):
        archives.set_role("no-such-archive", "live")
    with pytest.raises(ValueError):
        archives.set_role("arc", "Not A Role!")
    # Two homes sharing a display label: the ref must refuse, not silently pick.
    path = archives.registry_path()
    data = json.loads(path.read_text())
    data["archives"] = archives.merge_entry(
        data["archives"], Path("/elsewhere/arc2"), at="T1", label="arc")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        archives.set_role("arc", "live")


def test_suppress_registration_scopes_the_kill_switch(registry, monkeypatch):
    with archives.suppress_registration():
        archives.register(Path("/data/scratch"), force=True)
        with pytest.raises(KeyError):  # role writes are off too
            archives.set_role("/data/scratch", "snapshot")
    assert archives.read_registry() == []  # nothing leaked into the registry
    # The prior override (this fixture's sandbox path) is restored on exit.
    archives.register(Path("/data/real"), force=True)
    assert [e["label"] for e in archives.read_registry()] == ["real"]


def test_list_is_newest_opened_first(registry):
    archives.register(Path("/a"), force=True)  # last_opened = now
    # Write a second entry opened far in the future, straight through the file.
    path = archives.registry_path()
    data = json.loads(path.read_text())
    data["archives"] = archives.merge_entry(
        data["archives"], Path("/b"), at="2099-01-01T00:00:00")
    path.write_text(json.dumps(data))
    homes = [a["home"] for a in archives.list_archives()]
    assert homes[0] == "/b"  # 2099 sorts ahead of the just-now /a
