"""Regression tests for the vendored thread_import ChatGPT timestamp +
content-text fixes.

ChatGPT exports carry epoch `create_time`/`update_time`; these must import as
aware UTC (not naive-local, which silently skews all ChatGPT history by the
host's UTC offset). SQLite (this store's index) has no session-TZ coercion, so a
naive-local string would be stored skewed. And a non-string `content.text` must
not crash event build. Also pins offset-less ISO -> aware UTC so the event-
ordering path never mixes naive and aware datetimes.
"""

from datetime import datetime, timezone

from thread_archive._thread_import.parsers.chatgpt import (
    _extract_create_time_iso,
    _extract_update_time_iso,
)
from thread_archive._thread_import.parsers.chatgpt_content import extract_text_from_content
from thread_archive._thread_import.timestamps import parse_timestamp, parse_timestamp_iso


def test_create_time_epoch_is_aware_utc():
    # A host in any zone must emit +00:00 for this epoch.
    assert _extract_create_time_iso({"create_time": 1609459200}) == "2021-01-01T00:00:00+00:00"


def test_update_time_epoch_is_aware_utc():
    assert _extract_update_time_iso({"update_time": 1609459200}) == "2021-01-01T00:00:00+00:00"


def test_missing_create_time_is_none():
    assert _extract_create_time_iso({}) is None


def test_epoch_iso_pinned_to_utc_offset():
    assert parse_timestamp_iso(1609459200) == "2021-01-01T00:00:00+00:00"


def test_offsetless_iso_string_is_treated_as_utc():
    dt = parse_timestamp("2021-01-01T00:00:00")
    assert dt is not None and dt.tzinfo is not None
    assert dt == datetime(2021, 1, 1, tzinfo=timezone.utc)


def test_explicit_offset_is_preserved():
    dt = parse_timestamp("2021-01-01T00:00:00-05:00")
    assert dt == datetime(2021, 1, 1, 5, 0, tzinfo=timezone.utc)


def test_naive_and_aware_results_are_comparable():
    # Mixing the two must not raise TypeError on comparison.
    assert parse_timestamp(1609459200) == parse_timestamp("2021-01-01T00:00:00")


def test_content_text_string_passthrough():
    assert extract_text_from_content({"text": "hello"}) == "hello"


def test_content_text_dict_does_not_crash():
    # A dict `text` must come back as "" — verbatim passthrough crashes on `.strip()`.
    assert extract_text_from_content({"text": {"nested": "x"}}) == ""


def test_content_text_none_falls_through_to_parts():
    assert extract_text_from_content({"text": None, "parts": ["a", "b"]}) == "a\n\nb"
