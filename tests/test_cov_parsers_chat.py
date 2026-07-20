"""Branch-coverage tests for the vendored ChatGPT/Claude export parsers and the
thinking-block validator.

These drive the parser entry points (`ChatGPTParser.parse_export`,
`ClaudeParser.parse_export`) plus the stateless helper bodies in
`chatgpt_content` and the shared scaffolding in `base`, and exercise the
`ThinkingBlockValidator` accept/reject branches directly against constructed
`ValidationContext`s. Each test asserts on a real parsed shape (block types,
content_text, coalescing, synthetic stubs) or a real validation outcome
(error/warning added), not merely line execution.

Coverage tag: pch.
"""

from __future__ import annotations

import pytest

from thread_archive._thread_import.parsers.base import (
    FieldMapping,
    ProviderParser,
)
from thread_archive._thread_import.parsers.chatgpt import (
    ChatGPTParser,
    _extract_message_order,
)
from thread_archive._thread_import.parsers.chatgpt_content import (
    append_image_parts,
    append_metadata_tool_calls,
    append_text_content,
    build_content_blocks,
    collect_coalesce_candidates,
    extract_content_blocks,
    extract_special_block,
    extract_text_from_content,
    is_primary_message,
)
from thread_archive._thread_import.parsers.claude import ClaudeParser, _extract_role
from thread_archive._thread_import.parsers.config import (
    CHATGPT_CONFIG,
    CLAUDE_CONFIG,
    ProviderConfig,
)
from thread_archive._thread_import.parsers.validators.base import ValidationContext
from thread_archive._thread_import.parsers.validators.thinking import (
    ThinkingBlockValidator,
)

# =============================================================================
# base.py — FieldMapping / ProviderParser scaffolding
# =============================================================================


def test_field_mapping_returns_default_when_extractor_yields_none():
    fm = FieldMapping(model_field="x", extractor=lambda d: None, default="DEF")
    assert fm.extract({}) == "DEF"


def test_field_mapping_required_extractor_error_propagates():
    fm = FieldMapping(model_field="x", extractor=lambda d: 1 / 0, required=True)
    with pytest.raises(ZeroDivisionError):
        fm.extract({})


def test_field_mapping_optional_extractor_error_returns_default():
    fm = FieldMapping(
        model_field="x", extractor=lambda d: 1 / 0, required=False, default="D"
    )
    assert fm.extract({}) == "D"


class _RaisingMapping:
    """Minimal FieldMapping-shaped object whose extract() always raises,
    to reach apply_field_mappings' own except handler."""

    def __init__(self, required: bool, default=None, model_field="f"):
        self.required = required
        self.default = default
        self.model_field = model_field

    def extract(self, raw):
        raise RuntimeError("boom")


def test_apply_field_mappings_defaults_to_class_mappings():
    # No explicit mappings -> uses self.FIELD_MAPPINGS (empty on ProviderParser).
    parser = ChatGPTParser()
    assert parser.apply_field_mappings({"anything": 1}) == {}


def test_apply_field_mappings_required_failure_raises_valueerror():
    parser = ChatGPTParser()
    with pytest.raises(ValueError, match="Failed to extract required field 'f'"):
        parser.apply_field_mappings({}, [_RaisingMapping(required=True)])


def test_apply_field_mappings_optional_failure_uses_default():
    parser = ChatGPTParser()
    out = parser.apply_field_mappings(
        {}, [_RaisingMapping(required=False, default="fallback")]
    )
    assert out == {"f": "fallback"}


def test_hash_message_int_and_integer_float_hash_identically():
    as_int = ProviderParser.hash_message("id", "user", "c", 1700000000)
    as_float = ProviderParser.hash_message("id", "user", "c", 1700000000.0)
    assert as_int == as_float
    assert len(as_int) == 64


def test_hash_message_non_integer_float_kept_as_is():
    # Non-integer float skips the int-normalization branch; still hashes cleanly.
    h = ProviderParser.hash_message("id", "user", "c", 1700000000.5)
    assert isinstance(h, str) and len(h) == 64
    assert h != ProviderParser.hash_message("id", "user", "c", 1700000000)


