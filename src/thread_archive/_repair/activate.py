"""Patch activation and pinning: the deterministic gate a fix passes through.

Nobody enables a patch by asserting it works — not the user, not an agent they
handed the scaffold to. Activation re-derives everything: load the module,
check it resolves to an override of the right provider, run the scaffold's test
suite in a subprocess, and only on green flip ``enabled: true`` in config.json
and run the ledger-driven re-import. A red suite refuses, whatever the fix
claimed about itself. This is what makes the loop trustworthy with a thin model
in it: the exit bar is enforced by code the model cannot edit.

Pinning is the user's "I always want mine" flag: a pinned patch survives
self-update retirement (see :mod:`.retire`) until unpinned.
"""

from __future__ import annotations

import importlib
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .._config import load_config, save_config
from .ledger import record_patch_event
from .scaffold import plugin_dir

logger = logging.getLogger(__name__)

TEST_TIMEOUT_S = 600


class ActivationError(RuntimeError):
    """A patch failed an activation check; the message says which and why."""


def _patch_entry(cfg: dict, provider_name: str) -> dict:
    entry = (cfg.get("providers") or {}).get(provider_name)
    if not isinstance(entry, dict) or not isinstance(entry.get("patch"), dict):
        raise ActivationError(
            f"no fix-import patch is registered for {provider_name!r} — "
            f"run `thread_archive fix-import {provider_name}` first"
        )
    return entry


def _load_override(entry: dict, provider_name: str):
    """Import the patch module the way discovery will, and validate the result
    the way the registry would — but *before* enabling, so a broken module is a
    refusal here rather than a silently-skipped plugin at every future load."""
    from ..provider import resolve_provider

    module_ref = str(entry.get("module") or "")
    path = str(entry.get("path") or "")
    if ":" not in module_ref:
        raise ActivationError(f"malformed module reference {module_ref!r} in config")
    module_name, attr = module_ref.split(":", 1)
    if path:
        # Front of the path even if already present, and a fresh import rather
        # than a reload: the agent iterates on the module between activation
        # attempts, and a reload would re-resolve through the module's original
        # spec — stale code (or a prior test home's copy) must not win.
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    sys.modules.pop(module_name, None)
    try:
        module = importlib.import_module(module_name)
        provider = resolve_provider(getattr(module, attr))
    except ActivationError:
        raise
    except Exception as e:  # noqa: BLE001 — surface exactly what discovery would hit
        raise ActivationError(f"patch module failed to load: {e!r}") from e
    if provider.name != provider_name:
        raise ActivationError(
            f"patch resolves to provider {provider.name!r}, expected "
            f"{provider_name!r} — an override must keep the built-in's name"
        )
    return provider


def _run_tests(directory: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(directory), "-q"],
        capture_output=True,
        text=True,
        timeout=TEST_TIMEOUT_S,
    )


def activate(
    provider_name: str,
    home: Optional[str] = None,
    *,
    reimport: bool = True,
    run_tests: Optional[Callable[[Path], subprocess.CompletedProcess]] = None,
) -> dict:
    """Validate → test → enable → re-import. Returns a summary dict; raises
    :class:`ActivationError` with the failing check's story on refusal.
    ``run_tests`` is injectable for tests of this module; production uses the
    pytest subprocess."""
    from .. import __version__
    from .._providers import reset as reset_registry
    from .reimport import reimport_source

    cfg = load_config(home)
    entry = _patch_entry(cfg, provider_name)
    directory = Path(str(entry.get("path") or plugin_dir(provider_name, home)))

    _load_override(entry, provider_name)

    try:
        import pytest  # noqa: F401 — presence check only
    except ImportError as e:
        raise ActivationError(
            "activation runs the patch's test suite, which needs pytest — "
            "install it into the archive's venv (pip install pytest) and re-run"
        ) from e
    result = (run_tests or _run_tests)(directory)
    if result.returncode != 0:
        tail = "\n".join((result.stdout or "").strip().splitlines()[-15:])
        raise ActivationError(
            f"the patch's test suite is red — activation refused.\n{tail}"
        )

    entry["enabled"] = True
    entry["patch"]["activated_at"] = datetime.now(timezone.utc).isoformat()
    entry["patch"]["built_against"] = entry["patch"].get("built_against") or __version__
    save_config(cfg, home)
    reset_registry()  # the next registry read must see the override
    record_patch_event(
        "activated", provider_name, home=home,
        built_against=entry["patch"]["built_against"],
    )
    logger.info("%s: patch active (override enabled in config.json)", provider_name)

    summary: dict = {"activated": True, "reimport": None}
    if reimport:
        summary["reimport"] = reimport_source(provider_name, home=home)
    return summary


def set_pinned(provider_name: str, pinned: bool, home: Optional[str] = None) -> None:
    """Flip the patch's pin flag: pinned patches survive self-update retirement."""
    cfg = load_config(home)
    entry = _patch_entry(cfg, provider_name)
    entry["patch"]["pinned"] = bool(pinned)
    save_config(cfg, home)
    record_patch_event("pinned" if pinned else "unpinned", provider_name, home=home)
    logger.info(
        "%s: patch %s — %s",
        provider_name,
        "pinned" if pinned else "unpinned",
        "it will survive self-update retirement until unpinned"
        if pinned
        else "the next core release retires it (the default lifecycle)",
    )
