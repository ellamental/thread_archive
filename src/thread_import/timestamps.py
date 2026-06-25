"""Unified timestamp parsing for thread import.

Handles ISO 8601 strings (with or without Z suffix), unix epoch
(seconds or milliseconds), and None/empty values.
"""

from datetime import datetime, timezone
from typing import Optional, Union


def parse_timestamp(ts: Optional[Union[str, int, float]], *, default: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a timestamp value into a datetime.

    Args:
        ts: ISO string, unix epoch (seconds or milliseconds), or None
        default: fallback value when ts is None/empty/unparseable.
                 If not provided, returns None on failure.

    Returns:
        Parsed datetime, or default if parsing fails.
    """
    if ts is None or ts == "":
        return default

    # ISO string
    if isinstance(ts, str):
        try:
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return default

    # Unix timestamp (seconds or milliseconds). Epoch is an absolute instant —
    # build an aware UTC datetime, not a naive-local one (the host's local zone
    # would silently skew the time by the UTC offset).
    if isinstance(ts, (int, float)):
        try:
            if ts > 1_000_000_000_000:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            return default

    return default


def parse_timestamp_iso(ts: Optional[Union[str, int, float]]) -> Optional[str]:
    """Parse a timestamp and return as ISO string, or None."""
    dt = parse_timestamp(ts)
    return dt.isoformat() if dt else None