def test_extract_text_from_blocks_includes_system_context_skips_empty():
    blocks = [
        {"type": "text", "text": ""},  # empty -> skipped
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Bash"},  # non-text -> ignored
        {"type": "system_context", "text": "custom ctx"},
        {"type": "system_context", "text": ""},  # empty -> skipped
    ]
    assert ProviderParser.extract_text_from_blocks(blocks) == "hello\n\ncustom ctx"


def test_normalize_role_variants():
    assert ProviderParser.normalize_role("user") == "user"
    assert ProviderParser.normalize_role("Human") == "user"
    assert ProviderParser.normalize_role("assistant") == "assistant"
    assert ProviderParser.normalize_role("system") == "system"
    assert ProviderParser.normalize_role("Tool") == "tool"
    assert ProviderParser.normalize_role(None, sender="human") == "user"
    assert ProviderParser.normalize_role(None, sender="assistant") == "assistant"
    assert ProviderParser.normalize_role(None, sender="robot") == "unknown"
    assert ProviderParser.normalize_role("weirdrole") == "weirdrole"


def test_factory_blocks_carry_optional_kwargs_and_drop_none():
    tb = ProviderParser.create_text_block(
        "t", 3, provider_block_id="pb", start_timestamp=None
    )
    assert tb == {"type": "text", "text": "t", "seq": 3, "provider_block_id": "pb"}
    assert "start_timestamp" not in tb  # None-valued kwargs dropped

    tu = ProviderParser.create_tool_use_block(
        "Edit", {"a": 1}, 4, provider_message_id="mid", provider_tool_name="edit_v2"
    )
    assert tu["type"] == "tool_use"
    assert tu["provider_message_id"] == "mid"
    assert tu["provider_tool_name"] == "edit_v2"

    tr = ProviderParser.create_tool_result_block(
        "Bash", "out", 5, is_error=True, provider_message_id="mid", display_content="d"
    )
    assert tr["type"] == "tool_result"
    assert tr["is_error"] is True
    assert tr["provider_message_id"] == "mid"
    assert tr["display_content"] == "d"

    th = ProviderParser.create_thinking_block(
        "pondering", 6, thinking_type="analysis", provider_block_id="pb2"
    )
    assert th["type"] == "thinking"
    assert th["thinking_type"] == "analysis"
    assert th["provider_block_id"] == "pb2"

    # No thinking_type and a None-valued kwarg -> neither is written.
    plain = ProviderParser.create_thinking_block("t", 0, provider_block_id=None)
    assert plain == {"type": "thinking", "text": "t", "seq": 0}


# =============================================================================
# chatgpt_content.py — pure helper bodies
# =============================================================================


def test_extract_text_from_content_string_dict_parts_and_other():
    assert extract_text_from_content("plain") == "plain"
    # A dict with a string `text` field returns it directly.
    assert extract_text_from_content({"text": "direct"}) == "direct"
    # parts array: strings and {text:...} dicts join; other shapes ignored.
    assert (
        extract_text_from_content({"parts": ["a", {"text": "b"}, {"x": 1}, 42]})
        == "a\n\nb"
    )
    assert extract_text_from_content(42) == ""


def test_extract_special_block_stub_returns_empty():
    assert extract_special_block({"is_stub": True}, {}, "", "", "id", 7) == ([], 7)


def test_extract_special_block_tool_role_makes_tool_result():
    blocks, nxt = extract_special_block(
        {"author_name": "python"}, "result-data", "", "tool", "mid", 0
    )
    assert nxt == 1
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["name"] == "python"
    assert blocks[0]["content"] == "result-data"
    # missing author_name -> "unknown"
    blocks2, _ = extract_special_block({}, "r", "", "tool", None, 0)
    assert blocks2[0]["name"] == "unknown"


