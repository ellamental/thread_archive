"""The version tripwire: ``<home>/seen-versions.json``.

Harness format changes ride version bumps — a Claude Code line carries the CLI
version that wrote it — so the *first sighting* of a new version string is the
earliest drift warning available: it fires before any field has drifted, and
when one later does, it names the release that grew it. Each first sighting
appends one record to the validation-drift ledger (the same trail
``thread-archive coverage`` and the nightly's escalation read) and is remembered here
so it never fires twice. The record is flagged ``advisory``: a release is a
heads-up, not a finding, so it shows in the trail without counting toward the
drift volume that warns or degrades the source.

State is ``{provider: {version: first_seen_iso}}``. Advisory and fail-soft
throughout: a lost or raced state file costs at worst a duplicate ledger
record, never an import. Providers whose lines carry no ``version`` key simply
never trip it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from .._config import resolve_paths
from ._validation_ledger import VERSION_SIGHTING_LEAD, record_drift

logger = logging.getLogger(__name__)

SEEN_VERSIONS_FILE = "seen-versions.json"


def _line_versions(messages: list) -> set[str]:
    """Every distinct ``version`` string on the messages' raw source lines."""
    versions: set[str] = set()
    for msg in messages:
        line = (msg.get("provider_data") or {}).get("line")
        if isinstance(line, dict):
            v = line.get("version")
            if isinstance(v, str) and v:
                versions.add(v)
    return versions


def _load_seen(path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_seen(path, seen: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".seen-versions-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(seen, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def note_new_versions(
    messages: list, *, provider: str, source_id: str, batch_safe: bool
) -> None:
    """Record a drift-ledger sighting for each version string never seen before."""
    try:
        versions = _line_versions(messages)
        if not versions:
            return
        path = resolve_paths().home / SEEN_VERSIONS_FILE
        seen = _load_seen(path)
        known = seen.setdefault(provider, {})
        new = sorted(v for v in versions if v not in known)
        if not new:
            return
        now = datetime.now(timezone.utc).isoformat()
        for v in new:
            known[v] = now
        _save_seen(path, seen)
        for v in new:
            logger.info("first sighting of %s version %s", provider, v)
        record_drift(
            provider,
            source_id,
            findings=[
                f"{VERSION_SIGHTING_LEAD} {provider} version '{v}' - format changes "
                f"ride version bumps; if field/line-type warnings follow, this "
                f"is the release that grew them (advisory)"
                for v in new
            ],
            batch_safe=batch_safe,
            advisory=True,
        )
    except Exception:  # noqa: BLE001 — the tripwire must never break an import
        logger.exception("version tripwire failed for %s", provider)
