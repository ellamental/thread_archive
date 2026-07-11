"""The ChatGPT tool-coalescing transform: separate tool/thinking/context messages
become content_blocks on their primary parent, and nothing else moves.

ChatGPT exports a turn as a *chain* of messages linked by parent_id — thinking,
tool call (structural), tool result — with the displayable user/assistant turns as
the only "primary" rows. ``ToolCoalescingTransformer`` folds each chain into
content_blocks on the primary that heads it and drops the folded rows from the
output. These tests pin that contract on realistic event sequences:

* what gets merged (tool role, thinking/context/structural content types, stubs —
  active-path only) and what stays separate (primaries, branch messages,
  non-coalescible orphans);
* output ordering (message_order) and block ordering (primary's own blocks first,
  then the chain in walk order, ``seq`` contiguous from 0);
* the block shapes produced per source kind (tool_result / thinking /
  system_context / tool_use-from-metadata / text / image);
* edge cases: empty input, single message, interleaved multi-turn chains,
  empty-thinking fall-through, stub children.

The transformer mutates and returns the same RawMessage objects; identity is by
provider_message_id.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from thread_archive._thread_import.parsers.pipeline.interfaces import RawMessage
from thread_archive._thread_import.parsers.transformers import ToolCoalescingTransformer


def _msg(
    msg_id: str,
    role: Optional[str] = None,
    *,
    parent: Optional[str] = None,
    content: Any = None,
    content_type: Optional[str] = None,
    order: Optional[int] = None,
    active: bool = True,
    stub: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> RawMessage:
    return RawMessage(
        provider_message_id=msg_id,
        provider_conversation_id="conv-1",
        provider_parent_id=parent,
        role=role,
        content=content,
        content_type=content_type,
        message_order=order,
        is_active_path=active,
        is_stub=stub,
        metadata=metadata or {},
    )


def _ids(messages: List[RawMessage]) -> List[str]:
    return [m.provider_message_id for m in messages]


def _turn_chain() -> List[RawMessage]:
    """A realistic ChatGPT tool turn: user → thinking → tool call → tool result →
    final assistant, each the parent of the next."""
    return [
        _msg("u1", "user", content={"parts": ["What's the weather in Berlin?"]}, order=0),
        _msg("think", "assistant", parent="u1", content={"text": "I should call the weather tool."},
             content_type="thoughts", order=1),
        _msg("call", "assistant", parent="think", content={"parts": []}, content_type="code", order=2,
             metadata={"tool_calls": [{"name": "get_weather", "args": {"city": "Berlin"}}]}),
        _msg("result", "tool", parent="call", content={"text": "22C, sunny"}, order=3,
             metadata={"author_name": "get_weather"}),
        _msg("a1", "assistant", parent="result", content={"parts": ["It's 22C and sunny in Berlin."]}, order=4),
    ]


# ── the flagship contract: a full tool turn folds onto its primaries ────────────
def test_tool_turn_chain_coalesces_into_primaries() -> None:
    out = ToolCoalescingTransformer().transform(_turn_chain())

    # Only the two primaries survive as rows; the chain between them is folded.
    assert _ids(out) == ["u1", "a1"]

    u1 = out[0]
    kinds = [b["type"] for b in u1.content_blocks]
    assert kinds == ["text", "thinking", "tool_use", "tool_result"]

    text, thinking, tool_use, tool_result = u1.content_blocks
    assert text["text"] == "What's the weather in Berlin?"
    assert thinking["text"] == "I should call the weather tool."
    assert thinking["thinking_type"] == "thoughts"
    assert thinking["provider_message_id"] == "think"
    assert tool_use["name"] == "get_weather"
    assert tool_use["input"] == {"city": "Berlin"}
    assert tool_use["provider_message_id"] == "call"
    assert tool_result["name"] == "get_weather"  # from tool metadata author_name
    assert tool_result["content"] == {"text": "22C, sunny"}  # raw content, verbatim
    assert tool_result["provider_message_id"] == "result"

    # seq is contiguous from 0 across primary-own + coalesced blocks.
    assert [b["seq"] for b in u1.content_blocks] == [0, 1, 2, 3]

    # The final assistant keeps only its own text.
    a1 = out[1]
    assert [b["type"] for b in a1.content_blocks] == ["text"]
    assert a1.content_blocks[0]["text"] == "It's 22C and sunny in Berlin."


def test_multi_turn_interleaved_chains_fold_onto_their_own_turn() -> None:
    """Two exchanges in one thread: each chain folds onto the primary that heads
    it, never onto a neighbour's turn."""
    messages = _turn_chain() + [
        _msg("u2", "user", parent="a1", content={"parts": ["and tomorrow?"]}, order=5),
        _msg("think2", "assistant", parent="u2", content={"text": "Forecast lookup."},
             content_type="thoughts", order=6),
        _msg("a2", "assistant", parent="think2", content={"parts": ["Rain tomorrow."]}, order=7),
    ]
    out = ToolCoalescingTransformer().transform(messages)

    assert _ids(out) == ["u1", "a1", "u2", "a2"]
    by_id = {m.provider_message_id: m for m in out}
    assert [b["type"] for b in by_id["u1"].content_blocks] == ["text", "thinking", "tool_use", "tool_result"]
    assert [b["type"] for b in by_id["u2"].content_blocks] == ["text", "thinking"]
    assert by_id["u2"].content_blocks[1]["text"] == "Forecast lookup."
    assert [b["type"] for b in by_id["a2"].content_blocks] == ["text"]