def test_extract_special_block_thinking_and_empty_thinking():
    blocks, nxt = extract_special_block(
        {}, {"parts": ["deep thought"]}, "thoughts", "assistant", "mid", 2
    )
    assert nxt == 3
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["thinking_type"] == "thoughts"
    # empty thinking text falls through (no block produced, no other case matches)
    assert extract_special_block({}, {"parts": []}, "thoughts", "assistant", "m", 0) is None


def test_extract_special_block_system_context():
    blocks, nxt = extract_special_block(
        {}, {"parts": ["custom instr"]}, "user_editable_context", "user", "mid", 0
    )
    assert nxt == 1
    assert blocks[0]["type"] == "system_context"
    assert blocks[0]["context_type"] == "user_editable_context"
    # Context content type but empty text -> no block, falls through.
    assert (
        extract_special_block({}, {"parts": []}, "model_editable_context", "user", "m", 0)
        is None
    )


def test_extract_special_block_recipient_tool_call():
    blocks, nxt = extract_special_block(
        {"recipient": "python"}, {"parts": ["print(1)"]}, "text", "assistant", "mid", 0
    )
    assert nxt == 1
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["name"] == "python"
    assert blocks[0]["input"] == {"code": "print(1)"}
    # recipient with no text -> empty input dict
    blocks2, _ = extract_special_block(
        {"recipient": "browser"}, {"parts": []}, "", "assistant", "mid", 0
    )
    assert blocks2[0]["input"] == {}


def test_extract_special_block_returns_none_for_plain_text():
    assert extract_special_block({}, {"parts": ["x"]}, "text", "user", "m", 0) is None


def test_append_metadata_tool_calls_list_and_dict_and_function_nesting():
    blocks = []
    seq = append_metadata_tool_calls(
        blocks, {"tool_calls": [{"name": "foo", "args": {"a": 1}}]}, "m", 0
    )
    assert seq == 1 and blocks[0]["name"] == "foo" and blocks[0]["input"] == {"a": 1}

    blocks = []
    append_metadata_tool_calls(
        blocks,
        {"tool_calls": [{"function": {"name": "bar", "arguments": "{}"}}, "not-a-dict"]},
        "m",
        0,
    )
    assert len(blocks) == 1 and blocks[0]["name"] == "bar"

    blocks = []
    seq = append_metadata_tool_calls(blocks, {"tool_call": {"name": "baz"}}, "m", 5)
    assert seq == 6 and blocks[0]["name"] == "baz"

    blocks = []
    append_metadata_tool_calls(
        blocks, {"tool_call": {"function": {"name": "qux", "arguments": "a"}}}, "m", 0
    )
    assert blocks[0]["name"] == "qux" and blocks[0]["input"] == "a"

    # No tool calls -> nothing appended, seq unchanged.
    blocks = []
    assert append_metadata_tool_calls(blocks, {}, "m", 9) == 9 and blocks == []

    # Truthy tool_calls that is neither list nor dict -> ignored, seq unchanged.
    blocks = []
    assert append_metadata_tool_calls(blocks, {"tool_calls": "weird"}, "m", 3) == 3
    assert blocks == []


def test_append_text_content_appends_only_when_text_present():
    blocks = []
    assert append_text_content(blocks, {"parts": []}, 0) == 0 and blocks == []
    seq = append_text_content(blocks, {"parts": ["hi"]}, 0)
    assert seq == 1 and blocks[0]["text"] == "hi"


def test_append_image_parts_shapes():
    # Non-dict content -> no-op.
    assert append_image_parts([], "not-a-dict", 4) == 4

    blocks = []
    seq = append_image_parts(
        blocks,
        {
            "parts": [
                {
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "file-service://x",
                    "size_bytes": 10,
                    "width": 5,
                    "height": 6,
                },
                {"content_type": "image/png", "asset_pointer": "p"},
                {"asset_pointer": "sediment://y"},  # image via bare pointer
                {"content_type": "text"},  # not an image -> skipped
                "str-part",  # non-dict -> skipped
                42,
            ]
        },
        0,
    )
    assert seq == 3
    assert blocks[0]["type"] == "image"
    assert blocks[0]["asset_pointer"] == "file-service://x"
    assert blocks[0]["metadata"] == {"size_bytes": 10, "width": 5, "height": 6}
    assert blocks[1]["mime_type"] == "image/png"
    assert blocks[2]["asset_pointer"] == "sediment://y"


