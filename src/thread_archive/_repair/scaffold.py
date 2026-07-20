"""The fix-import scaffold: everything around the fix, pre-generated.

"Lands in the right places" is a property of the scaffold, not the model. The
scaffold decides where every artifact goes — the override module, its tests,
the fixtures dir, the patch descriptor, the config.json registration — so the
repair agent's entire job is parse logic inside one pre-wired file, exercised
by tests it did not write and cannot relocate. A thin model that fills in the
blank correctly produces a correct patch; a thin model that fills it in badly
produces a red test run, which activation refuses.

Layout under ``<home>/plugins/<provider>/``::

    patch_<provider>.py   the override module (generated once, never clobbered)
    test_patch.py         the verification harness (generated once)
    conftest.py           enables thread_archive.provider.testing
    fixtures/             agent-derived minimal fixtures (empty at scaffold)
    samples/              real drifted source files, collected from the ledgers
    evidence.md           what broke, per the ledgers/coverage (refreshed)
    quirks.md             per-provider format knowledge (refreshed from package)
    patch.json            the patch descriptor (mirrors config.json's entry)

The fix module and tests are generated only when absent — re-running
``archive fix-import`` refreshes evidence, samples, and quirks around an
in-progress fix without discarding it. Everything the scaffold writes stays
outside the archive's git clone: the self-updater's clean-tree requirement is
untouched by any number of patches.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import __version__
from .._config import load_config, resolve_paths, save_config
from .ledger import record_patch_event

logger = logging.getLogger(__name__)

PLUGINS_DIRNAME = "plugins"
# Sample-collection bounds: enough real drifted files to diagnose from, small
# enough that a scaffold never balloons the home. Truncation is logged and
# noted in evidence.md.
MAX_SAMPLE_FILES = 15
MAX_SAMPLE_BYTES = 50 * 1024 * 1024
# Beyond the ledgered files, the freshest store files ride along: line-stream
# drift can be invisible to the ledgers (a field the parser silently drops),
# and the newest sessions are where the current format actually is.
FRESH_SAMPLE_FILES = 5


def plugin_dir(provider_name: str, home: Optional[str] = None) -> Path:
    return resolve_paths(home).home / PLUGINS_DIRNAME / provider_name


def module_name(provider_name: str) -> str:
    return "patch_" + provider_name.replace("-", "_")


# ── generated file templates ─────────────────────────────────────────────────

_MODULE_TEMPLATE = '''"""Override patch for the {name} provider — scaffolded by `archive fix-import`.

