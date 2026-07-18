"""The provider registry — one list of every source archive can preserve.

Built-in providers and plugin-supplied ones land in the same registry and are
indistinguishable downstream. Everything that used to enumerate providers by
hand now asks here: the watcher set, ``archive import --provider``, setup's
source list, export-drop classification, capture coverage.

Loading a provider has one side effect beyond registration: its
``parser_config`` and ``parser`` are pushed into the parser island, which cannot
reach outward for them (see
:func:`~thread_archive._thread_import.parsers.config.base.register_provider_config`).

The registry is cached per config, because it is read on every watch poll and
rebuilding it would re-import every plugin each time. :func:`reset` drops the
cache; tests that install a provider call it.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from ..provider import Provider
from .builtins import builtin_providers

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, dict[str, Provider]] = {}


def _cache_key(cfg: dict) -> str:
    declared = cfg.get("providers")
    if not isinstance(declared, dict):
        return ""
    return repr(sorted(declared.items()))


def _register_parsing(provider: Provider) -> None:
    """Push a provider's parser knowledge into the parser island."""
    from ..provider.parse import register_parser, register_provider_config

    if provider.parser_config is not None:
        register_provider_config(provider.parser_config)
    if provider.parser is not None:
        register_parser(provider.name, provider.parser)


def _build(cfg: dict) -> dict[str, Provider]:
    from .discovery import discover

    providers: dict[str, Provider] = {p.name: p for p in builtin_providers()}
    for origin, provider in discover(cfg):
        if provider.name in providers:
            # A plugin may deliberately replace a built-in — that is how a
            # provider gets maintained outside the package. Say so, because a
            # silent override is indistinguishable from a name collision.
            logger.info("provider %r overridden by %s", provider.name, origin)
        providers[provider.name] = provider
    for provider in providers.values():
        try:
            _register_parsing(provider)
        except Exception:  # noqa: BLE001 — bad parser metadata must not stop ingest
            logger.exception(
                "provider %r: registering its parser/config failed — its drift ledger "
                "is inactive, import continues", provider.name,
            )
    return dict(sorted(providers.items(), key=lambda kv: (kv[1].order, kv[0])))


def registry(cfg: Optional[dict] = None, home=None) -> dict[str, Provider]:
    """Every known provider by name, in poll order.

    ``cfg`` is a loaded ``config.json``; omit it and the home's config is read.
    """
    if cfg is None:
        from .._config import load_config

        cfg = load_config(home)
    key = _cache_key(cfg)
    with _lock:
        cached = _cache.get(key)
        if cached is None:
            cached = _build(cfg)
            _cache[key] = cached
        return cached


def reset() -> None:
    """Drop the cached registry so the next read rebuilds it."""
    with _lock:
        _cache.clear()


def get(name: str, cfg: Optional[dict] = None, home=None) -> Optional[Provider]:
    """One provider by name, or None."""
    return registry(cfg, home).get(name)


def sources(*, include_mechanisms: bool = True, cfg: Optional[dict] = None, home=None) -> list[Provider]:
    """Providers in poll order, optionally without archive's own machinery."""
    return [
        p for p in registry(cfg, home).values()
        if include_mechanisms or not p.mechanism
    ]


def sources_using_parser(parser_id: str, cfg: Optional[dict] = None, home=None) -> list[Provider]:
    """Every provider whose transcripts are read by the ``parser_id`` parser.

    What tooling that re-parses stored transcripts iterates over — a repair pass
    over Claude-Code-shaped logs covers every harness that writes that shape, not
    only Claude Code itself, and picks up a plugin declaring the same parser
    without being edited.
    """
    return [p for p in registry(cfg, home).values() if p.parser_id == parser_id]


def mechanism_names(cfg: Optional[dict] = None, home=None) -> frozenset[str]:
    """Names of the sources that are archive's own machinery rather than a store
    an operator chose to have — the drop zone reading the archive's own dumps
    directory, a recovery pass over another source's log. Reports that speak
    about "sources the operator uses" exclude these."""
    return frozenset(p.name for p in registry(cfg, home).values() if p.mechanism)


def labels(cfg: Optional[dict] = None, home=None) -> dict[str, str]:
    """``{name: human label}`` for every provider."""
    return {p.name: p.label for p in registry(cfg, home).values()}


def importers(kind: str, cfg: Optional[dict] = None, home=None) -> dict[str, object]:
    """``{name: importer}`` for every provider of one dispatch ``kind``."""
    return {
        p.name: p.importer
        for p in registry(cfg, home).values()
        if p.kind == kind and p.importer is not None
    }


def export_specs(cfg: Optional[dict] = None, home=None):
    """``(provider, spec)`` for every provider accepting account exports."""
    return [(p, p.export) for p in registry(cfg, home).values() if p.export is not None]


__all__ = [
    "registry",
    "reset",
    "get",
    "sources",
    "labels",
    "importers",
    "export_specs",
    "builtin_providers",
]
