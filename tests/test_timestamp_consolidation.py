"""Parity pins for the provider timestamp parsers that route through the
canonical ``parse_timestamp``.

The opencode / cursor / grok delegations are guarded by their provider goldens;
these cover the three sites with no golden (xai exports, claude-science frame
synthesis) plus the shared ``parse_timestamp`` behaviour they lean on, so a future
change to the core can't silently shift their ``occurred_at``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive._importers.claude_science import _synthesize_line
from thread_archive._importers.exports import _xai_parse_time
from thread_archive._thread_import.timestamps import parse_timestamp

# A concrete instant: 1_700_000_000 s == 1_700_000_000_000 ms == 2023-11-14T22:13:20Z.
_INSTANT = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_parse_timestamp_rejects_bools() -> None:
    # bool is an int subclass; without the guard True/False read as epoch 1/0.
    assert parse_timestamp(True) is None
    assert parse_timestamp(False) is None
    assert parse_timestamp(True, default=_INSTANT) is _INSTANT


def test_parse_timestamp_ms_threshold_is_1e12() -> None:
    # Seconds stay seconds; a value past the ms threshold is divided.
    assert parse_timestamp(1_700_000_000) == _INSTANT
    assert parse_timestamp(1_700_000_000_000) == _INSTANT


def test_parse_timestamp_offsetless_iso_is_coerced_to_utc() -> None:
    dt = parse_timestamp("2023-11-14T22:13:20")
    assert dt == _INSTANT and dt.tzinfo == timezone.utc


def test_xai_parse_time_covers_every_input_shape() -> None:
    # Mongo extended-JSON (nested $numberLong and flat), epoch-ms, epoch-seconds,
    # and both ISO spellings all land on the same aware-UTC instant.
    assert _xai_parse_time({"$date": {"$numberLong": "1700000000000"}}) == _INSTANT
    assert _xai_parse_time({"$date": 1_700_000_000_000}) == _INSTANT
    assert _xai_parse_time(1_700_000_000_000) == _INSTANT  # ms (> 1e11)
    assert _xai_parse_time(1_700_000_000) == _INSTANT      # seconds (< 1e11)
    assert _xai_parse_time("2023-11-14T22:13:20Z") == _INSTANT
    assert _xai_parse_time("2023-11-14T22:13:20+00:00") == _INSTANT


def test_xai_parse_time_rejects_junk() -> None:
    assert _xai_parse_time(None) is None
    assert _xai_parse_time(True) is None       # bool is not a timestamp
    assert _xai_parse_time({"$date": None}) is None
    assert _xai_parse_time("not-a-date") is None


def test_synthesize_line_timestamp_matches_epoch_ms_formula() -> None:
    # The frame synthesizer stamps each message at base_ms + idx*step (ms), which
    # must still render as the plain aware-UTC ISO of that instant.
    base_ms = 1_700_000_000_000
    line0 = _synthesize_line("frame1", 0, {"role": "user", "content": "hi"}, None, base_ms)
    assert line0 is not None
    assert line0["timestamp"] == _INSTANT.isoformat()

    line1 = _synthesize_line("frame1", 1, {"role": "user", "content": "hi"}, None, base_ms)
    assert line1 is not None
    # idx 1 advances exactly one second (step is 1000 ms).
    assert line1["timestamp"] == datetime.fromtimestamp(
        1_700_000_001, tz=timezone.utc
    ).isoformat()
