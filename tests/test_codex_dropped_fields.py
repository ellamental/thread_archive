"""Codex importer maps the source fields easiest to drop: per-turn token usage,
user-message images, session_meta git provenance, and turn_context
effort/personality — all through the sanctioned channels (structured usage on
api_request_completed, image content blocks, annotations) without perturbing
dedup identity.
"""

from __future__ import annotations

import glob
import json
import os

import pytest

from thread_archive._importers.codex import (
    _build_codex_messages,
    _codex_ambient_annotations,
    _codex_call_maps,
    _codex_source_metadata,
)
from thread_archive._thread_import import DefaultEventBuilder


def _lines(*payloads):
    return list(payloads)


def _turn_lines(**turn_context_extra):
    ctx = {"model": "gpt-5.5", **turn_context_extra}
    return [
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"type": "task_started"}},
        {"type": "turn_context", "timestamp": "2026-01-01T10:00:00Z", "payload": ctx},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:01Z",
         "payload": {"type": "user_message", "message": "do the thing", "turn_id": "t1"}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:02Z",
         "payload": {"type": "reasoning", "summary": [{"text": "thinking it over"}]}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:03Z",
         "payload": {"type": "agent_message", "message": "done"}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:04Z",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 60,
                                  "output_tokens": 20, "reasoning_output_tokens": 5,
                                  "total_tokens": 120},
             "model_context_window": 258400,
         }}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:05Z",
         "payload": {"type": "agent_message", "message": "and more"}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:06Z",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {"input_tokens": 150, "cached_input_tokens": 100,
                                  "output_tokens": 30, "reasoning_output_tokens": 10,
                                  "total_tokens": 180},
             "model_context_window": 258400,
         }}},
    ]


def _build(lines, model="codex", ambient=None):
    names, inputs = _codex_call_maps(lines)
    return _build_codex_messages(lines, model, names, inputs, ambient=ambient)


def _events_by_type(messages):
    builder = DefaultEventBuilder()
    out = {}
    for msg in messages:
        for ev in builder.build_events(msg, stream_id="s1"):
            out.setdefault(ev.event_type, []).append(ev)
    return out


# ── Per-turn token usage ─────────────────────────────────────────────────────


def test_token_counts_sum_into_structured_usage() -> None:
    """token_count lines each measure one API request; the assembled turn sums
    them into canonical uncached input plus cache reads and output."""
    messages = _build(_turn_lines(effort="xhigh", personality="pragmatic"))
    # task_started/turn_context precede the user message (real codex ordering),
    # so they form a preserved preamble message; the turn's transcript is last.
    turn = messages[-1]
    assert turn["role"] == "assistant"
    assert any(b["type"] == "text" for b in turn["content_blocks"])
    usage = turn["provider_data"]["usage"]
    assert usage == {
        "input_tokens": 90,
        "input_tokens_includes_cache": False,
        "output_tokens": 50,
        "thinking_tokens": 15,
        "cache_read_tokens": 160,
    }
    assert "total_tokens" not in usage

    completed = _events_by_type([turn])["api_request_completed"]
    assert len(completed) == 1
    p = completed[0].payload
    assert p["input_tokens"] == 90
    assert p["input_tokens_includes_cache"] is False
    assert p["output_tokens"] == 50
    assert p["thinking_tokens"] == 15
    assert p["cache_read_tokens"] == 160


def test_raw_token_count_blocks_still_preserved() -> None:
    """Structured usage rides alongside the raw codex_token_count preserved
    blocks — the fallback record stays."""
    messages = _build(_turn_lines())
    assistant = messages[-1]
    raw = [b for b in assistant["content_blocks"] if b["type"] == "codex_token_count"]
    assert len(raw) == 2
    assert raw[0]["raw"]["info"]["last_token_usage"]["total_tokens"] == 120


def test_model_context_window_and_turn_context_annotations() -> None:
    messages = _build(_turn_lines(effort="xhigh", personality="pragmatic"))
    assistant = messages[-1]
    ann = assistant["provider_data"]["annotations"]
    assert ann["model_context_window"] == 258400
    assert ann["effort"] == "xhigh"
    assert ann["personality"] == "pragmatic"
    # The model itself stays a first-class field, not an annotation.
    assert assistant["provider_data"]["model"] == "gpt-5.5"

    completed = _events_by_type([assistant])["api_request_completed"][0]
    assert completed.payload["annotations"]["model_context_window"] == 258400
    assert completed.payload["annotations"]["effort"] == "xhigh"
    assert completed.payload["annotations"]["personality"] == "pragmatic"


def test_effort_carries_forward_from_prior_lines() -> None:
    """Incremental imports resume mid-session: effort/personality named behind
    the watermark still annotate the turns after it."""
    prior = [{"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "low",
                                                  "personality": "terse"}}]
    ambient = _codex_ambient_annotations(prior)
    assert ambient == {"effort": "low", "personality": "terse"}

    new_lines = [
        {"type": "event_msg", "timestamp": "2026-01-01T11:00:00Z",
         "payload": {"type": "agent_message", "message": "resumed"}},
    ]
    messages = _build(new_lines, model="gpt-5.5", ambient=ambient)
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["provider_data"]["annotations"] == {"effort": "low", "personality": "terse"}


# ── User-message images ──────────────────────────────────────────────────────

_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _image_user_lines(message="look at this"):
    return [
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"type": "user_message", "message": message, "turn_id": "t1",
                     "images": [f"data:image/png;base64,{_PNG_B64}"],
                     "local_images": ["/tmp/shot.png"], "text_elements": []}},
    ]