def test_build_content_blocks_merges_primary_and_coalesced():
    blocks = build_content_blocks(
        {"content": {"parts": ["primary"]}, "content_type": "text", "role": "user"},
        [{"content": {"parts": ["child"]}, "content_type": "text", "role": "user"}],
    )
    texts = [b["text"] for b in blocks if b["type"] == "text"]
    assert texts == ["primary", "child"]
    # seq is monotonic across primary + coalesced.
    assert [b["seq"] for b in blocks] == [0, 1]


def test_extract_content_blocks_additive_sections():
    raw = {
        "content": {"content_type": "text", "parts": ["body"]},
        "content_type": "text",
        "role": "assistant",
        "provider_message_id": "m",
        "msg_metadata": {"tool_calls": [{"name": "T", "args": {}}]},
    }
    blocks, seq = extract_content_blocks(raw, 0)
    kinds = [b["type"] for b in blocks]
    assert "tool_use" in kinds and "text" in kinds
    assert seq == len(blocks)


def test_is_primary_message_classification():
    assert is_primary_message({"is_stub": True}) is False
    assert is_primary_message({"role": "tool"}) is False
    assert is_primary_message({"content_type": "thoughts"}) is False
    assert is_primary_message({"content_type": "user_editable_context"}) is False
    assert is_primary_message({"content_type": "metadata"}) is False
    # Empty content dict -> nothing to display.
    assert is_primary_message({"role": "user", "content": {"parts": [], "text": ""}}) is False
    # String content is displayable; user role -> primary.
    assert is_primary_message({"role": "user", "content": "hi"}) is True
    assert is_primary_message({"role": "assistant", "content": {"parts": ["x"]}}) is True
    # Displayable content but a non-standard role -> not primary.
    assert is_primary_message({"role": "weird", "content": {"parts": ["x"]}}) is False


def test_collect_coalesce_candidates_branches():
    # Child id absent from lookup -> skipped.
    assert collect_coalesce_candidates("p", {"p": ["missing"]}, {}, {"missing"}) == []
    # Child not on active path -> skipped.
    assert collect_coalesce_candidates("p", {"p": ["c"]}, {"c": {"role": "tool"}}, set()) == []
    # Recursive coalesce of tool descendants on the active path.
    got = collect_coalesce_candidates(
        "p",
        {"p": ["c"], "c": ["gc"]},
        {"c": {"role": "tool"}, "gc": {"role": "tool"}},
        {"c", "gc"},
    )
    assert got == ["c", "gc"]
    # Active-path child that is a normal assistant turn -> not coalesced.
    assert collect_coalesce_candidates(
        "p", {"p": ["c"]}, {"c": {"role": "assistant", "content_type": "text"}}, {"c"}
    ) == []


# =============================================================================
# chatgpt.py — parse_export entry point + delegators
# =============================================================================


def test_extract_message_order_non_numeric_and_overflow():
    assert _extract_message_order({}) is None
    assert _extract_message_order({"create_time": "nope"}) is None
    assert _extract_message_order({"create_time": float("inf")}) is None
    assert _extract_message_order({"create_time": 100}) == 100_000_000


CONV_RICH = {
    "id": "conv-rich",
    "title": "Rich",
    "current_node": "tool1",
    "mapping": {
        "root": {"id": "root", "parent": None, "children": ["u1"], "message": None},
        "u1": {
            "id": "u1",
            "parent": "root",
            "children": ["a1"],
            "message": {
                "author": {"role": "user"},
                "create_time": 100.0,
                "content": {"content_type": "text", "parts": ["question?"]},
            },
        },
        "a1": {
            "id": "a1",
            "parent": "u1",
            "children": ["tool1"],
            "message": {
                "author": {"role": "assistant"},
                "create_time": 101.0,
                "metadata": {"model_slug": "o1"},
                "content": {"content_type": "text", "parts": ["the answer"]},
            },
        },
        "tool1": {
            "id": "tool1",
            "parent": "a1",
            "children": [],
            "message": {
                "author": {"role": "tool", "name": "python"},
                "create_time": 102.0,
                "content": {"content_type": "execution_output", "text": "42"},
            },
        },
    },
}


