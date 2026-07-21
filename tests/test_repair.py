"""``archive fix-import``: the scaffold, the activation gate, pinning,
retirement, and the ledger-driven re-import.

The design under test: the scaffold decides where everything lands (whoever
writes the fix only fills in parse logic), activation is deterministic (tests
green → enabled → re-import; nothing anyone claims about the fix matters), and
patches are temporary by default (retired by the next core release) unless
pinned.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from thread_archive import __version__, _repair
from thread_archive._config import load_config, save_config
from thread_archive._repair.activate import ActivationError
from thread_archive._repair.ledger import LEDGER_FILE as PATCH_LOG
from thread_archive._repair.retire import retire_patches
from thread_archive._repair.scaffold import plugin_dir, scaffold

from .helpers import cc_assistant, cc_user, write_jsonl


def _patch_log(archive_home) -> list[dict]:
    path = archive_home / PATCH_LOG
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


class _Green:
    returncode = 0
    stdout = "all green"
    stderr = ""


class _Red:
    returncode = 1
    stdout = "F fail\n1 failed"
    stderr = ""


# ── scaffold ─────────────────────────────────────────────────────────────────


def test_scaffold_generates_the_full_layout(archive_home):
    target = scaffold("codex")
    assert target == plugin_dir("codex") == archive_home / "plugins" / "codex"
    module = (target / "patch_codex.py").read_text()
    compile(module, "patch_codex.py", "exec")
    assert 'builtin("codex")' in module
    tests = (target / "test_patch.py").read_text()
    compile(tests, "test_patch.py", "exec")
    assert "test_watermark_reset_reimport_creates_no_duplicates" in tests
    assert "from patch_codex import BASE, PROVIDER" in tests
    assert "provider.testing" in (target / "conftest.py").read_text()
    assert (target / "fixtures").is_dir()
    assert "codex" in (target / "evidence.md").read_text()
    assert (target / "quirks.md").exists()  # falls back to the default doc

    entry = load_config()["providers"]["codex"]
    assert entry["module"] == "patch_codex:PROVIDER"
    assert entry["path"] == str(target)
    assert entry["enabled"] is False  # activation is the only enable path
    assert entry["patch"]["built_against"] == __version__
    assert entry["patch"]["pinned"] is False

    descriptor = json.loads((target / "patch.json").read_text())
    assert descriptor["provider"] == "codex"
    assert descriptor["built_against"] == __version__

    events = _patch_log(archive_home)
    assert [e["event"] for e in events] == ["scaffolded"]


def test_scaffold_uses_provider_quirks_when_packaged(archive_home):
    target = scaffold("claude-code")
    quirks = (target / "quirks.md").read_text()
    assert "Claude Code format quirks" in quirks
    assert "compact_boundary" in quirks
    # cowork reuses the claude-code parser → same quirks doc via parser_id
    cowork = scaffold("cowork")
    assert "Claude Code format quirks" in (cowork / "quirks.md").read_text()


def test_scaffold_refresh_preserves_fix_work(archive_home):
    target = scaffold("codex")
    fix = "# my in-progress fix\nfrom dataclasses import replace\n"
    (target / "patch_codex.py").write_text(fix)
    (target / "test_patch.py").write_text("# my extended tests\n")
    scaffold("codex")
    assert (target / "patch_codex.py").read_text() == fix
    assert (target / "test_patch.py").read_text() == "# my extended tests\n"
    # evidence regenerates every run
    assert "codex" in (target / "evidence.md").read_text()


def test_scaffold_refresh_rebases_onto_current_core(archive_home):
    scaffold("codex")
    cfg = load_config()
    cfg["providers"]["codex"]["patch"].update(
        built_against="0.0.1", retired={"at": "x", "by": "v0.0.2"}, pinned=True
    )
    save_config(cfg)
    scaffold("codex")
    patch = load_config()["providers"]["codex"]["patch"]
    assert patch["built_against"] == __version__  # a re-fix targets the running core
    assert "retired" not in patch  # the retirement note belongs to the old fix
    assert patch["pinned"] is True  # the user's pin survives


def test_scaffold_refuses_unknown_and_mechanism_providers(archive_home):
    with pytest.raises(ValueError, match="unknown provider"):
        scaffold("not-a-provider")
    with pytest.raises(ValueError, match="machinery"):
        scaffold("export-drop")


def test_scaffold_collects_ledgered_samples_first(archive_home, tmp_path):
    """Sample collection maps skip-ledger source_ids to store files through the
    watcher and copies those before fresh files, with the manifest naming both.
    The store-bearing codex comes in through the sanctioned plugin path — a
    config-declared override, exactly how a real out-of-tree provider would."""
    from thread_archive._providers import reset

    store = tmp_path / "codex-store"
    store.mkdir()
    (store / "s-led.jsonl").write_text('{"type":"x"}\n')
    (store / "s-fresh.jsonl").write_text('{"type":"y"}\n')
    now = datetime.now(timezone.utc).isoformat()
    (archive_home / "capture-skips.jsonl").write_text(
        json.dumps({"at": now, "source": "codex", "source_id": "s-led",
                    "lines_skipped": 1, "lines_total": 1, "reason": "empty_import_discarded"})
        + "\n"
    )

    pkg = tmp_path / "plugin-src"
    pkg.mkdir()
    (pkg / "codex_store_plugin.py").write_text(
        "from dataclasses import replace\n"
        "from pathlib import Path\n"
        "from thread_archive.provider import RglobWatcher, builtin\n"
        f"STORE = Path({str(store)!r})\n"
        "def _no_import(path, source_id):\n"
        "    raise AssertionError('sample collection must not import')\n"
        "PROVIDER = replace(builtin('codex'), watcher=lambda: RglobWatcher(\n"
        "    STORE, _no_import, lambda f: f.stem, name='codex'))\n"
    )
    save_config({"providers": {"codex": {
        "module": "codex_store_plugin:PROVIDER", "path": str(pkg)}}})
    reset()
    try:
        target = scaffold("codex")
    finally:
        reset()
    manifest = json.loads((target / "samples" / "manifest.json").read_text())
    by_id = {info["source_id"]: info for info in manifest.values()}
    assert by_id["s-led"]["ledgered"] is True
    assert by_id["s-fresh"]["ledgered"] is False
    for name in manifest:
        assert (target / "samples" / name).exists()


def test_scaffold_survives_a_broken_watcher(archive_home, tmp_path):
    """A watcher that blows up IS the evidence (possibly the drift itself):
    sample collection notes it and the scaffold still lands, because the repair
    flow exists precisely for providers in a broken state."""
    from thread_archive._providers import reset

    pkg = tmp_path / "plugin-src"
    pkg.mkdir()
    (pkg / "codex_broken_watcher.py").write_text(
        "from dataclasses import replace\n"
        "from thread_archive.provider import builtin\n"
        "def _boom():\n"
        "    raise RuntimeError('store walk exploded')\n"
        "PROVIDER = replace(builtin('codex'), watcher=_boom)\n"
    )
    save_config({"providers": {"codex": {
        "module": "codex_broken_watcher:PROVIDER", "path": str(pkg)}}})
    reset()
    try:
        target = scaffold("codex")
    finally:
        reset()
    assert not (target / "samples" / "manifest.json").exists()
    assert "watcher failed to enumerate" in (target / "evidence.md").read_text()


def test_scaffold_evidence_lists_drift_snapshot_generations(archive_home):
    gen = archive_home / "dumps" / "drift" / "codex" / "20260101T000000Z"
    gen.mkdir(parents=True)
    target = scaffold("codex")
    assert str(gen) in (target / "evidence.md").read_text()


# ── activation ───────────────────────────────────────────────────────────────


def test_activate_refuses_without_a_scaffold(archive_home):
    with pytest.raises(ActivationError, match="no fix-import patch"):
        _repair.activate("codex")


def test_activate_gates_on_red_tests(archive_home):
    scaffold("codex")
    with pytest.raises(ActivationError, match="red"):
        _repair.activate("codex", run_tests=lambda d: _Red())
    assert load_config()["providers"]["codex"]["enabled"] is False


def test_activate_refuses_a_broken_module(archive_home):
    target = scaffold("codex")
    (target / "patch_codex.py").write_text("this is not python(\n")
    with pytest.raises(ActivationError, match="failed to load"):
        _repair.activate("codex", run_tests=lambda d: _Green())


def test_activate_refuses_a_renamed_override(archive_home):
    target = scaffold("codex")
    (target / "patch_codex.py").write_text(
        "from dataclasses import replace\n"
        "from thread_archive.provider import builtin\n"
        'BASE = builtin("codex")\n'
        'PROVIDER = replace(BASE, name="not-codex")\n'
    )
    with pytest.raises(ActivationError, match="keep the built-in's name"):
        _repair.activate("codex", run_tests=lambda d: _Green())


def test_activate_enables_override_and_registry_sees_it(archive_home):
    from thread_archive._providers import get, reset

    scaffold("codex")
    summary = _repair.activate("codex", run_tests=lambda d: _Green())
    assert summary["activated"] is True
    entry = load_config()["providers"]["codex"]
    assert entry["enabled"] is True
    assert entry["patch"]["activated_at"]
    # discovery now loads the override in place of the builtin
    try:
        assert get("codex") is not None
        assert get("codex").name == "codex"
    finally:
        reset()
    events = [e["event"] for e in _patch_log(archive_home)]
    assert events == ["scaffolded", "activated"]
    # nothing ledgered → the re-import is a clean zero, not a crash
    assert summary["reimport"]["watermarks_reset"] == 0


def test_activate_reloads_iterated_module(archive_home):
    """The agent iterates on the module between activation attempts — a stale
    import must not win."""
    target = scaffold("codex")
    _repair.activate("codex", run_tests=lambda d: _Green())
    (target / "patch_codex.py").write_text(
        "from dataclasses import replace\n"
        "from thread_archive.provider import builtin\n"
        'BASE = builtin("codex")\n'
        'PROVIDER = replace(BASE, name="drifted")\n'
    )
    with pytest.raises(ActivationError, match="keep the built-in's name"):
        _repair.activate("codex", run_tests=lambda d: _Green())


def test_activate_refuses_a_malformed_module_reference(archive_home):
    scaffold("codex")
    cfg = load_config()
    cfg["providers"]["codex"]["module"] = "no-colon-here"
    save_config(cfg)
    with pytest.raises(ActivationError, match="malformed module reference"):
        _repair.activate("codex", run_tests=lambda d: _Green())


CODEX_FIXTURE = [
    {"type": "session_meta", "payload": {"id": "fix", "cwd": "/p", "model": "gpt-5"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"type": "user_message", "message": "hello fix", "turn_id": "t1"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:05Z",
     "payload": {"type": "agent_message", "message": "hi from the fix"}},
]


@pytest.mark.integration
def test_activation_runs_the_scaffolds_real_suite_end_to_end(archive_home):
    """No injected runner: activation shells out to real pytest over the
    generated ``test_patch.py``. A bare scaffold is *refused* — the generated
    suite demands fixtures, so an agent can't activate an empty patch — and
    with an obfuscated fixture in place the same flow goes green and enables
    the override."""
    from thread_archive._providers import reset

    target = scaffold("codex")
    with pytest.raises(ActivationError, match="red"):
        _repair.activate("codex", reimport=False)

    (target / "fixtures" / "session.jsonl").write_text(
        "\n".join(json.dumps(ln) for ln in CODEX_FIXTURE) + "\n", encoding="utf-8")
    try:
        summary = _repair.activate("codex", reimport=False)
    finally:
        reset()
    assert summary["activated"] is True
    assert load_config()["providers"]["codex"]["enabled"] is True


# ── pinning + retirement ─────────────────────────────────────────────────────


def test_pin_and_unpin(archive_home):
    scaffold("codex")
    _repair.set_pinned("codex", True)
    assert load_config()["providers"]["codex"]["patch"]["pinned"] is True
    _repair.set_pinned("codex", False)
    assert load_config()["providers"]["codex"]["patch"]["pinned"] is False
    events = [e["event"] for e in _patch_log(archive_home)]
    assert events == ["scaffolded", "pinned", "unpinned"]


def _seed_patch(name: str, *, built: str, enabled: bool = True, pinned: bool = False):
    cfg = load_config()
    cfg.setdefault("providers", {})[name] = {
        "module": f"patch_{name}:PROVIDER",
        "path": f"/plugins/{name}",
        "enabled": enabled,
        "patch": {"built_against": built, "pinned": pinned},
    }
    save_config(cfg)


def test_retire_disables_unpinned_patches_built_against_older_core(archive_home):
    _seed_patch("codex", built="0.0.4")
    _seed_patch("grok", built="0.0.5")
    retired = retire_patches(target="v0.0.5")
    assert retired == ["codex"]
    cfg = load_config()
    assert cfg["providers"]["codex"]["enabled"] is False
    assert cfg["providers"]["codex"]["patch"]["retired"]["by"] == "v0.0.5"
    assert cfg["providers"]["grok"]["enabled"] is True  # built against the target
    events = _patch_log(archive_home)
    assert [e["event"] for e in events] == ["retired"]
    assert events[0]["provider"] == "codex"


def test_retire_spares_pinned_and_hand_installed_plugins(archive_home):
    _seed_patch("codex", built="0.0.1", pinned=True)
    cfg = load_config()
    cfg["providers"]["myharness"] = {"module": "m:P", "path": "/x", "enabled": True}
    save_config(cfg)
    assert retire_patches(target="v9.9.9") == []
    cfg = load_config()
    assert cfg["providers"]["codex"]["enabled"] is True  # pinned survives
    assert cfg["providers"]["myharness"]["enabled"] is True  # no patch metadata


def test_retire_leaves_unparseable_versions_alone(archive_home):
    _seed_patch("codex", built="not-a-version")
    assert retire_patches(target="v0.0.5") == []
    assert load_config()["providers"]["codex"]["enabled"] is True
    assert retire_patches(target="garbage") == []


def test_retire_skips_non_dict_and_already_disabled_entries(archive_home):
    cfg = load_config()
    cfg["providers"] = {"weird": "just-a-string"}
    save_config(cfg)
    _seed_patch("codex", built="0.0.1", enabled=False)
    assert retire_patches(target="v9.9.9") == []  # nothing active to retire
    assert load_config()["providers"]["codex"]["enabled"] is False


def test_patch_ledger_write_failure_is_advisory(archive_home):
    """The ledger records history; it must never take the repair verb down."""
    from thread_archive._repair.ledger import record_patch_event

    (archive_home / PATCH_LOG).mkdir()  # append opens now raise IsADirectoryError
    record_patch_event("scaffolded", "codex")  # must not raise


# ── re-import ────────────────────────────────────────────────────────────────


def test_reimport_resets_ledgered_watermarks(archive_home, tmp_path):
    from sqlalchemy import select

    from thread_archive import _api as ta
    from thread_archive._importers import import_session_incremental
    from thread_archive._store import ImportState, get_session

    f = tmp_path / "led.jsonl"
    write_jsonl(f, [cc_user("led"), cc_assistant("led")])
    ta.open_archive()
    import_session_incremental(f, "proj:led")
    now = datetime.now(timezone.utc).isoformat()
    (archive_home / "capture-skips.jsonl").write_text(
        json.dumps({"at": now, "source": "claude-code", "source_id": "proj:led",
                    "reason": "empty_import_discarded"}) + "\n"
    )
    summary = _repair.reimport_source("claude-code")
    assert summary["source_ids"] == 1
    assert summary["watermarks_reset"] == 1
    with get_session() as s:
        gone = s.execute(
            select(ImportState).filter_by(source="claude-code", source_id="proj:led")
        ).first()
    assert gone is None


def test_reimport_replays_pruned_snapshot_copies(archive_home):
    """A drift-quarantine copy whose original the provider pruned re-imports
    through the (fixed) parser — the unconditional-preservation promise paying
    off."""
    from sqlalchemy import text

    from thread_archive import _api as ta
    from thread_archive._store import get_session

    ta.open_archive()
    gen = archive_home / "dumps" / "drift" / "claude-code" / "20260701T000000Z"
    gen.mkdir(parents=True)
    stored = gen / "snap.jsonl"
    write_jsonl(stored, [cc_user("snap"), cc_assistant("snap")])
    (gen / "manifest.json").write_text(json.dumps({
        "source": "claude-code",
        "files": [{"path": str(archive_home / "gone" / "snap.jsonl"),
                   "stored": "snap.jsonl", "source_id": "proj:snap"}],
    }))
    summary = _repair.reimport_source("claude-code")
    assert summary["snapshot_replayed"] == 1
    assert summary["snapshot_events"] > 0
    with get_session() as s:
        n = s.execute(text(
            "SELECT count(*) FROM threads WHERE source_id = 'proj:snap'"
        )).scalar()
    assert n == 1


def test_reimport_skips_snapshot_copies_whose_originals_live(archive_home, tmp_path):
    from thread_archive import _api as ta

    ta.open_archive()
    original = tmp_path / "orig.jsonl"
    write_jsonl(original, [cc_user("live")])
    gen = archive_home / "dumps" / "drift" / "claude-code" / "20260701T000000Z"
    gen.mkdir(parents=True)
    write_jsonl(gen / "orig.jsonl", [cc_user("live")])
    (gen / "manifest.json").write_text(json.dumps({
        "files": [{"path": str(original), "stored": "orig.jsonl",
                   "source_id": "proj:live"}],
    }))
    summary = _repair.reimport_source("claude-code")
    assert summary["snapshot_replayed"] == 0  # the live poll owns it


def test_reimport_unknown_provider_raises(archive_home):
    with pytest.raises(ValueError, match="unknown provider"):
        _repair.reimport_source("not-a-provider")


def test_reimport_tolerates_ledger_noise(archive_home):
    """The skip ledger is an append-only crash-tolerant log: blank lines, torn
    JSON, other providers' records, naive timestamps, and records with no
    source_id all coexist with the one usable record."""
    from thread_archive import _api as ta

    ta.open_archive()
    now = datetime.now(timezone.utc)
    (archive_home / "capture-skips.jsonl").write_text("\n".join([
        "",
        '{"torn": ',
        json.dumps({"at": now.isoformat(), "source": "grok", "source_id": "other",
                    "reason": "r"}),
        json.dumps({"at": now.replace(tzinfo=None).isoformat(), "source": "claude-code",
                    "source_id": "proj:naive", "reason": "r"}),  # naive → read as UTC
        json.dumps({"at": now.isoformat(), "source": "claude-code", "reason": "r"}),
        json.dumps({"at": "not-a-date", "source": "claude-code", "source_id": "x",
                    "reason": "r"}),
    ]) + "\n", encoding="utf-8")
    summary = _repair.reimport_source("claude-code")
    assert summary["source_ids"] == 1  # only the well-formed claude-code record


def test_reimport_snapshot_replay_survives_bad_generations(archive_home):
    """A torn manifest or a vanished stored copy must not stop replay of the
    generations that are intact."""
    from thread_archive import _api as ta

    ta.open_archive()
    base = archive_home / "dumps" / "drift" / "claude-code"
    bad = base / "20260101T000000Z"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text('{"torn": ', encoding="utf-8")
    gone = base / "20260102T000000Z"
    gone.mkdir()
    (gone / "manifest.json").write_text(json.dumps({
        "files": [{"path": "/nowhere/orig.jsonl", "stored": "vanished.jsonl",
                   "source_id": "proj:v"},
                  {"stored": "no-source-id.jsonl"}],
    }), encoding="utf-8")
    good = base / "20260103T000000Z"
    good.mkdir()
    write_jsonl(good / "snap.jsonl", [cc_user("ok"), cc_assistant("ok")])
    (good / "manifest.json").write_text(json.dumps({
        "files": [{"path": str(archive_home / "gone" / "snap.jsonl"),
                   "stored": "snap.jsonl", "source_id": "proj:ok"}],
    }), encoding="utf-8")
    summary = _repair.reimport_source("claude-code")
    assert summary["snapshot_replayed"] == 1
    assert summary["snapshot_errors"] == []


def test_reimport_collects_importer_failures_per_copy(archive_home, tmp_path):
    """One unreadable stored copy is an error entry, not the end of recovery.
    The failing importer comes in through the sanctioned plugin path — a
    config-declared override, the same seam a real fix-import patch uses."""
    from thread_archive import _api as ta
    from thread_archive._providers import reset

    pkg = tmp_path / "plugin-src"
    pkg.mkdir()
    (pkg / "cc_broken_importer.py").write_text(
        "from dataclasses import replace\n"
        "from thread_archive.provider import builtin\n"
        "def _boom(path, source_id):\n"
        "    raise RuntimeError('stored copy unreadable')\n"
        "PROVIDER = replace(builtin('claude-code'), importer=_boom, watcher=None)\n"
    )
    save_config({"providers": {"claude-code": {
        "module": "cc_broken_importer:PROVIDER", "path": str(pkg)}}})
    reset()
    try:
        ta.open_archive()
        gen = archive_home / "dumps" / "drift" / "claude-code" / "20260101T000000Z"
        gen.mkdir(parents=True)
        write_jsonl(gen / "snap.jsonl", [cc_user("x")])
        (gen / "manifest.json").write_text(json.dumps({
            "files": [{"path": str(archive_home / "gone.jsonl"), "stored": "snap.jsonl",
                       "source_id": "proj:x"}],
        }), encoding="utf-8")
        summary = _repair.reimport_source("claude-code")
    finally:
        reset()
    assert summary["snapshot_replayed"] == 0
    assert summary["snapshot_errors"] == ["proj:x: stored copy unreadable"]


# ── the protocol ─────────────────────────────────────────────────────────────


def test_scaffold_carries_the_repair_protocol(archive_home):
    """The fix happens in the scaffold, so its instructions ship in it —
    readable by the user or handed to whatever agent they point at it."""
    target = scaffold("codex")
    protocol = (target / "PROTOCOL.md").read_text()
    assert "obfuscated" in protocol  # the fixture-privacy rule rides every scaffold
    assert "archive fix-import codex --activate" in protocol


# ── CLI dispatch ─────────────────────────────────────────────────────────────


def test_fix_import_cli_dispatches(monkeypatch, capsys):
    from thread_archive import cli

    seen = {}
    monkeypatch.setattr(
        _repair, "scaffold", lambda p, h: seen.update(p=p, h=h) or plugin_dir(p, h)
    )
    assert cli.main(["fix-import", "codex", "--home", "/h"]) == 0
    assert seen == {"p": "codex", "h": "/h"}
    assert "PROTOCOL.md" in capsys.readouterr().out  # points at the next step

    monkeypatch.setattr(
        _repair, "activate",
        lambda p, h, reimport: seen.update(act=(p, h, reimport)) or {"activated": True},
    )
    assert cli.main(["fix-import", "codex", "--activate", "--no-reimport"]) == 0
    assert seen["act"] == ("codex", None, False)
    assert "patch active" in capsys.readouterr().out

    monkeypatch.setattr(_repair, "set_pinned", lambda p, pin, h: seen.update(pin=(p, pin)))
    assert cli.main(["fix-import", "codex", "--pin"]) == 0
    assert seen["pin"] == ("codex", True)
    assert cli.main(["fix-import", "codex", "--unpin"]) == 0
    assert seen["pin"] == ("codex", False)


def test_fix_import_cli_surfaces_refusals(monkeypatch, capsys):
    from thread_archive import cli

    def _refuse(p, h, reimport):
        raise ActivationError("the patch's test suite is red — activation refused.")

    monkeypatch.setattr(_repair, "activate", _refuse)
    assert cli.main(["fix-import", "codex", "--activate"]) == 1
    assert "red" in capsys.readouterr().out
