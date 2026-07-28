"""Patch retirement: override patches are temporary by default.

A ``thread-archive source fix`` patch exists to bridge the gap until a core release
fixes the same drift properly. Left active past that release it would shadow
the proper fix forever — plugin-shadows-builtin is permanent by design, which
is the *pin* case, not the default. So self-update retires them: any release
newer than the core a patch was built against disables it (``enabled: false``
in config.json — the seam discovery already honors), with a ledger note.

Deliberately no cleverness about whether the release actually fixed that
provider's drift — that can't be known cheaply. If the drift persists past the
release, the degradation notice simply re-fires and the user re-runs
``thread-archive source fix``; the loop self-corrects with no intelligence required.
Pinning (``thread-archive source fix <provider> --pin``) is the explicit opt-out for
"I always want mine": a pinned patch survives every update until unpinned.

Retirement disables, never deletes: the patch directory and its fixtures stay,
so re-running the fix against the new core starts from the previous attempt's
evidence rather than from scratch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _version_tuple(version: str) -> Optional[tuple[int, ...]]:
    """``(major, minor, patch)`` from ``X.Y.Z`` or ``vX.Y.Z``; None if unparseable."""
    text = version.strip().lstrip("v")
    parts = text.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def retire_patches(home: Optional[str] = None, *, target: str) -> list[str]:
    """Disable every unpinned override patch built against a core older than
    ``target`` (a release version or tag). Returns the providers retired.

    Conservative on bad data: an entry whose ``built_against`` won't parse is
    left alone with a warning — retirement must never break a user's config
    over a malformed field it didn't write.
    """
    from .._config import load_config, save_config
    from .ledger import record_patch_event

    target_v = _version_tuple(target)
    if target_v is None:
        logger.warning("patch retirement: target version %r unparseable — skipped", target)
        return []

    cfg = load_config(home)
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return []

    retired: list[str] = []
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        patch = entry.get("patch")
        if not isinstance(patch, dict):
            continue  # a hand-installed plugin, not a fix-import patch
        if not entry.get("enabled", True):
            continue  # already inactive
        if patch.get("pinned"):
            logger.info("patch retirement: %s is pinned — kept across update to %s",
                        name, target)
            continue
        built = _version_tuple(str(patch.get("built_against", "")))
        if built is None:
            logger.warning(
                "patch retirement: %s has unparseable built_against=%r — left alone",
                name, patch.get("built_against"),
            )
            continue
        if built >= target_v:
            continue
        entry["enabled"] = False
        patch["retired"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": target,
        }
        retired.append(name)

    if retired:
        save_config(cfg, home)
        for name in retired:
            record_patch_event(
                "retired", name, home=home,
                built_against=str(providers[name]["patch"].get("built_against")),
                by=target,
            )
            logger.info(
                "patch retirement: %s override disabled (built against %s; core now %s). "
                "If its drift persists, the degradation notice re-fires and "
                "`thread-archive source fix %s` rebuilds it against the new core.",
                name, providers[name]["patch"].get("built_against"), target, name,
            )
    return retired
