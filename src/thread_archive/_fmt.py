"""Display formatting shared by the surfaces that print to a person.

A leaf: it formats values and calls nothing of ours. Lives here rather than in one
of its callers so that a second caller does not have to import a front door to
borrow a formatter — which is how a setup wizard ends up importing the CLI.

Not the only age formatter in the package, and deliberately so:
:func:`.._ops.notices._age` renders at the resolution an *operator alert* is acted
on ("just now", "12m ago", "never"), while this one is the fractional-hours form
the CLI's status tables read in. They are different answers to different
questions; collapsing them would change what one of the two surfaces prints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def age(iso: Optional[str]) -> str:
    """How long ago ``iso`` was, as ``"3.2h ago"`` / ``"1.5d ago"``.

    ``"?"`` for anything unreadable — absent, not a timestamp, the wrong type. A
    status table prints one of these per row beside a real value, so a stamp it
    cannot parse must degrade to a mark rather than raise and take the table with
    it. Naive timestamps are read as UTC, which is what every stamp the archive
    writes is.
    """
    if iso is None:
        return "?"
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return f"{hours / 24:.1f}d ago" if hours >= 48 else f"{hours:.1f}h ago"