def test_parse_export_coalesces_tool_into_assistant():
    # Bundled {"conversations": [...]} form.
    msgs = ChatGPTParser().parse_export({"conversations": [CONV_RICH]})
    by_id = {m["provider_message_id"]: m for m in msgs}
    # tool1 was coalesced into a1, so it is not a standalone row.
    assert "tool1" not in by_id
    a1 = by_id["a1"]
    kinds = [b["type"] for b in a1["content_blocks"]]
    assert "text" in kinds and "tool_result" in kinds
    assert a1["content_text"] == "the answer"
    result_block = next(b for b in a1["content_blocks"] if b["type"] == "tool_result")
    # The tool_result preserves the raw content object verbatim.
    assert result_block["content"] == {"content_type": "execution_output", "text": "42"}
    # The stub root and the two primaries remain.
    assert by_id["u1"]["role"] == "user"


CONV_STUB = {
    "id": "conv-b",
    "title": "B",
    "current_node": "m2",
    "mapping": {
        "m1": {
            "id": "m1",
            "parent": "ghost",  # references a node absent from mapping
            "children": ["m2", "m3"],
            "message": {
                "author": {"role": "user"},
                "create_time": 200.0,
                "content": "raw string content",  # non-dict content
                "metadata": {"content_type": "text"},
            },
        },
        "m2": {
            "id": "m2",
            "parent": "m1",
            "children": [],
            "message": {
                "author": {"role": "assistant"},
                "create_time": 210.0,
                "content": {"content_type": "text", "parts": ["reply"]},
            },
        },
        "m3": {
            "id": "m3",
            "parent": "m1",
            "children": [],
            "message": {
                "author": {"role": "assistant"},
                "create_time": 220.0,
                "content": {"content_type": "text", "parts": ["reply2"]},
            },
        },
    },
}


def test_parse_export_synthesizes_missing_parent_stub():
    # Single-conversation dict (no "conversations" key) -> treated as one conv.
    msgs = ChatGPTParser().parse_export(CONV_STUB)
    by_id = {m["provider_message_id"]: m for m in msgs}
    # A synthetic stub row stands in for the missing "ghost" parent.
    ghost = by_id["ghost"]
    assert ghost["provider_data"]["synthetic_stub"] is True
    assert ghost["content_blocks"] == []
    # String content on m1 became a text block.
    assert by_id["m1"]["content_text"] == "raw string content"


def test_parse_export_list_form_and_empty():
    assert ChatGPTParser().parse_export([]) == []
    msgs = ChatGPTParser().parse_export([CONV_RICH])
    assert any(m["provider_message_id"] == "a1" for m in msgs)


def test_chatgpt_thin_delegators_and_timestamp_helpers():
    p = ChatGPTParser()
    raw = {
        "content": {"content_type": "text", "parts": ["x"]},
        "content_type": "text",
        "role": "user",
        "provider_message_id": "m",
        "msg_metadata": {},
    }
    blocks, seq = p._extract_content_blocks(raw, 0)
    assert blocks and seq == len(blocks)

    assert p._extract_special_block({"is_stub": True}, {}, "", "", "m", 0) == ([], 0)

    acc = []
    assert p._append_metadata_tool_calls(acc, {"tool_calls": [{"name": "f"}]}, "m", 0) == 1
    acc2 = []
    assert p._append_text_content(acc2, {"parts": ["t"]}, 0) == 1
    acc3 = []
    assert (
        p._append_image_parts(
            acc3, {"parts": [{"content_type": "image/png", "asset_pointer": "p"}]}, 0
        )
        == 1
    )
    assert p._extract_text_from_content("hello") == "hello"

    assert p._timestamp_to_iso(None) is None
    assert p._timestamp_to_iso(1609459200).startswith("2021-01-01T00:00:00")
    assert p._timestamp_to_order(None) is None
    assert p._timestamp_to_order("x") is None
    assert p._timestamp_to_order(float("inf")) is None
    assert p._timestamp_to_order(1.0) == 1_000_000