# ── edges: empty / single / ordering ─────────────────────────────────────────────
def test_empty_input_returns_empty() -> None:
    assert ToolCoalescingTransformer().transform([]) == []


def test_single_primary_message_gets_its_own_text_block() -> None:
    out = ToolCoalescingTransformer().transform(
        [_msg("u1", "user", content={"parts": ["hello"]}, order=0)]
    )
    assert _ids(out) == ["u1"]
    assert out[0].content_blocks == [{"type": "text", "text": "hello", "seq": 0}]


def test_output_is_sorted_by_message_order_regardless_of_input_order() -> None:
    messages = [
        _msg("b", "assistant", content={"parts": ["second"]}, order=2),
        _msg("c", "user", content={"parts": ["third"]}, order=3),
        _msg("a", "user", content={"parts": ["first"]}, order=1),
        _msg("z", "user", content={"parts": ["unordered"]}, order=None),  # None sorts as 0
    ]
    out = ToolCoalescingTransformer().transform(messages)
    assert _ids(out) == ["z", "a", "b", "c"]


# ── what stays separate ───────────────────────────────────────────────────────────
def test_branch_messages_are_not_coalesced(  # noqa: D103 - name is the contract
) -> None:
    """A thinking child off the active path stays a separate row (branches are
    preserved, not folded into the active turn)."""
    messages = [
        _msg("u1", "user", content={"parts": ["hi"]}, order=0),
        _msg("branch-think", "assistant", parent="u1", content={"text": "abandoned branch"},
             content_type="thoughts", order=1, active=False),
    ]
    out = ToolCoalescingTransformer().transform(messages)
    assert _ids(out) == ["u1", "branch-think"]
    assert [b["type"] for b in out[0].content_blocks] == ["text"]  # nothing folded in


def test_orphan_tool_message_is_kept_as_separate_row() -> None:
    """A coalesce-candidate whose parent is not a primary (or missing) is never
    folded — it survives as its own row rather than being dropped."""
    messages = [
        _msg("u1", "user", content={"parts": ["hi"]}, order=0),
        _msg("orphan", "tool", parent="not-a-known-id", content={"text": "stray output"}, order=1),
    ]
    out = ToolCoalescingTransformer().transform(messages)
    assert _ids(out) == ["u1", "orphan"]


def test_chain_walk_stops_at_the_next_primary() -> None:
    """Recursion through a chain halts at a primary — the downstream turn's chain
    is not stolen by the upstream primary."""
    messages = [
        _msg("u1", "user", content={"parts": ["q"]}, order=0),
        _msg("think", "assistant", parent="u1", content={"text": "t"}, content_type="thoughts", order=1),
        _msg("a1", "assistant", parent="think", content={"parts": ["answer"]}, order=2),
        # a1's own child chain — must fold onto a1, not u1
        _msg("think2", "assistant", parent="a1", content={"text": "post-answer reflection"},
             content_type="thoughts", order=3),
    ]
    out = ToolCoalescingTransformer().transform(messages)
    assert _ids(out) == ["u1", "a1"]
    assert [b["type"] for b in out[0].content_blocks] == ["text", "thinking"]
    assert out[0].content_blocks[1]["text"] == "t"
    assert [b["type"] for b in out[1].content_blocks] == ["text", "thinking"]
    assert out[1].content_blocks[1]["text"] == "post-answer reflection"


