"""Provider-export fields that used to be silently dropped now survive import.

claude.ai: tool_use/tool_result pairing ids, text citations, thinking summaries,
tool integration/MCP metadata + structured_content, safety ``flag`` blocks, and
``parent_message_uuid`` branch structure. ChatGPT: message metadata citations /
content_references / canvas, tether content text, code language, multimodal
assets. Both: conversation-level metadata folded into the thread's
source_metadata.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from thread_archive._importers.exports import (
    fold_conversation_metadata,
    import_chatgpt_export,
    import_claude_ai_export,
)
from thread_archive._store import Event, Thread, get_session, init_db
from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.parsers.chatgpt import ChatGPTParser
from thread_archive._thread_import.parsers.chatgpt_content import (
    extract_content_blocks,
    extract_text_from_content,
)
from thread_archive._thread_import.parsers.claude import ClaudeParser


# ── claude.ai fixture ────────────────────────────────────────────────────────

_CLAUDE_CONV = {
    "uuid": "conv-df",
    "name": "Dropped Fields",
    "summary": "what this chat was about",
    "created_at": "2026-01-01T10:00:00Z",
    "updated_at": "2026-01-01T10:01:00Z",
    "account": {"uuid": "acct-1"},
    "chat_messages": [
        {
            "uuid": "m1",
            "sender": "human",
            "text": "question",
            "content": [{"type": "text", "text": "question"}],
            "parent_message_uuid": "root-0000",
            "created_at": "2026-01-01T10:00:00Z",
        },
        {
            "uuid": "m2",
            "sender": "assistant",
            "text": "",
            "parent_message_uuid": "m1",
            "created_at": "2026-01-01T10:00:10Z",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "pondering",
                    "summaries": [{"summary": "pondered briefly"}],
                    "signature": "sig",
                },
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "web_search",
                    "input": {"query": "q"},
                    "integration_name": "Web Search",
                    "is_mcp_app": True,
                    "icon_name": "globe",
                    "integration_icon_url": "https://icons/x.png",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "name": "web_search",
                    "content": [{"type": "knowledge", "title": "T", "text": "body"}],
                    "structured_content": {"hits": 1},
                    "integration_name": "Web Search",
                },
                {
                    "type": "text",
                    "text": "answer with citation",
                    "citations": [
                        {
                            "uuid": "cit-1",
                            "start_index": 0,
                            "end_index": 6,
                            "details": {"type": "web_search_citation", "url": "https://x"},
                        }
                    ],
                },
                {
                    "type": "flag",
                    "flag": "self_harm_risk",
                    "helpline": {"name": "988 Lifeline", "phone_number": "988"},
                },
            ],
        },
    ],
}


def _claude_events():
    parser = ClaudeParser()
    messages = parser.parse_export([json.loads(json.dumps(_CLAUDE_CONV))])
    builder = DefaultEventBuilder()
    events = []
    for m in messages:
        events.extend(builder.build_events(m, stream_id="s1"))
    return messages, events


def _one(events, event_type):
    matched = [e for e in events if e.event_type == event_type]
    assert len(matched) == 1, f"expected one {event_type}, got {len(matched)}"
    return matched[0]


def test_claude_tool_use_and_result_are_paired() -> None:
    _, events = _claude_events()
    use = _one(events, "tool_use_complete")
    assert use.payload["tool_call_id"] == "toolu_1"
    assert "unpaired" not in use.payload
    assert "tool=toolu_1" in use.dedup_key
    result = _one(events, "tool_execution_completed")
    assert result.payload["tool_call_id"] == "toolu_1"
    assert "unpaired" not in result.payload
    assert "tool=toolu_1" in result.dedup_key


def test_claude_block_annotations_land_on_events() -> None:
    _, events = _claude_events()
    text = next(
        e for e in events
        if e.event_type == "text_complete" and e.payload["text"] == "answer with citation"
    )
    citations = text.payload["annotations"]["citations"]
    assert citations[0]["details"]["url"] == "https://x"

    think = _one(events, "thinking_complete")
    assert think.payload["annotations"]["summaries"] == [{"summary": "pondered briefly"}]

    use = _one(events, "tool_use_complete")
    assert use.payload["annotations"]["integration_name"] == "Web Search"
    assert use.payload["annotations"]["is_mcp_app"] is True
    assert "icon_name" not in use.payload["annotations"]
    assert "integration_icon_url" not in use.payload["annotations"]

    result = _one(events, "tool_execution_completed")
    assert result.payload["annotations"]["structured_content"] == {"hits": 1}
    assert result.payload["annotations"]["integration_name"] == "Web Search"


def test_claude_flag_block_survives_as_content_block_event() -> None:
    _, events = _claude_events()
    flag = next(
        e for e in events
        if e.event_type == "content_block" and e.payload.get("block_type") == "flag"
    )
    raw = flag.payload["data"]["data"]
    assert raw["flag"] == "self_harm_risk"
    assert raw["helpline"]["phone_number"] == "988"


def test_claude_parent_uuid_persists_as_branch_metadata() -> None:
    messages, events = _claude_events()
    by_id = {m["provider_message_id"]: m for m in messages}
    assert by_id["m1"]["provider_parent_id"] == "root-0000"
    assert by_id["m2"]["provider_parent_id"] == "m1"
    user = _one(events, "user_message_sent")
    assert user.payload["branch"]["parent_id"] == "root-0000"
    use = _one(events, "tool_use_complete")
    assert use.payload["branch"]["parent_id"] == "m1"


def test_claude_source_metadata_folds_conversation_metadata(archive_home) -> None:
    init_db()
    export_dir = archive_home / "claude_export"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text(
        json.dumps([_CLAUDE_CONV]), encoding="utf-8"
    )
    result = import_claude_ai_export(export_dir)
    assert result.imported == 1
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude")).scalar_one()
        meta = t.source_metadata or {}
    assert meta["provider"] == "claude" and meta["surface"] == "web"
    assert meta["summary"] == "what this chat was about"
    assert meta["account_uuid"] == "acct-1"
    # Empty conversation fields don't null out the dict.
    assert "project_uuid" not in meta and "title" not in meta


def test_claude_pairing_end_to_end_in_db(archive_home) -> None:
    init_db()
    export_dir = archive_home / "claude_export"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text(
        json.dumps([_CLAUDE_CONV]), encoding="utf-8"
    )
    assert import_claude_ai_export(export_dir).imported == 1
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude")).scalar_one()
        events = list(s.execute(select(Event).where(Event.thread_id == t.id)).scalars())
    use = next(e for e in events if e.event_type == "tool_use_complete")
    res = next(e for e in events if e.event_type == "tool_execution_completed")
    assert use.payload["tool_call_id"] == "toolu_1" == res.payload["tool_call_id"]
    assert "unpaired" not in use.payload and "unpaired" not in res.payload


# ── ChatGPT fixtures ─────────────────────────────────────────────────────────

_CHATGPT_CONV = {
    "id": "gconv-df",
    "title": "GPT Dropped Fields",
    "create_time": 1767261600.0,
    "update_time": 1767261610.0,
    "current_node": "a1",
    "gizmo_id": "g-abc",
    "default_model_slug": "gpt-5-2",
    "is_starred": True,
    "is_archived": False,
    "moderation_results": [],
    "safe_urls": ["https://ignored.example"],
    "plugin_ids": None,
    "mapping": {
        "root": {"id": "root", "parent": None, "children": ["u1"], "message": None},
        "u1": {"id": "u1", "parent": "root", "children": ["a1"], "message": {
            "id": "u1", "author": {"role": "user"}, "create_time": 1767261600.0,
            "content": {"content_type": "text", "parts": ["cite something"]},
            "status": "finished_successfully", "metadata": {},
        }},
        "a1": {"id": "a1", "parent": "u1", "children": [], "message": {
            "id": "a1", "author": {"role": "assistant"}, "create_time": 1767261605.0,
            "content": {"content_type": "text", "parts": ["cited answer"]},
            "status": "finished_successfully",
            "metadata": {
                "model_slug": "gpt-5-2",
                "citations": [{
                    "start_ix": 0, "end_ix": 5, "citation_format_type": "tether_og",
                    "metadata": {
                        "type": "webpage", "title": "A Page", "url": "https://cited.example",
                        "text": "BULKY full document text that is redundant here",
                    },
                }],
                "content_references": [{
                    "matched_text": "cite", "start_idx": 0, "end_idx": 5,
                    "type": "grouped_webpages", "alt": "([A Page](https://cited.example))",
                    "safe_urls": ["https://cited.example"],
                    "status": "done", "refs": [], "fallback_items": None,
                    "items": [{
                        "title": "A Page", "url": "https://cited.example",
                        "pub_date": None, "snippet": "a snippet", "attribution": "example",
                        "refs": [{"turn_index": 1}], "hue": None,
                    }],
                }],
                "canvas": {"textdoc_id": "doc-1", "version": 2, "title": "Doc"},
            },
        }},
    },
}


def test_chatgpt_metadata_annotations_reach_events() -> None:
    parser = ChatGPTParser()
    messages = parser.parse_export([json.loads(json.dumps(_CHATGPT_CONV))])
    assistant = next(m for m in messages if m["role"] == "assistant")
    ann = assistant["provider_data"]["annotations"]
    assert ann["citations"][0]["metadata"]["url"] == "https://cited.example"
    # Bulky fetched-document text is dropped from the compact citation.
    assert "text" not in ann["citations"][0]["metadata"]
    ref = ann["content_references"][0]
    assert ref["items"][0] == {
        "title": "A Page", "url": "https://cited.example",
        "snippet": "a snippet", "attribution": "example",
    }
    assert "status" not in ref and "fallback_items" not in ref
    assert ann["canvas"]["textdoc_id"] == "doc-1"

    builder = DefaultEventBuilder()
    events = []
    for m in messages:
        events.extend(builder.build_events(m, stream_id="s1"))
    completed = next(e for e in events if e.event_type == "api_request_completed")
    assert completed.payload["annotations"]["canvas"]["textdoc_id"] == "doc-1"
    assert completed.payload["annotations"]["citations"][0]["start_ix"] == 0


def test_chatgpt_tether_content_yields_real_text() -> None:
    assert extract_text_from_content({
        "content_type": "tether_browsing_display",
        "result": "fetched page content",
        "summary": "short summary",
    }) == "fetched page content"
    assert extract_text_from_content({
        "content_type": "tether_browsing_display",
        "summary": "only a summary",
    }) == "only a summary"
    # tether_quote keeps using its own text.
    assert extract_text_from_content({
        "content_type": "tether_quote", "text": "quoted words",
        "url": "https://q.example", "domain": "q.example",
    }) == "quoted words"


def test_chatgpt_tether_quote_block_carries_source_annotations() -> None:
    raw_msg = {
        "role": "assistant",
        "content_type": "tether_quote",
        "provider_message_id": "tq1",
        "msg_metadata": {},
        "content": {
            "content_type": "tether_quote", "text": "quoted words",
            "url": "https://q.example", "domain": "q.example", "title": "Quoted Page",
        },
    }
    blocks, _ = extract_content_blocks(raw_msg, 0)
    assert blocks[0]["type"] == "text" and blocks[0]["text"] == "quoted words"
    assert blocks[0]["annotations"] == {
        "url": "https://q.example", "domain": "q.example", "title": "Quoted Page",
    }


def test_chatgpt_tether_browsing_display_text_end_to_end() -> None:
    raw_msg = {
        "role": "assistant",
        "content_type": "tether_browsing_display",
        "provider_message_id": "tb1",
        "msg_metadata": {},
        "content": {"content_type": "tether_browsing_display", "result": "fetched content"},
    }
    blocks, _ = extract_content_blocks(raw_msg, 0)
    assert blocks[0]["type"] == "text" and blocks[0]["text"] == "fetched content"


def test_chatgpt_code_language_annotation() -> None:
    raw_msg = {
        "role": "assistant",
        "content_type": "code",
        "recipient": "python",
        "provider_message_id": "c1",
        "msg_metadata": {},
        "content": {
            "content_type": "code", "language": "python",
            "response_format_name": None, "text": "print(1)",
        },
    }
    blocks, _ = extract_content_blocks(raw_msg, 0)
    assert blocks[0]["type"] == "tool_use" and blocks[0]["input"] == {"code": "print(1)"}
    assert blocks[0]["annotations"] == {"language": "python"}

    # Without a recipient the code text becomes a text block, same annotation.
    raw_msg2 = dict(raw_msg, recipient=None)
    blocks2, _ = extract_content_blocks(raw_msg2, 0)
    assert blocks2[0]["type"] == "text"
    assert blocks2[0]["annotations"] == {"language": "python"}


def test_chatgpt_multimodal_assets_annotation() -> None:
    conv = json.loads(json.dumps(_CHATGPT_CONV))
    conv["mapping"]["u1"]["message"]["content"] = {
        "content_type": "multimodal_text",
        "parts": [
            "look at this",
            {"content_type": "image_asset_pointer", "asset_pointer": "sediment://x",
             "assets": [{"asset_pointer": "sediment://x", "kind": "image"}]},
        ],
    }
    messages = ChatGPTParser().parse_export([conv])
    user = next(m for m in messages if m["role"] == "user")
    assert user["provider_data"]["annotations"]["assets"] == [
        {"asset_pointer": "sediment://x", "kind": "image"}
    ]


def test_chatgpt_source_metadata_folds_conversation_metadata(archive_home) -> None:
    init_db()
    export_dir = archive_home / "chatgpt_export"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text(
        json.dumps([_CHATGPT_CONV]), encoding="utf-8"
    )
    (export_dir / "user.json").write_text(json.dumps({"id": "u"}), encoding="utf-8")
    assert import_chatgpt_export(export_dir).imported == 1
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "chatgpt")).scalar_one()
        meta = t.source_metadata or {}
    assert meta["provider"] == "chatgpt" and meta["surface"] == "web"
    assert meta["gizmo_id"] == "g-abc"
    assert meta["default_model_slug"] == "gpt-5-2"
    assert meta["is_starred"] is True
    assert meta["is_archived"] is False  # False is data, not emptiness
    # Noise keys stay out.
    assert "moderation_results" not in meta
    assert "safe_urls" not in meta
    assert "plugin_ids" not in meta
    assert "title" not in meta


def test_fold_conversation_metadata_handles_missing_and_empty() -> None:
    base = {"provider": "chatgpt", "surface": "web"}
    assert fold_conversation_metadata(base, []) == base
    assert fold_conversation_metadata(base, [{"conversation_metadata": None}]) == base
    folded = fold_conversation_metadata(
        base,
        [{"conversation_metadata": {
            "title": "T", "gizmo_id": None, "current_node": "n2", "is_archived": False,
        }}],
    )
    assert folded == {**base, "current_node": "n2", "is_archived": False}