def test_parse_export_sibling_min_create_time_tracked():
    # m2 (210) then m3 (220) share parent m1; the second must not lower the
    # tracked minimum. Import succeeds and both children become rows.
    msgs = ChatGPTParser().parse_export(CONV_STUB)
    ids = {m["provider_message_id"] for m in msgs}
    assert {"m2", "m3"} <= ids


# =============================================================================
# claude.py — parse_export + block handlers + attachments
# =============================================================================

CLAUDE_CONV = [
    {
        "uuid": "conv-1",
        "name": "Test",
        "summary": "sum",
        "account": {"uuid": "acc-1"},
        "project": {"uuid": "proj-1"},
        "chat_messages": [
            {
                "uuid": "m1",
                "sender": "human",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:01Z",
                "text": "",
                "content": [
                    {
                        "type": "text",
                        "text": "hi there",
                        "start_timestamp": "2026-01-01T00:00:00Z",
                    }
                ],
                "attachments": [
                    {"file_name": "doc.txt", "extracted_content": "body"},
                    "not-a-dict",
                ],
                "files": [{"file_name": "img.png"}, 42],
            },
            {
                "uuid": "m2",
                "sender": "assistant",
                "created_at": "2026-01-01T00:00:05Z",
                "content": [
                    {"type": "thinking", "thinking": "pondering", "signature": "sig"},
                    {"type": "thinking", "thinking": ""},  # empty -> dropped
                    {"type": "text", "text": "answer"},
                    {"type": "text", "text": ""},  # empty -> dropped
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    {
                        "type": "tool_result",
                        "name": "Bash",
                        "content": "out",
                        "is_error": False,
                    },
                    {"type": "token_budget", "budget": 100},
                    {"type": "mystery", "text": "kept as text"},  # unknown -> text
                    {"type": "mystery2", "content": "also text"},  # unknown -> text
                    {"type": "mystery3", "foo": "bar"},  # unknown, no text -> dropped
                ],
            },
            {
                "uuid": "m3",
                "sender": "human",
                "content": [{"type": "token_budget", "x": 1}],
                "text": "fallback text",  # blocks yield no text -> use fallback
            },
        ],
    }
]

CLAUDE_BUNDLE = {
    "conversations": CLAUDE_CONV,
    "memories": {"project_memories": {"proj-1": "remembered"}},
    "projects": [{"uuid": "proj-1", "name": "Proj", "description": "desc"}],
    "users": [{"uuid": "acc-1", "full_name": "Sam"}],
}


def test_claude_parse_export_bundle_all_block_types():
    msgs = ClaudeParser().parse_export(CLAUDE_BUNDLE)
    by_id = {m["provider_message_id"]: m for m in msgs}
    assert len(msgs) == 3

    m1 = by_id["m1"]
    assert m1["role"] == "user"
    assert m1["content_text"] == "hi there"
    kinds1 = [b["type"] for b in m1["content_blocks"]]
    assert "attachment" in kinds1 and "file" in kinds1
    att = next(b for b in m1["content_blocks"] if b["type"] == "attachment")
    assert att["file_name"] == "doc.txt"
    # Non-dict attachment/file entries were skipped (exactly one of each).
    assert kinds1.count("attachment") == 1 and kinds1.count("file") == 1

    m2 = by_id["m2"]
    kinds2 = [b["type"] for b in m2["content_blocks"]]
    assert kinds2.count("thinking") == 1  # empty thinking dropped
    assert kinds2.count("text") == 3  # "answer" + two unknown-as-text; empty dropped
    assert "tool_use" in kinds2 and "tool_result" in kinds2
    assert "system_metadata" in kinds2  # token_budget preserved
    think = next(b for b in m2["content_blocks"] if b["type"] == "thinking")
    assert think["text"] == "pondering" and think["signature"] == "sig"

    # Enrichment from the bundle lookups.
    assert m2["conversation_metadata"]["project_name"] == "Proj"
    assert m2["conversation_metadata"]["project_memory"] == "remembered"
    assert m2["conversation_metadata"]["account_name"] == "Sam"

    # Fallback text used when content blocks produce no display text.
    assert by_id["m3"]["content_text"] == "fallback text"