# ── block shapes per source kind ─────────────────────────────────────────────────
def test_system_context_child_becomes_system_context_block() -> None:
    messages = [
        _msg("u1", "user", content={"parts": ["first message"]}, order=0),
        _msg("ctx", "system", parent="u1", content={"text": "custom instructions here"},
             content_type="user_editable_context", order=1),
    ]
    out = ToolCoalescingTransformer().transform(messages)
    assert _ids(out) == ["u1"]
    ctx = out[0].content_blocks[1]
    assert ctx == {
        "type": "system_context",
        "text": "custom instructions here",
        "context_type": "user_editable_context",
        "seq": 1,
    }


def test_stub_child_is_absorbed_and_contributes_no_blocks() -> None:
    messages = [
        _msg("u1", "user", content={"parts": ["hi"]}, order=0),
        _msg("stub", None, parent="u1", order=1, stub=True),
    ]
    out = ToolCoalescingTransformer().transform(messages)
    assert _ids(out) == ["u1"]
    assert [b["type"] for b in out[0].content_blocks] == ["text"]


def test_empty_thinking_child_is_absorbed_without_a_block() -> None:
    """A thinking message with no extractable text folds away silently — removed
    from the output but contributing zero blocks."""
    messages = [
        _msg("u1", "user", content={"parts": ["hi"]}, order=0),
        _msg("think", "assistant", parent="u1", content={"parts": []},
             content_type="thoughts", order=1),
    ]
    out = ToolCoalescingTransformer().transform(messages)
    assert _ids(out) == ["u1"]
    assert [b["type"] for b in out[0].content_blocks] == ["text"]


def test_metadata_tool_call_shapes_openai_function_and_single_dict() -> None:
    """tool_calls metadata is accepted as a list of OpenAI-style function entries
    or a single bare dict; missing names fall back to 'unknown'."""
    messages = [
        _msg("u1", "user", content={"parts": ["go"]}, order=0),
        _msg("call1", "assistant", parent="u1", content={"parts": []}, content_type="code", order=1,
             metadata={"tool_calls": [
                 {"function": {"name": "search", "arguments": '{"q": "eds"}'}},
                 {"args": {"x": 1}},  # nameless → unknown
             ]}),
        _msg("call2", "assistant", parent="call1", content={"parts": []}, content_type="code", order=2,
             metadata={"tool_call": {"name": "browser", "arguments": {"url": "u"}}}),
    ]
    out = ToolCoalescingTransformer().transform(messages)
    assert _ids(out) == ["u1"]
    tool_uses = [b for b in out[0].content_blocks if b["type"] == "tool_use"]
    assert [(b["name"], b["input"]) for b in tool_uses] == [
        ("search", '{"q": "eds"}'),
        ("unknown", {"x": 1}),
        ("browser", {"url": "u"}),
    ]
    assert [b["seq"] for b in out[0].content_blocks] == [0, 1, 2, 3]


def test_multimodal_primary_extracts_text_then_images() -> None:
    """Mixed text+image parts on a primary produce a joined text block followed by
    one image block per image part, in part order."""
    content = {"parts": [
        "look at this",
        {"content_type": "image/png", "asset_pointer": "file-service://abc"},
        {"text": "and this caption"},
        {"content_type": "image/jpeg", "asset_pointer": "file-service://def"},
    ]}
    out = ToolCoalescingTransformer().transform([_msg("u1", "user", content=content, order=0)])
    blocks = out[0].content_blocks
    assert [b["type"] for b in blocks] == ["text", "image", "image"]
    assert blocks[0]["text"] == "look at this\n\nand this caption"  # str + text parts joined
    assert blocks[1]["asset_pointer"] == "file-service://abc"
    assert blocks[1]["mime_type"] == "image/png"
    assert blocks[2]["asset_pointer"] == "file-service://def"


def test_contentless_assistant_is_not_primary_but_survives_uncoalesced() -> None:
    """An assistant row with empty parts/text is not a primary (nothing displayable)
    — but if nothing folds it in, it is still kept, never dropped."""
    out = ToolCoalescingTransformer().transform(
        [_msg("a-empty", "assistant", content={"parts": [], "text": ""}, order=0)]
    )
    assert _ids(out) == ["a-empty"]
    assert out[0].content_blocks == []  # untouched: no block build for non-primaries
