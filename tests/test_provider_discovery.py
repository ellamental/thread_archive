"""Finding provider plugins declared in ``config.json`` (``_providers.discovery``).

A plugin is third-party code loaded into the ingest path of an always-on daemon,
so what this covers is as much *how much* a declaration is allowed to change
about the process as whether the provider arrives.
"""

from __future__ import annotations

import sys

from thread_archive._providers import discovery

_PLUGIN = '''\
from thread_archive.provider import Provider

PROVIDER = Provider(name="declared-provider", label="Declared Provider")
'''


def _declare(tmp_path, *, name: str = "declared-provider") -> dict:
    """A config declaring one provider out of a source tree, the no-install shape."""
    pkg = tmp_path / "plugin-src"
    pkg.mkdir(exist_ok=True)
    (pkg / "declared_plugin.py").write_text(_PLUGIN, encoding="utf-8")
    return {"providers": {name: {"module": "declared_plugin:PROVIDER", "path": str(pkg)}}}


def test_a_declared_provider_loads_from_its_source_tree(tmp_path):
    before = list(sys.path)
    try:
        found = list(discovery.from_config(_declare(tmp_path)))
    finally:
        sys.path[:] = before
        sys.modules.pop("declared_plugin", None)
    assert [p.name for _, p in found] == ["declared-provider"]


def test_a_declared_path_joins_the_end_of_the_search_path(tmp_path, monkeypatch):
    """Appended, never prepended.

    The front of ``sys.path`` decides every *later* import in the process too, so
    a directory placed there shadows the standard library and site-packages for
    the whole daemon — a far wider grant than "load this module from here", and
    one that turns any file dropped in that directory into an import hijack. The
    declaration is already a grant of code execution; it should not also be a
    grant over imports it never named.
    """
    before = list(sys.path)
    try:
        list(discovery.from_config(_declare(tmp_path)))
        added = [p for p in sys.path if p not in before]
        assert added == [str(tmp_path / "plugin-src")]
        assert sys.path.index(added[0]) > 0
        assert sys.path[0] == before[0]
    finally:
        sys.path[:] = before
        sys.modules.pop("declared_plugin", None)


def test_a_broken_declaration_is_skipped_not_fatal(tmp_path, caplog):
    """One bad plugin must not take ingest down with it: the providers around it
    keep capturing, and the failure is loud in the log."""
    cfg = {"providers": {
        "no-module": {"enabled": True},
        "bad-target": {"module": "not_a_real_module_anywhere:X"},
        **_declare(tmp_path)["providers"],
    }}
    before = list(sys.path)
    try:
        found = list(discovery.from_config(cfg))
    finally:
        sys.path[:] = before
        sys.modules.pop("declared_plugin", None)
    assert [p.name for _, p in found] == ["declared-provider"]


def test_a_disabled_declaration_is_not_loaded(tmp_path):
    cfg = _declare(tmp_path)
    cfg["providers"]["declared-provider"]["enabled"] = False
    assert list(discovery.from_config(cfg)) == []
    assert "declared_plugin" not in sys.modules


def test_a_providers_key_that_is_not_an_object_is_ignored():
    assert list(discovery.from_config({"providers": ["nope"]})) == []
    assert list(discovery.from_config({})) == []


def test_an_invalid_config_declares_nothing():
    """``load_config`` marks a config it cannot structurally trust invalid and
    hands back an empty mapping, so a corrupt file cannot smuggle a provider —
    the same fail-closed direction the source policy takes."""
    from thread_archive._config import ArchiveConfig

    assert list(discovery.from_config(ArchiveConfig(valid=False))) == []