def test_user_images_become_image_blocks_on_multimodal_payload() -> None:
    messages = _build(_image_user_lines())
    user = messages[0]
    assert user["role"] == "user"
    blocks = user["content_blocks"]
    assert blocks and blocks[0]["type"] == "image"
    assert blocks[0]["source"] == {
        "type": "base64", "media_type": "image/png", "data": _PNG_B64,
    }

    sent = _events_by_type(messages)["user_message_sent"]
    assert len(sent) == 1
    p = sent[0].payload
    assert p["content_type"] == "multimodal"
    assert p["images"] == [{"media_type": "image/png", "data": _PNG_B64}]
    assert p["annotations"]["local_images"] == ["/tmp/shot.png"]
    assert "text_elements" not in p.get("annotations", {})  # empty list stays out


def test_image_only_user_message_survives() -> None:
    """A screenshot paste with no caption must not vanish."""
    messages = _build(_image_user_lines(message=""))
    assert messages and messages[0]["role"] == "user"
    sent = _events_by_type(messages)["user_message_sent"]
    assert len(sent) == 1
    assert sent[0].payload["images"]


def test_non_data_url_image_kept_as_reference() -> None:
    lines = [
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"type": "user_message", "message": "see", "turn_id": "t1",
                     "images": ["https://example.com/x.png"]}},
    ]
    messages = _build(lines)
    block = messages[0]["content_blocks"][0]
    assert block == {"type": "image", "url": "https://example.com/x.png",
                     "start_timestamp": "2026-01-01T10:00:00Z"}
    sent = _events_by_type(messages)["user_message_sent"][0]
    assert sent.payload["images"] == [{"media_type": "image", "url": "https://example.com/x.png"}]


# ── Dedup identity ───────────────────────────────────────────────────────────


def test_dedup_keys_unchanged_by_extras() -> None:
    """usage, annotations, and the images payload key are all outside the dedup
    content keys: enriching an already-stored turn never changes its identity,
    so re-imports collapse instead of duplicating. Driven by stripping the extras
    from the built messages and comparing dedup keys against the enriched build."""
    import copy

    messages = _build(_turn_lines(effort="xhigh", personality="pragmatic"))
    messages += _build(_image_user_lines())
    stripped = copy.deepcopy(messages)
    for msg in stripped:
        msg["provider_data"].pop("usage", None)
        msg["provider_data"].pop("annotations", None)

    enriched_events = _events_by_type(messages)
    stripped_events = _events_by_type(stripped)
    assert set(enriched_events) == set(stripped_events)
    for etype, evs in enriched_events.items():
        assert [e.dedup_key for e in evs] == \
            [e.dedup_key for e in stripped_events[etype]], etype

    # And the enrichment really was there to strip.
    completed = enriched_events["api_request_completed"]
    assert any(e.payload["input_tokens"] == 90 for e in completed)
    assert all(e.payload["input_tokens"] == 0
               for e in stripped_events["api_request_completed"])

    # The images key itself is outside the dedup content keys too: the same
    # user text with and without its images hashes to the same identity.
    plain_user = [
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"type": "user_message", "message": "look at this", "turn_id": "t1"}},
    ]
    with_images = _events_by_type(_build(_image_user_lines()))["user_message_sent"][0]
    without = _events_by_type(_build(plain_user))["user_message_sent"][0]
    assert with_images.dedup_key == without.dedup_key


# ── session_meta provenance ──────────────────────────────────────────────────


def test_source_metadata_git_provenance() -> None:
    meta = {
        "cwd": "/proj",
        "cli_version": "0.142.0",
        "git": {"commit_hash": "abc123", "branch": "main",
                "repository_url": "git@github.com:x/y.git"},
    }
    assert _codex_source_metadata(meta) == {
        "cwd": "/proj",
        "cli_version": "0.142.0",
        "git": {"commit_hash": "abc123", "branch": "main",
                "repository_url": "git@github.com:x/y.git"},
    }
    assert _codex_source_metadata({}) is None
    assert _codex_source_metadata({"git": {}}) is None
    assert _codex_source_metadata({"cwd": "/p"}) == {"cwd": "/p"}


# ── Real-data smoke (read-only) ──────────────────────────────────────────────


def test_real_session_maps_nonzero_usage() -> None:
    """Run the message builder over one real codex session file (read-only) and
    confirm token_count lines land as nonzero structured usage.

    conftest sandboxes $HOME, so the real home is resolved via pwd; nothing is
    ever written under it here."""
    import pwd

    real_home = pwd.getpwuid(os.getuid()).pw_dir
    files = sorted(glob.glob(
        os.path.join(real_home, ".codex/sessions/**/*.jsonl"), recursive=True))
    target = None
    for path in reversed(files):
        try:
            with open(path, encoding="utf-8") as fh:
                if '"token_count"' in fh.read():
                    target = path
                    break
        except OSError:
            continue
    if target is None:
        pytest.skip("no real codex session with token_count available")

    lines = []
    with open(target, encoding="utf-8") as fh:
        for raw in fh:
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    names, inputs = _codex_call_maps(lines)
    messages = _build_codex_messages(lines, "codex", names, inputs)
    usages = [m["provider_data"].get("usage") for m in messages
              if m["role"] == "assistant" and m["provider_data"].get("usage")]
    assert usages, f"no structured usage mapped from {target}"
    assert any(u.get("input_tokens", 0) > 0 and u.get("output_tokens", 0) > 0
               for u in usages)