def test_claude_parse_export_list_without_enrichment():
    # Plain list form: no projects/users maps, project present but unenriched.
    msgs = ClaudeParser().parse_export(CLAUDE_CONV)
    meta = msgs[0]["conversation_metadata"]
    assert meta["project_uuid"] == "proj-1"
    assert meta["project_name"] is None
    assert meta["project_memory"] == ""
    assert meta["account_name"] is None


def test_claude_parse_export_non_list_non_dict_is_empty():
    assert ClaudeParser().parse_export(None) == []


def test_claude_parse_export_fallback_text_when_no_blocks():
    conv = [
        {
            "uuid": "c",
            "chat_messages": [
                {"uuid": "only", "sender": "assistant", "text": "just text, no blocks"}
            ],
        }
    ]
    msgs = ClaudeParser().parse_export(conv)
    assert msgs[0]["content_text"] == "just text, no blocks"
    assert msgs[0]["content_blocks"][0]["type"] == "text"


def test_claude_conversation_without_project_or_account():
    conv = [{"uuid": "c2", "chat_messages": [{"uuid": "x", "sender": "human", "text": "hi"}]}]
    msgs = ClaudeParser().parse_export(conv)
    meta = msgs[0]["conversation_metadata"]
    assert meta["project_uuid"] is None
    assert meta["account_uuid"] is None


def test_claude_extract_role_unknown_sender():
    assert _extract_role({"sender": "robot"}) == "robot"
    assert _extract_role({}) == "unknown"


def test_claude_thinking_block_without_signature():
    conv = [
        {
            "uuid": "c",
            "chat_messages": [
                {
                    "uuid": "u",
                    "sender": "assistant",
                    "content": [{"type": "thinking", "thinking": "no sig here"}],
                }
            ],
        }
    ]
    msgs = ClaudeParser().parse_export(conv)
    think = msgs[0]["content_blocks"][0]
    assert think["type"] == "thinking" and think["text"] == "no sig here"
    assert "signature" not in think


def test_claude_unknown_block_from_content_field():
    # Unknown block type with only a `content` string is preserved as text.
    conv = [
        {
            "uuid": "c",
            "chat_messages": [
                {
                    "uuid": "u",
                    "sender": "assistant",
                    "content": [{"type": "weird", "content": "recovered"}],
                }
            ],
        }
    ]
    msgs = ClaudeParser().parse_export(conv)
    assert msgs[0]["content_blocks"][0]["text"] == "recovered"


# =============================================================================
# validators/thinking.py — accept/reject branches
# =============================================================================


def _assistant_msg(thinking: bool = False, model=None):
    if thinking:
        blocks = [{"type": "thinking", "text": "t", "seq": 0}]
    else:
        blocks = [{"type": "text", "text": "hi", "seq": 0}]
    provider_data = {"message": {}}
    if model:
        provider_data = {"message": {"metadata": {"model_slug": model}}}
    return {"role": "assistant", "content_blocks": blocks, "provider_data": provider_data}


def _ctx(conv_id="conv-1", provider="p", strict=False):
    return ValidationContext(conversation_id=conv_id, source_provider=provider, strict=strict)


def _run(config, messages, ctx):
    ThinkingBlockValidator(config).validate(messages, ctx)
    return ctx


