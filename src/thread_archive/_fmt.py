"""Display formatting shared by the surfaces that print to a person.

A leaf: it formats values and calls nothing of ours. Lives here rather than in one
of its callers so that a second caller does not have to import a front door to
borrow a formatter — which is how a setup wizard ends up importing the CLI.

Not the only age formatter in the package, and deliberately so:
:func:`.._ops.notices._age` renders at the resolution an *operator alert* is acted
on ("just now", "12m ago", "never"), while this one is the fractional-hours form
the CLI's status tables read in. They are different answers to different
questions; collapsing them would change what one of the two surfaces prints.

The same holds for the two byte formatters that stay private to their callers:
``cli._fmt_bytes`` renders *decimal* units (a ledger's retained bytes, stated the
way a disk vendor states them) and ``_retrieval.read._fmt_bytes`` renders a
whole-number KB inside a one-line binary marker, degrading to an empty string
rather than a mark. :func:`size` is the binary, operator-facing one everything
else shares.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def size(n: Optional[int]) -> str:
    """Bytes as an operator reads them — a small number and a unit that keeps it
    small. Binary units, matching what ``du`` reports for the same directory.

    Past ``GB`` the number keeps growing rather than inventing a unit: a size that
    big is a fault to look at, not a figure to make comfortable. ``"?"`` for an
    absent size, so a table cell that has no answer says so."""
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KB", "MB"):
        if abs(value) < 1024.0:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


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
