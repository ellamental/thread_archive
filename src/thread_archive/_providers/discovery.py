"""Finding provider plugins: installed entry points, then config declarations.

Both mechanisms answer the same question — what providers exist beyond the
built-in ones — and differ only in how a provider announces itself. An entry
point is how a provider *ships*: declared in its distribution metadata, found
without the operator naming it anywhere. A config declaration is how a provider
is *developed*: a module path (and optionally a directory to import it from)
written into ``<home>/config.json``, needing no packaging step.

Everything here is fail-soft, and deliberately so. A plugin is third-party code
loaded into the ingest path; if it raises on import, the cost of propagating
that is every other source stopping too. A broken plugin is logged loudly and
skipped, and the providers around it keep capturing.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterator

from ..provider import Provider, resolve_provider

logger = logging.getLogger(__name__)

#: Distribution entry-point group a provider plugin publishes under.
ENTRY_POINT_GROUP = "thread_archive.providers"


def _load_target(target: str) -> object:
    """Import ``pkg.module:attr`` and return the attribute."""
    from importlib import import_module

    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise ValueError(f"expected 'module:attribute', got {target!r}")
    module = import_module(module_name)
    try:
        return getattr(module, attr)
    except AttributeError as e:
        raise ImportError(f"{module_name} has no attribute {attr!r}") from e


def from_entry_points() -> Iterator[tuple[str, Provider]]:
    """Providers published by installed distributions, as ``(origin, provider)``.

    A provider is visible here when its distribution is installed into the same
    environment as archive itself. The watcher daemon runs from archive's own
    console script, so that environment is the daemon's too — an entry point
    registered there needs no further wiring to reach ingest.
    """
    from importlib.metadata import entry_points

    try:
        found = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 — malformed metadata anywhere must not stop ingest
        logger.exception("provider discovery: could not read entry points")
        return
    for ep in found:
        origin = f"entry point {ep.name!r} ({ep.value})"
        try:
            yield origin, resolve_provider(ep.load())
        except Exception:  # noqa: BLE001 — one bad plugin must not take the rest down
            logger.exception("provider discovery: %s failed to load — skipped", origin)


def from_config(cfg: dict) -> Iterator[tuple[str, Provider]]:
    """Providers declared under config.json's ``providers`` key.

    Each entry is ``{"module": "pkg.mod:ATTR", "path": "<dir>"}``. ``path``, when
    given, is prepended to ``sys.path`` before the import, so a provider can be
    loaded straight out of a source tree with no install step — the shape a
    plugin takes while it is being written, and the shape one takes when it lives
    beside the harness it serves rather than being published on its own.
    """
    declared = cfg.get("providers")
    if not isinstance(declared, dict):
        if declared is not None:
            logger.error("provider discovery: config 'providers' is not an object — ignored")
        return
    for name, entry in sorted(declared.items()):
        if not isinstance(entry, dict):
            logger.error("provider discovery: config entry %r is not an object — skipped", name)
            continue
        if entry.get("enabled") is False:
            continue
        target = entry.get("module")
        if not isinstance(target, str) or not target:
            logger.error("provider discovery: config entry %r has no 'module' — skipped", name)
            continue
        origin = f"config provider {name!r} ({target})"
        try:
            raw_path = entry.get("path")
            if isinstance(raw_path, str) and raw_path:
                resolved = str(Path(raw_path).expanduser())
                if resolved not in sys.path:
                    sys.path.insert(0, resolved)
            yield origin, resolve_provider(_load_target(target))
        except Exception:  # noqa: BLE001 — one bad declaration must not take the rest down
            logger.exception("provider discovery: %s failed to load — skipped", origin)


def discover(cfg: dict) -> list[tuple[str, Provider]]:
    """Every plugin-supplied provider, entry points first then config.

    Order is the precedence order: a config declaration for a name an installed
    distribution already published wins, so a checkout can be pointed at during
    development without uninstalling the released copy.
    """
    return list(from_entry_points()) + list(from_config(cfg))