# The "never" path needs a dedicated config: CLAUDE_CONFIG is "optional" (claude.ai
# exports carry thinking blocks for reasoning models).
NEVER_CONFIG = ProviderConfig(provider_name="never", thinking_expectation="never")


def test_thinking_never_flags_present_thinking():
    ctx = _run(NEVER_CONFIG, [_assistant_msg(thinking=True)], _ctx(provider="never"))
    assert ctx.has_errors
    assert "NEVER" in ctx.errors[0]


def test_thinking_never_ok_without_thinking():
    ctx = _run(NEVER_CONFIG, [_assistant_msg(thinking=False)], _ctx(provider="never"))
    assert not ctx.has_errors


def test_claude_config_thinking_optional():
    # claude.ai exports include thinking for reasoning models; present thinking
    # must not error.
    ctx = _run(CLAUDE_CONFIG, [_assistant_msg(thinking=True)], _ctx(provider="claude"))
    assert not ctx.has_errors


def test_thinking_no_assistant_messages_is_noop():
    ctx = _run(
        CLAUDE_CONFIG,
        [{"role": "user", "content_blocks": [], "provider_data": {}}],
        _ctx(),
    )
    assert not ctx.has_errors and not ctx.warnings


REQUIRED_CONFIG = ProviderConfig(provider_name="req", thinking_expectation="required")


def test_thinking_required_zero_is_error():
    ctx = _run(REQUIRED_CONFIG, [_assistant_msg(thinking=False)], _ctx())
    assert ctx.has_errors and "MUST have thinking" in ctx.errors[0]


def test_thinking_required_partial_is_warning():
    ctx = _run(
        REQUIRED_CONFIG,
        [_assistant_msg(thinking=True), _assistant_msg(thinking=False)],
        _ctx(),
    )
    assert not ctx.has_errors
    assert ctx.warnings and "50%" in ctx.warnings[0]


def test_thinking_required_subagent_is_exempt():
    ctx = _run(REQUIRED_CONFIG, [_assistant_msg(thinking=False)], _ctx(conv_id="agent-x"))
    assert not ctx.has_errors and not ctx.warnings


def test_thinking_required_full_is_clean():
    ctx = _run(REQUIRED_CONFIG, [_assistant_msg(thinking=True)], _ctx())
    assert not ctx.has_errors and not ctx.warnings


def test_thinking_model_specific_reasoning_model_missing_is_error():
    ctx = _run(CHATGPT_CONFIG, [_assistant_msg(thinking=False, model="o1")], _ctx())
    assert ctx.has_errors
    assert "thinking-capable" in ctx.errors[0]


def test_thinking_model_specific_partial_is_warning():
    ctx = _run(
        CHATGPT_CONFIG,
        [
            _assistant_msg(thinking=True, model="o1"),
            _assistant_msg(thinking=False, model="o1"),
        ],
        _ctx(),
    )
    assert not ctx.has_errors
    assert ctx.warnings and "50%" in ctx.warnings[0]


def test_thinking_model_specific_full_is_clean():
    ctx = _run(CHATGPT_CONFIG, [_assistant_msg(thinking=True, model="o1")], _ctx())
    assert not ctx.has_errors and not ctx.warnings


def test_thinking_model_specific_exempt_model_is_noop():
    ctx = _run(CHATGPT_CONFIG, [_assistant_msg(thinking=False, model="gpt-4o")], _ctx())
    assert not ctx.has_errors and not ctx.warnings


def test_thinking_model_specific_subagent_is_exempt():
    ctx = _run(
        CHATGPT_CONFIG,
        [_assistant_msg(thinking=False, model="o1")],
        _ctx(conv_id="agent-y"),
    )
    assert not ctx.has_errors and not ctx.warnings


OPTIONAL_CONFIG = ProviderConfig(provider_name="opt", thinking_expectation="optional")


def test_thinking_optional_never_validates():
    ctx = _run(OPTIONAL_CONFIG, [_assistant_msg(thinking=False)], _ctx())
    assert not ctx.has_errors and not ctx.warnings
