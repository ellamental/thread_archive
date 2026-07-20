"""``archive fix-import``: the scaffold, the activation gate, pinning,
retirement, the ledger-driven re-import, and the repair-agent spawn.

The design under test: the scaffold decides where everything lands (a thin
model only fills in parse logic), activation is deterministic (tests green →
enabled → re-import; nothing the agent claims matters), and patches are
temporary by default (retired by the next core release) unless pinned.
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


# ── the spawn ────────────────────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, argv, on_wait=None, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = 0
        self._on_wait = on_wait

    def wait(self, timeout=None):
        if self._on_wait:
            self._on_wait()
        return 0


class _SpawnRecorder:
    """Captures every Popen the repair driver makes; ``on_wait`` simulates what
    the agent did before exiting (e.g. activating the patch)."""

    def __init__(self) -> None:
        self.calls: list[_FakeProc] = []
        self.on_wait = None

    def __call__(self, argv, **kwargs):
        proc = _FakeProc(argv, on_wait=self.on_wait, **kwargs)
        self.calls.append(proc)
        return proc

    def __len__(self) -> int:
        return len(self.calls)


@pytest.fixture()
def spawned(monkeypatch):
    recorder = _SpawnRecorder()
    monkeypatch.setattr(_repair.subprocess, "Popen", recorder)
    return recorder


def test_run_spawns_bounded_headless_claude(archive_home, spawned):
    rc = _repair.run("codex", claude="/fake/claude")
    assert rc == 1  # nothing activated the patch
    (proc,) = spawned.calls
    assert proc.argv[0] == "/fake/claude"
    assert "--print" in proc.argv
    # Scoped, not bypassed: edits auto-approve only inside the scaffold cwd,
    # and the shell surface is exactly the protocol's commands.
    assert proc.argv[proc.argv.index("--permission-mode") + 1] == "acceptEdits"
    allowed = proc.argv[proc.argv.index("--allowedTools") + 1]
    assert set(allowed.split(",")) == {
        "Bash(python:*)", "Bash(python3:*)", "Bash(pytest:*)", "Bash(archive:*)",
    }
    # The venv's bin dir leads PATH so the allowlist's bare names resolve here.
    import os
    import sys
    from pathlib import Path as _Path

    spawn_path = proc.kwargs["env"]["PATH"]
    assert spawn_path.split(os.pathsep)[0] == str(_Path(sys.executable).parent)
    assert proc.argv[proc.argv.index("--model") + 1] == "opus"
    assert proc.argv[proc.argv.index("--effort") + 1] == "xhigh"
    assert "--strict-mcp-config" in proc.argv
    mcp_path = proc.argv[proc.argv.index("--mcp-config") + 1]
    assert json.loads(open(mcp_path).read()) == {"mcpServers": {}}  # no servers leak in
    prompt = proc.argv[-1]
    assert "codex" in prompt and "fix-import codex --activate" in prompt
    assert "obfuscated" in prompt  # the fixture-privacy rule rides every run
    assert proc.kwargs["cwd"] == str(plugin_dir("codex"))
    assert proc.kwargs["start_new_session"] is True


def test_run_reports_success_when_agent_activates(archive_home, spawned):
    def _activate_behind_the_scenes():
        cfg = load_config()
        cfg["providers"]["codex"]["enabled"] = True
        save_config(cfg)

    spawned.on_wait = _activate_behind_the_scenes
    assert _repair.run("codex", claude="/fake/claude") == 0


def test_run_without_claude_leaves_scaffold_and_fails(archive_home, spawned, monkeypatch):
    monkeypatch.setattr(_repair, "resolve_claude", lambda: None)
    assert _repair.run("codex") == 1
    assert not spawned  # no CLI → no spawn
    assert plugin_dir("codex").exists()  # scaffold still ready for by-hand work


def test_repair_settings_config_and_fallbacks(archive_home):
    assert _repair.repair_settings() == ("opus", "xhigh")
    save_config({"repair": {"model": "sonnet", "effort": None}})
    assert _repair.repair_settings() == ("sonnet", "")  # explicit null omits the flag
    save_config({"repair": {"model": 7, "effort": ["x"]}})
    assert _repair.repair_settings() == ("opus", "xhigh")  # bad types fall back


# ── CLI dispatch ─────────────────────────────────────────────────────────────


def test_fix_import_cli_dispatches(monkeypatch, capsys):
    from thread_archive import cli

    seen = {}
    monkeypatch.setattr(
        _repair, "run", lambda p, h, timeout: seen.update(p=p, h=h, t=timeout) or 0
    )
    assert cli.main(["fix-import", "codex", "--home", "/h", "--timeout", "60"]) == 0
    assert seen == {"p": "codex", "h": "/h", "t": 60}

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