Shadows the built-in {name} provider (declared under "providers" in the archive
home's config.json). Start from the built-in descriptor and replace only what
the fix changes. Import from thread_archive.provider / .parse — with one
sanctioned exception: a patch may reach into thread_archive internals it is
fixing (a patch is temporary by design; retirement on the next core release
bounds the exposure).
"""

from dataclasses import replace

from thread_archive.provider import builtin

BASE = builtin("{name}")

# ── the fix ──────────────────────────────────────────────────────────────────
# Smallest shape that covers the drift wins:
#
# 1. Ledger drift — validators flag new block types / fields / line kinds, but
#    content still parses. Extend the parser config; no code:
#
#      PROVIDER = replace(BASE, parser_config=BASE.parser_config.derive(
#          "{name}",
#          expected_block_types={"the_new_block_type"},
#          known_line_fields={"assistant": {"theNewField"}},
#      ))
#
#    (If BASE.parser_config is None this provider had no drift ledger; build a
#    thread_archive.provider.parse.ProviderConfig from scratch instead.)
#
# 2. Structural drift — the parser mis-reads changed structure. Subclass the
#    parser (or wrap the importer) and override the narrowest thing that fixes
#    the samples; then: PROVIDER = replace(BASE, parser=PatchedParser, ...).
#
# 3. Store drift — files or databases moved/renamed. Replace the watcher
#    factory: PROVIDER = replace(BASE, watcher=lambda: ...).
#
# PROVIDER.name must stay equal to BASE.name — the shared name is what makes
# this an override rather than a new source.

PROVIDER = replace(BASE)  # TODO: apply the fix
'''

_CONFTEST_TEMPLATE = '''"""Enables the archive's plugin test harness (isolated tmp archive)."""

pytest_plugins = ["thread_archive.provider.testing"]
'''

_TEST_HEADER = '''"""Verification harness for the {name} patch — green here is the exit bar.

Pre-wired by `archive fix-import`; activation re-runs it and refuses a red
suite. Add tests freely; never weaken or remove the generated ones. Fixtures
are yours to derive: minimal, obfuscated files under fixtures/, built from the
real drifted files in samples/ (structure and keys intact, free text replaced
with placeholder words — no personal content in a fixture).
"""

import json
from pathlib import Path

from thread_archive.provider.testing import init_archive

from {module} import BASE, PROVIDER

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_files():
    if not FIXTURES.is_dir():
        return []
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def test_patch_overrides_the_builtin():
    assert PROVIDER.name == BASE.name == "{name}"


def test_fixtures_exist():
    assert _fixture_files(), (
        "no fixtures yet — derive minimal, obfuscated fixtures from samples/ "
        "into fixtures/ first"
    )
'''

_TEST_IMPORT_LINE_STREAM = '''

def _import_all():
    total = 0
    for i, fixture in enumerate(_fixture_files()):
        result = PROVIDER.importer(fixture, f"fixture-{i}")
        total += result.events_created
    return total


def test_fixtures_import_events(archive_home):
    init_archive()
    assert _import_all() > 0, "fixtures imported no events — the fix isn't reading them"


def test_no_validation_drift_on_fixtures(archive_home):
    """The fixed parser must not still be tripping validators on the shapes it
    claims to fix — a finding here is the drift ledger saying the fix is
    incomplete."""
    init_archive()
    _import_all()
    ledger = archive_home / "validation-drift.jsonl"
    findings = []
    if ledger.exists():
        findings = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    assert not findings, f"the fixed parser still trips validators: {findings[:3]}"
'''

_TEST_IMPORT_DB_SCAN = '''

def _import_all():
    total = 0
    for fixture in _fixture_files():
        if fixture.suffix not in (".db", ".sqlite", ".vscdb"):
            continue
        result = PROVIDER.importer(fixture)
        total += result.events_created
    return total


def test_fixtures_import_events(archive_home):
    init_archive()
    assert _import_all() > 0, "fixtures imported no events — the fix isn't reading them"
'''

_TEST_DEDUP = '''

def test_watermark_reset_reimport_creates_no_duplicates(archive_home):
    """The activation flow deletes watermarks and re-imports everything through
    the fixed parser, trusting event-level dedup to keep already-understood
    content single. A fix that changes an event's dedup identity double-imports
    every event it touches — the one way a patch corrupts an archive instead of
    degrading it. Never weaken this test."""
    from sqlalchemy import delete, text

    from thread_archive.provider import ImportState, get_session

    init_archive()
    _import_all()
    with get_session() as s:
        before = s.execute(text("SELECT count(*) FROM events")).scalar()
        s.execute(delete(ImportState))
        s.commit()
    _import_all()
    with get_session() as s:
        after = s.execute(text("SELECT count(*) FROM events")).scalar()
    assert after == before, (
        f"re-import duplicated events ({before} -> {after}) — the fix changed "
        "dedup identity"
    )
'''

_TEST_OTHER_KIND = '''

def test_fixtures_import_events(archive_home):
    """This provider has no generic importer signature (kind={kind!r}) — wire
    the import call for its shape here (see quirks.md), keeping the structure
    of the line-stream harness: import every fixture, assert events landed,
    and keep the watermark-reset re-import dedup check."""
    init_archive()
    raise AssertionError(
        "wire the {kind!r}-kind import call for this provider's fixtures"
    )
'''


def _test_body(provider) -> str:
    name = provider.name
    mod = module_name(name)
    header = _TEST_HEADER.replace("{name}", name).replace("{module}", mod)
    if provider.kind == "line-stream":
        return header + _TEST_IMPORT_LINE_STREAM + _TEST_DEDUP
    if provider.kind == "db-scan":
        return header + _TEST_IMPORT_DB_SCAN + _TEST_DEDUP
    return header + _TEST_OTHER_KIND.replace("{kind!r}", repr(provider.kind))


# ── evidence + samples ───────────────────────────────────────────────────────


def _recent_ledger_records(
    filename: str, source_key: str, source: str, home: Optional[str], *, limit: int = 20
) -> list[dict]:
    try:
        lines = (resolve_paths(home).home / filename).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):  # newest last on disk → newest first here
        if len(out) >= limit:
            break
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get(source_key) == source:
            out.append(rec)
    return out


def _collect_samples(provider, target: Path, home: Optional[str]) -> dict:
    """Copy real drifted source files into ``samples/``: the ledgered ones
    first (they are the diagnosed failures), then the freshest store files
    (the current format lives there). Returns the manifest written."""
    from .reimport import _recent_source_ids

    target.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    if provider.watcher is None or provider.kind != "line-stream":
        return manifest
    try:
        watcher = provider.watcher()
        pairs = list(watcher.iter_files()) if hasattr(watcher, "iter_files") else []
    except Exception:  # noqa: BLE001 — a broken watcher is itself evidence; note and go on
        logger.warning("sample collection: %s watcher failed", provider.name, exc_info=True)
        return manifest

    ledgered = _recent_source_ids(provider.name, home=home, days=60.0)
    by_recency = sorted(
        pairs, key=lambda ps: ps[0].stat().st_mtime if ps[0].exists() else 0, reverse=True
    )
    ordered = [ps for ps in by_recency if ps[1] in ledgered]
    fresh = [ps for ps in by_recency if ps[1] not in ledgered][:FRESH_SAMPLE_FILES]
    total = 0
    for path, source_id in ordered + fresh:
        if len(manifest) >= MAX_SAMPLE_FILES:
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0 or total + size > MAX_SAMPLE_BYTES:
            continue
        dest_name = f"{len(manifest):02d}-{path.name}"
        try:
            shutil.copy2(path, target / dest_name)
        except OSError:
            continue
        manifest[dest_name] = {
            "path": str(path),
            "source_id": source_id,
            "ledgered": source_id in ledgered,
        }
        total += size
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _snapshot_generations(provider_name: str, home: Optional[str]) -> list[str]:
    from .._watcher.drift_snapshot import DRIFT_DIRNAME

    root = resolve_paths(home).dumps_dir / DRIFT_DIRNAME / provider_name
    if not root.is_dir():
        return []
    return [str(p) for p in sorted(root.iterdir()) if p.is_dir()]


def _store_paths_line(provider) -> str:
    if provider.watcher is None:
        return "(no live store — export-fed or mechanism provider)"
    try:
        watcher = provider.watcher()
        paths = list(watcher.store_paths())
    except Exception:  # noqa: BLE001
        return "(watcher failed to enumerate — possibly the drift itself)"
    if not paths:
        return "(store empty or unavailable)"
    if len(paths) <= 3:
        return ", ".join(str(p) for p in paths)
    return f"{paths[0]} … ({len(paths)} files)"


def _write_evidence(provider, target_dir: Path, home: Optional[str], samples: dict) -> None:
    from .._importers._skip_ledger import LEDGER_FILE as SKIP_FILE
    from .._importers._validation_ledger import LEDGER_FILE as DRIFT_FILE
    from .._ops.health import read_health

    name = provider.name
    coverage = read_health().get("coverage_last") or {}
    verdict = (coverage.get("degraded") or {}).get(name)
    drift = _recent_ledger_records(DRIFT_FILE, "provider", name, home)
    skips = _recent_ledger_records(SKIP_FILE, "source", name, home)
    versions = {}
    try:
        versions = json.loads(
            (resolve_paths(home).home / "seen-versions.json").read_text(encoding="utf-8")
        ).get(name, {})
    except (OSError, ValueError):
        pass

    lines = [
        f"# Evidence: {name} import drift",
        "",
        f"- provider: `{name}` (kind `{provider.kind}`, parser `{provider.parser_id or '—'}`)",
        f"- archive core: `{__version__}`",
        "- coverage verdict: "
        + (
            f"**degraded** — {verdict['reason']} since {verdict.get('since') or 'unknown'}"
            if verdict
            else "not currently degraded (user-initiated fix)"
        ),
        f"- live store: {_store_paths_line(provider)}",
        "",
        "## Validation-drift records (newest first)",
        "",
    ]
    if drift:
        for rec in drift:
            lines.append(
                f"- {rec.get('at')} `{rec.get('source_id')}` "
                f"({rec.get('count')} finding(s)): "
                + "; ".join(str(f) for f in (rec.get("findings") or [])[:5])
            )
    else:
        lines.append("(none in the ledger window)")
    lines += ["", "## Capture-skip records (newest first)", ""]
    if skips:
        for rec in skips:
            lines.append(
                f"- {rec.get('at')} `{rec.get('source_id')}` — {rec.get('reason')} "
                f"({rec.get('lines_skipped')}/{rec.get('lines_total')} lines)"
            )
    else:
        lines.append("(none in the ledger window)")
    lines += ["", "## Provider versions first seen", ""]
    if versions:
        for version, first_seen in sorted(versions.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"- `{version}` first seen {first_seen}")
        lines.append(
            "\nA drift onset that matches a new version's first-seen date names "
            "the release that changed the format."
        )
    else:
        lines.append("(no version tripwire state for this provider)")
    generations = _snapshot_generations(name, home)
    lines += ["", "## Drift-quarantine snapshots", ""]
    if generations:
        lines += [f"- {g}" for g in generations]
        lines.append(
            "\nEach generation's manifest.json maps stored copies to original "
            "paths and source ids; activation replays copies whose originals "
            "the provider has pruned."
        )
    else:
        lines.append("(none)")
    lines += ["", "## Samples collected", ""]
    if samples:
        for dest, info in samples.items():
            flag = " (ledgered failure)" if info.get("ledgered") else " (fresh store file)"
            lines.append(f"- `samples/{dest}`{flag} — source_id `{info['source_id']}`")
    else:
        lines.append(
            "(none collectable — diagnose from the quarantine snapshots or the "
            "live store paths above)"
        )
    (target_dir / "evidence.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_quirks(provider, target_dir: Path) -> None:
    from importlib import resources

    quirks_root = resources.files(__package__) / "quirks"
    for candidate in (provider.parser_id, provider.name, "_default"):
        if not candidate:
            continue
        doc = quirks_root / f"{candidate}.md"
        if doc.is_file():
            (target_dir / "quirks.md").write_text(
                doc.read_text(encoding="utf-8"), encoding="utf-8"
            )
            return


# ── the scaffold itself ──────────────────────────────────────────────────────


def scaffold(provider_name: str, home: Optional[str] = None) -> Path:
    """Generate (or refresh) the patch scaffold for ``provider_name``; returns
    the plugin directory. Fix-in-progress files are never clobbered — only
    evidence, samples, and quirks refresh. Registers the patch in config.json
    ``providers`` (disabled; activation is the deterministic enable gate)."""
    from .._api import open_archive
    from .._providers import get as get_provider

    open_archive(home)
    provider = get_provider(provider_name, home=home)
    if provider is None:
        raise ValueError(
            f"unknown provider {provider_name!r} — `archive providers` lists them"
        )
    if provider.mechanism:
        raise ValueError(
            f"{provider_name!r} is archive machinery, not a fixable source provider"
        )

    target = plugin_dir(provider_name, home)
    target.mkdir(parents=True, exist_ok=True)
    (target / "fixtures").mkdir(exist_ok=True)
    mod = module_name(provider_name)

    module_file = target / f"{mod}.py"
    if not module_file.exists():
        module_file.write_text(
            _MODULE_TEMPLATE.replace("{name}", provider_name), encoding="utf-8"
        )
    test_file = target / "test_patch.py"
    if not test_file.exists():
        test_file.write_text(_test_body(provider), encoding="utf-8")
    conftest = target / "conftest.py"
    if not conftest.exists():
        conftest.write_text(_CONFTEST_TEMPLATE, encoding="utf-8")

    samples = _collect_samples(provider, target / "samples", home)
    _write_evidence(provider, target, home, samples)
    _write_quirks(provider, target)

    now = datetime.now(timezone.utc).isoformat()
    cfg = load_config(home)
    providers = cfg.setdefault("providers", {})
    entry = providers.get(provider_name)
    if not isinstance(entry, dict):
        entry = providers[provider_name] = {"enabled": False}
    entry["module"] = f"{mod}:PROVIDER"
    entry["path"] = str(target)
    patch = entry.setdefault("patch", {})
    # A (re-)scaffold is a fix being built against the *current* core: stamp it,
    # and clear any retirement note from a previous core's patch. Pinning and
    # the enabled flag are the user's state and survive the refresh.
    patch["built_against"] = __version__
    patch.setdefault("created_at", now)
    patch["scaffolded_at"] = now
    patch.setdefault("pinned", False)
    patch.pop("retired", None)
    save_config(cfg, home)

    (target / "patch.json").write_text(
        json.dumps({"provider": provider_name, **patch}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    record_patch_event(
        "scaffolded", provider_name, home=home,
        built_against=__version__, samples=len(samples),
    )
    logger.info("scaffold ready: %s (%d sample(s) collected)", target, len(samples))
    return target
