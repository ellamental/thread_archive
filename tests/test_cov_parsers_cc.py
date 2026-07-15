"""Branch-coverage tests for the vendored claude-code parser.

Exercises ``ClaudeCodeParser`` (``claude_code.py``) and its stateless block
helpers (``claude_code_blocks.py``) directly — feeding record fixtures through
each entry point and each block/edge branch, asserting the produced
normalized-message / content-block shapes are correct.

These are unit tests over the parser layer: they call ``parse_export`` and the
(test-called) private methods / block helpers directly rather than driving the
full importer, so a single malformed-record branch can be pinned in isolation.
"""

from __future__ import annotations

from thread_archive._thread_import.parsers import claude_code_blocks as blocks
from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser


def _parser() -> ClaudeCodeParser:
    return ClaudeCodeParser()


def _by_type(msg: dict, block_type: str) -> list:
    return [b for b in msg.get("content_blocks", []) if b.get("type") == block_type]


# ── parse_export: input-shape dispatch ───────────────────────────────────────


def test_parse_export_raw_jsonl_string_with_parse_error() -> None:
    """A raw JSONL *string* routes through ``_parse_jsonl``: valid lines parse,
    and an unparseable line is preserved as a parse_error message (not dropped),
    carrying the raw text, error string and 1-based line number."""
    jsonl = "\n".join([
        '{"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",'
        ' "sessionId": "s1", "message": {"role": "user", "content": "hello"}}',
        "",  # blank line — skipped, must not shift the error's line number
        "{this is not valid json",
    ])
    msgs = _parser().parse_export(jsonl)

    users = [m for m in msgs if m["role"] == "user"]
    assert len(users) == 1 and users[0]["content_text"] == "hello"

    errs = [m for m in msgs if _by_type(m, "parse_error")]
    assert len(errs) == 1
    block = errs[0]["content_blocks"][0]
    assert block["raw_text"] == "{this is not valid json"
    assert block["line_number"] == 3  # blank line counted, error is the 3rd line
    assert errs[0]["provider_message_id"].startswith("parse-error-")
    assert errs[0]["provider_data"]["parse_error"] is True
    assert "[Parse error at line 3]" in errs[0]["content_text"]


def test_parse_export_dict_lines_form() -> None:
    """A dict with a ``lines`` key parses that single session, threading the
    provided ``session_id`` onto messages that carry none of their own."""
    data = {
        "lines": [
            {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
             "message": {"role": "user", "content": "hi"}},
        ],
        "session_id": "explicit-sess",
    }
    msgs = _parser().parse_export(data)
    assert msgs[0]["provider_conversation_id"] == "explicit-sess"


def test_parse_export_dict_provider_form_routes_to_bundle() -> None:
    """A dict tagged ``provider: claude-code`` (no ``sessions``/``lines`` keys
    at top level, but a ``sessions`` list) is treated as a bundle."""
    data = {
        "provider": "claude-code",
        "sessions": [
            {"session_id": "b1", "lines": [
                {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
                 "message": {"role": "user", "content": "bundled"}},
            ]},
        ],
    }
    msgs = _parser().parse_export(data)
    assert len(msgs) == 1
    assert msgs[0]["provider_conversation_id"] == "b1"
    assert msgs[0]["content_text"] == "bundled"


def test_parse_export_bundle_uses_id_fallback_for_session() -> None:
    """In a bundle, a session lacking ``session_id`` falls back to ``id``."""
    data = {
        "sessions": [
            {"id": "via-id", "lines": [
                {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
                 "message": {"role": "user", "content": "x"}},
            ]},
        ],
    }
    msgs = _parser().parse_export(data)
    assert msgs[0]["provider_conversation_id"] == "via-id"


def test_parse_export_provider_tagged_without_sessions_is_empty_bundle() -> None:
    """A ``provider: claude-code`` envelope with no ``sessions``/``lines`` keys is
    recognized as a (degenerate, empty) bundle rather than a single line."""
    assert _parser().parse_export({"provider": "claude-code"}) == []


def test_parse_export_bare_dict_single_line() -> None:
    """A dict that is neither a bundle nor a ``lines`` wrapper is treated as one
    line and parsed on its own."""
    data = {"type": "user", "uuid": "solo", "timestamp": "2026-01-01T10:00:00Z",
            "message": {"role": "user", "content": "solo turn"}}
    msgs = _parser().parse_export(data)
    assert len(msgs) == 1 and msgs[0]["content_text"] == "solo turn"


def test_parse_export_list_of_lines() -> None:
    """A bare list of line dicts parses as one session."""
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
         "sessionId": "s1", "message": {"role": "user", "content": "listy"}},
    ]
    msgs = _parser().parse_export(lines)
    assert msgs[0]["content_text"] == "listy"
    assert msgs[0]["provider_conversation_id"] == "s1"


def test_parse_export_unsupported_type_returns_empty() -> None:
    """An input that is neither str/dict/list yields no messages."""
    assert _parser().parse_export(42) == []
    assert _parser().parse_export(None) == []


# ── session-metadata inference across lines ──────────────────────────────────


def test_session_metadata_inferred_from_first_line_only() -> None:
    """sessionId / cwd are inferred from the first line that carries each and
    threaded onto later turns; a line that yields no message (empty user turn) is
    skipped without aborting. git_branch, by contrast, is read from each own line
    (the inferred value is not threaded down)."""
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
         "sessionId": "sess-A", "cwd": "/first", "gitBranch": "main",
         "message": {"role": "user", "content": "first"}},
        {"type": "user"},  # no message → parser returns None, loop continues
        {"type": "user", "uuid": "u2", "timestamp": "2026-01-01T10:00:02Z",
         "sessionId": "sess-B", "cwd": "/second", "gitBranch": "dev",
         "message": {"role": "user", "content": "second"}},
    ]
    msgs = _parser().parse_export(lines)
    # Only the two non-empty turns survive.
    assert [m["content_text"] for m in msgs] == ["first", "second"]
    # Both carry the *first* line's inferred session id + cwd.
    for m in msgs:
        assert m["provider_conversation_id"] == "sess-A"
        assert m["conversation_metadata"]["project_path"] == "/first"
    # git_branch is per-line, not inferred/threaded.
    assert msgs[0]["conversation_metadata"]["git_branch"] == "main"
    assert msgs[1]["conversation_metadata"]["git_branch"] == "dev"


# ── user messages: list content, tool_result, image, unknown, bare string ────


def test_user_list_content_tool_result_image_and_unknown() -> None:
    """A user turn whose content is a list of blocks: tool_result, image, an
    unmodeled block kind, and a bare string are each preserved as their own
    content block (Archivist, not Filter)."""
    line = {
        "type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": [{"type": "text", "text": "result body"}], "is_error": True},
            {"type": "image", "source": {"type": "base64", "data": "aGk="}},
            {"type": "mcp_tool_use", "name": "weird", "payload": {"k": 1}},
            "a bare string block",
        ]},
    }
    (msg,) = _parser().parse_export([line])

    tr = _by_type(msg, "tool_result")[0]
    assert tr["tool_use_id"] == "tu1"
    assert tr["text"] == "result body"
    assert tr["is_error"] is True

    img = _by_type(msg, "image")[0]
    assert img["source"]["data"] == "aGk="

    unknown = _by_type(msg, "mcp_tool_use")[0]
    assert unknown["raw"]["payload"] == {"k": 1}

    assert "a bare string block" in msg["content_text"]


def test_user_string_content_with_model_change_marker() -> None:
    """A ``/model`` stdout confirmation on a user string turn yields a
    model_change block (the switch target) even though its command tags strip to
    nothing else."""
    line = {
        "type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1",
        "message": {"role": "user",
                    "content": "<local-command-stdout>Set model to claude-fable-5"
                               "</local-command-stdout>"},
    }
    (msg,) = _parser().parse_export([line])
    mc = _by_type(msg, "model_change")[0]
    assert mc["to_model"] == "claude-fable-5"
    assert mc["trigger"] == "user"


def test_user_message_missing_message_returns_none() -> None:
    """A user line with no ``message`` produces no normalized message."""
    assert _parser().parse_export([{"type": "user", "uuid": "u1"}]) == []


def test_user_content_neither_str_nor_list_yields_empty_text() -> None:
    """A user message whose content is an unexpected shape (a dict, not str or
    list) doesn't crash: it yields a message with empty text and no blocks."""
    line = {
        "type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1", "message": {"role": "user", "content": {"weird": 1}},
    }
    (msg,) = _parser().parse_export([line])
    assert msg["content_text"] == ""
    assert msg["content_blocks"] == []


def test_user_ide_context_blocks_appended_and_renumbered() -> None:
    """IDE selection tags are extracted to ide_context blocks appended after the
    main text block, with seq renumbered to follow it."""
    line = {
        "type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1",
        "message": {"role": "user",
                    "content": "fix this\n<ide_selection>def f(): pass</ide_selection>"},
    }
    (msg,) = _parser().parse_export([line])
    assert msg["content_text"] == "fix this"
    ide = _by_type(msg, "ide_context")[0]
    assert ide["context_type"] == "selection"
    assert ide["seq"] == 1  # renumbered to come after the text block at seq 0


# ── attachment: queued_command steering messages ─────────────────────────────


def test_attachment_queued_command_string_prompt() -> None:
    """A ``queued_command`` attachment (a mid-turn steering message) is rebuilt
    as a user message, stamped with the attachment's own queue timestamp."""
    line = {
        "type": "attachment", "uuid": "at1", "timestamp": "2026-01-01T10:00:05Z",
        "sessionId": "s1",
        "attachment": {"type": "queued_command", "prompt": "stop and reconsider",
                       "timestamp": "2026-01-01T10:00:01Z", "commandMode": "prompt"},
    }
    (msg,) = _parser().parse_export([line])
    assert msg["role"] == "user"
    assert msg["content_text"] == "stop and reconsider"
    assert msg["provider_data"]["queued_command"] is True
    assert msg["provider_data"]["command_mode"] == "prompt"


def test_attachment_queued_command_list_prompt() -> None:
    """A queued_command whose prompt is a list of text blocks concatenates the
    text; non-text / empty blocks are ignored."""
    line = {
        "type": "attachment", "uuid": "at2", "timestamp": "2026-01-01T10:00:05Z",
        "sessionId": "s1",
        "attachment": {"type": "queued_command", "prompt": [
            {"type": "text", "text": "part one"},
            {"type": "text", "text": ""},          # empty → skipped
            {"type": "image", "source": {}},        # non-text → skipped
            {"type": "text", "text": "part two"},
        ]},
    }
    (msg,) = _parser().parse_export([line])
    assert [b["text"] for b in _by_type(msg, "text")] == ["part one", "part two"]


def test_attachment_queued_command_empty_prompt_dropped() -> None:
    """A queued_command with no usable prompt text yields nothing (there is no
    steering content to reconstruct)."""
    line = {
        "type": "attachment", "uuid": "at3", "timestamp": "2026-01-01T10:00:05Z",
        "sessionId": "s1",
        "attachment": {"type": "queued_command", "prompt": ""},
    }
    assert _parser().parse_export([line]) == []


def test_attachment_queued_command_nonstring_prompt_dropped() -> None:
    """A queued_command whose prompt is neither a string nor a list (missing /
    malformed) reconstructs no text and is dropped."""
    line = {
        "type": "attachment", "uuid": "at6", "timestamp": "2026-01-01T10:00:05Z",
        "sessionId": "s1",
        "attachment": {"type": "queued_command", "prompt": {"unexpected": 1}},
    }
    assert _parser().parse_export([line]) == []


def test_attachment_non_queued_preserved_hidden() -> None:
    """A non-queued attachment is preserved as a hidden system record carrying
    the raw attachment, not dropped."""
    line = {
        "type": "attachment", "uuid": "at4", "timestamp": "2026-01-01T10:00:05Z",
        "sessionId": "s1",
        "attachment": {"type": "todo_reminder", "content": "remember X"},
    }
    (msg,) = _parser().parse_export([line])
    assert msg["role"] == "system"
    assert msg["is_visually_hidden"] is True
    att = _by_type(msg, "attachment")[0]
    assert att["attachment_type"] == "todo_reminder"
    assert att["raw"]["content"] == "remember X"


def test_attachment_missing_type_preserved_as_attachment() -> None:
    """An attachment record with no ``type`` still round-trips (labelled the
    generic ``attachment``)."""
    line = {
        "type": "attachment", "uuid": "at5", "timestamp": "2026-01-01T10:00:05Z",
        "sessionId": "s1", "attachment": {"content": "no type here"},
    }
    (msg,) = _parser().parse_export([line])
    assert msg["content_text"] == "[attachment: attachment]"


# ── assistant messages ───────────────────────────────────────────────────────


def test_assistant_missing_message_returns_none() -> None:
    """An assistant line with no ``message`` produces nothing."""
    assert _parser().parse_export([{"type": "assistant", "uuid": "a1"}]) == []


def test_assistant_blocks_thinking_text_tool_use_and_unknown() -> None:
    """An assistant turn with thinking / text / tool_use / unknown blocks maps
    each to the normalized shape, and content_text is the joined text parts."""
    line = {
        "type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1",
        "message": {"role": "assistant", "model": "claude-opus-4", "content": [
            {"type": "thinking", "thinking": "reasoning", "signature": "sig"},
            {"type": "text", "text": "first"},
            {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}},
            {"type": "server_tool_use", "id": "srv", "name": "web", "input": {}},
            {"type": "text", "text": "second"},
        ]},
    }
    (msg,) = _parser().parse_export([line])
    think = _by_type(msg, "thinking")[0]
    assert think["text"] == "reasoning" and think["signature"] == "sig"
    tu = _by_type(msg, "tool_use")[0]
    assert tu["tool_call_id"] == "tu1" and tu["name"] == "Bash"
    assert _by_type(msg, "server_tool_use")[0]["raw"]["name"] == "web"
    assert msg["content_text"] == "first\nsecond"
    assert msg["conversation_metadata"]["model"] == "claude-opus-4"


def test_assistant_string_content_plain_and_xml() -> None:
    """A string-valued assistant content: plain text becomes one text block;
    inline ``<function_calls>`` XML is split into interleaved text/tool_use."""
    plain = {
        "type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1",
        "message": {"role": "assistant", "model": "m", "content": "just text"},
    }
    (m1,) = _parser().parse_export([plain])
    assert m1["content_text"] == "just text"

    xml = {
        "type": "assistant", "uuid": "a2", "timestamp": "2026-01-01T10:00:01Z",
        "sessionId": "s1",
        "message": {"role": "assistant", "model": "m", "content":
                    'before<function_calls><invoke name="Bash">'
                    '<parameter name="command">ls -la</parameter>'
                    '</invoke></function_calls>after'},
    }
    (m2,) = _parser().parse_export([xml])
    tu = _by_type(m2, "tool_use")[0]
    assert tu["name"] == "Bash" and tu["input"] == {"command": "ls -la"}
    texts = [b["text"] for b in _by_type(m2, "text")]
    assert texts == ["before", "after"]


# ── system messages ──────────────────────────────────────────────────────────


def test_system_message_with_content() -> None:
    """A system line with a content string keeps it as a system_context block."""
    line = {
        "type": "system", "uuid": "sy1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1", "content": "compaction happened", "subtype": "info",
        "level": "warn",
    }
    (msg,) = _parser().parse_export([line])
    assert msg["is_visually_hidden"] is True
    ctx = _by_type(msg, "system_context")[0]
    assert ctx["context_type"] == "info" and ctx["content"] == "compaction happened"
    assert msg["provider_data"]["level"] == "warn"


def test_system_message_empty_with_metadata_synthesizes_summary() -> None:
    """An empty-content system line that still carries metadata (compact_boundary)
    is preserved with a synthesized ``[system: <subtype>]`` summary."""
    line = {
        "type": "system", "uuid": "sy2", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1", "content": "", "subtype": "compact_boundary",
        "compactMetadata": {"preTokens": 999},
    }
    (msg,) = _parser().parse_export([line])
    assert msg["content_text"] == "[system: compact_boundary]"


def test_system_message_empty_metadata_no_subtype_generic_summary() -> None:
    """An empty system line carrying metadata but no subtype gets the generic
    ``[system event]`` summary."""
    line = {
        "type": "system", "uuid": "sy3", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1", "content": "", "level": "info",
    }
    (msg,) = _parser().parse_export([line])
    assert msg["content_text"] == "[system event]"


def test_system_message_wholly_empty_dropped() -> None:
    """A system line with neither content nor metadata is dropped."""
    line = {"type": "system", "uuid": "sy4", "timestamp": "2026-01-01T10:00:00Z",
            "sessionId": "s1", "content": ""}
    assert _parser().parse_export([line]) == []


# ── summary / file-snapshot / queue-operation / progress lines ───────────────


def test_summary_message_from_summary_field() -> None:
    """A ``summary`` line preserves its text as a context_summary block and lists
    which messages it summarizes."""
    line = {
        "type": "summary", "uuid": "sum1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1", "summary": "we discussed the plan",
        "summarizes": ["u1", "a1"],
    }
    (msg,) = _parser().parse_export([line])
    cs = _by_type(msg, "context_summary")[0]
    assert cs["text"] == "we discussed the plan"
    assert cs["summarizes"] == ["u1", "a1"]
    assert msg["content_text"] == "[Context Summary]: we discussed the plan"


def test_summary_message_long_text_truncated_and_message_fallback() -> None:
    """Summary text pulled from ``message.content`` and summarized ids from
    ``summarizedMessages``; a >200-char summary truncates in the display text."""
    long_text = "x" * 250
    line = {
        "type": "summary", "uuid": "", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1", "summarizedMessages": ["a", "b"],
        "message": {"content": long_text},
    }
    (msg,) = _parser().parse_export([line])
    cs = _by_type(msg, "context_summary")[0]
    assert cs["summarizes"] == ["a", "b"]
    assert msg["content_text"].endswith("...")
    assert msg["content_text"].startswith("[Context Summary]: " + "x" * 200)
    # No uuid → synthesized summary-<order> id.
    assert msg["provider_message_id"].startswith("summary-")


def test_file_snapshot_from_files_field() -> None:
    """A file-history-snapshot line keeps its file list and a ``N file(s)``
    summary."""
    line = {
        "type": "file-history-snapshot", "uuid": "fs1",
        "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
        "files": [{"path": "/a"}, {"path": "/b"}],
    }
    (msg,) = _parser().parse_export([line])
    fs = _by_type(msg, "file_snapshot")[0]
    assert len(fs["files"]) == 2
    assert msg["content_text"] == "[File Snapshot]: 2 file(s)"
    assert msg["is_visually_hidden"] is True


def test_file_snapshot_from_content_list_and_snapshot_dict() -> None:
    """The snapshot file list is recovered from ``content`` (list) or
    ``snapshot.files`` when ``files`` is absent."""
    from_content = {
        "type": "file-history-snapshot", "uuid": "fs2",
        "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
        "content": [{"path": "/c"}],
    }
    (m1,) = _parser().parse_export([from_content])
    assert _by_type(m1, "file_snapshot")[0]["files"] == [{"path": "/c"}]

    from_snapshot = {
        "type": "file-history-snapshot", "uuid": "fs3",
        "timestamp": "2026-01-01T10:00:01Z", "sessionId": "s1",
        "snapshot": {"files": [{"path": "/d"}, {"path": "/e"}]},
    }
    (m2,) = _parser().parse_export([from_snapshot])
    assert m2["content_text"] == "[File Snapshot]: 2 file(s)"


def test_file_snapshot_no_uuid_synthesizes_id() -> None:
    """A snapshot with no uuid gets a synthesized ``file-snapshot-<order>`` id."""
    line = {"type": "file-history-snapshot", "uuid": "",
            "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1", "files": []}
    (msg,) = _parser().parse_export([line])
    assert msg["provider_message_id"].startswith("file-snapshot-")


def test_queue_operation_line() -> None:
    """A queue-operation line becomes a hidden system record labelled with the
    operation."""
    line = {
        "type": "queue-operation", "operation": "enqueue",
        "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
    }
    (msg,) = _parser().parse_export([line])
    assert msg["content_text"] == "[Queue enqueue]"
    qo = _by_type(msg, "queue_operation")[0]
    assert qo["data"]["operation"] == "enqueue"
    assert msg["provider_message_id"].startswith("queue-enqueue-")


def test_progress_line() -> None:
    """A progress line preserves its data payload and tool-use id."""
    line = {
        "type": "progress", "uuid": "p1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1", "toolUseID": "tu9", "data": {"type": "mcp_progress"},
    }
    (msg,) = _parser().parse_export([line])
    assert msg["content_text"] == "[Progress: mcp_progress]"
    pr = _by_type(msg, "progress")[0]
    assert pr["tool_use_id"] == "tu9"
    assert pr["data"]["type"] == "mcp_progress"


def test_unknown_line_type_preserved() -> None:
    """A line type the parser doesn't model is preserved verbatim as an
    unknown_line system record."""
    line = {
        "type": "telemetry_v9", "uuid": "t1", "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1", "payload": {"cpu": 0.9},
    }
    (msg,) = _parser().parse_export([line])
    ul = _by_type(msg, "unknown_line")[0]
    assert ul["line_type"] == "telemetry_v9"
    assert ul["raw"]["payload"] == {"cpu": 0.9}
    assert msg["content_text"] == "[unrecognized line: telemetry_v9]"


def test_unknown_line_no_type_labelled_unknown() -> None:
    """A line with an empty type falls through to the unknown-line preserver and
    is labelled 'unknown'."""
    line = {"type": "", "uuid": "t2", "timestamp": "2026-01-01T10:00:00Z",
            "sessionId": "s1"}
    (msg,) = _parser().parse_export([line])
    assert msg["content_text"] == "[unrecognized line: unknown]"


# ── delegating private methods (thin wrappers over claude_code_blocks) ───────


def test_delegating_methods_round_trip() -> None:
    """The instance delegators forward to the block helpers unchanged."""
    p = _parser()

    # _extract_tool_result_text
    assert p._extract_tool_result_text("plain") == "plain"

    # _parse_xml_function_calls
    cbs, texts, seq = p._parse_xml_function_calls(
        'x<function_calls><invoke name="Read">'
        '<parameter name="path">/f</parameter></invoke></function_calls>', 0
    )
    assert any(b["type"] == "tool_use" and b["name"] == "Read" for b in cbs)
    assert texts == ["x"]
    assert seq == 2

    # _append_assistant_block
    blocks_out: list = []
    parts: list = []
    nxt = p._append_assistant_block(
        {"type": "text", "text": "hey"}, blocks_out, parts, 0
    )
    assert nxt == 1 and parts == ["hey"]

    # _assistant_content
    cb, tp = p._assistant_content("hello")
    assert tp == ["hello"] and cb[0]["type"] == "text"


def test_chain_helper_delegators() -> None:
    """The static chain delegators (build_chain_indices / find_chain_roots /
    merge_chain) forward to the block helpers."""
    p = _parser()
    msgs = [
        {"provider_message_id": "u1", "role": "user"},
        {"provider_message_id": "a1", "provider_parent_id": "u1",
         "role": "assistant", "content_blocks": [{"type": "text", "text": "x"}]},
        {"provider_message_id": "a2", "provider_parent_id": "a1",
         "role": "assistant", "content_blocks": [{"type": "text", "text": "y"}]},
    ]
    by_id, children_of = p._build_chain_indices(msgs)
    assert set(by_id) == {"u1", "a1", "a2"}
    roots = p._find_chain_roots(msgs, by_id)
    assert [r["provider_message_id"] for r in roots] == ["a1"]
    to_remove: set = set()
    p._merge_chain(roots[0], children_of, to_remove)
    assert to_remove == {"a2"}
    assert len(roots[0]["content_blocks"]) == 2


# ── claude_code_blocks: direct unit coverage of the pure helpers ─────────────


def test_extract_tool_result_text_shapes() -> None:
    """extract_tool_result_text: str passthrough, list of mixed str/dict, and a
    non-str/list falls back to ''."""
    assert blocks.extract_tool_result_text("hi") == "hi"
    mixed = ["a", {"text": "b"}, {"no_text": 1}, 42]
    assert blocks.extract_tool_result_text(mixed) == "a\nb"
    assert blocks.extract_tool_result_text(None) == ""
    assert blocks.extract_tool_result_text(99) == ""


def test_user_content_blocks_from_list_all_shapes() -> None:
    """user_content_blocks_from_list covers image/text-with-ide/unknown/bare-str,
    returning renumbered ide blocks separately."""
    content = [
        {"type": "image", "source": {"data": "img"}},
        {"type": "text", "text": "keep me\n<ide_selection>sel</ide_selection>"},
        {"type": "text", "text": ""},          # empty text → no block
        {"type": "text", "text": "<ide_selection>only</ide_selection>"},  # all-IDE → text empties out
        {"type": "future_kind", "k": 1},        # unknown → preserved
        "bare",
        "",                                       # empty bare string → skipped
        {"not_a": "type"},                        # no type key → skipped
        None,                                     # neither dict nor str → skipped
        42,                                       # neither dict nor str → skipped
    ]
    cbs, text, ide = blocks.user_content_blocks_from_list(content)
    types = [b["type"] for b in cbs]
    assert types == ["image", "text", "future_kind", "text"]
    assert text == "keep me\nbare"
    # Two selections: one from the mixed text block, one from the all-IDE block.
    assert [b["context_type"] for b in ide] == ["selection", "selection"]


def test_parse_xml_function_calls_multiple_invokes_and_text() -> None:
    """parse_xml_function_calls splits interleaved text and multiple invokes,
    extracting parameters and assigning deterministic ids."""
    raw = ('lead<function_calls>'
           '<invoke name="Read"><parameter name="path">/a</parameter></invoke>'
           '<invoke name="Bash"><parameter name="command">ls</parameter></invoke>'
           '</function_calls>tail')
    cbs, texts, seq = blocks.parse_xml_function_calls(raw, 0)
    kinds = [(b["type"], b.get("name")) for b in cbs]
    assert kinds == [
        ("text", None), ("tool_use", "Read"), ("tool_use", "Bash"), ("text", None),
    ]
    assert texts == ["lead", "tail"]
    read = next(b for b in cbs if b.get("name") == "Read")
    assert read["input"] == {"path": "/a"}
    assert read["tool_call_id"]  # deterministic uuid5 assigned
    assert seq == 4


def test_append_assistant_block_empty_and_unknown() -> None:
    """append_assistant_block: empty thinking/text add nothing (seq unchanged);
    a typeless block returns seq unchanged; an unknown type is preserved."""
    cbs: list = []
    parts: list = []
    # Empty thinking → no append, seq unchanged.
    assert blocks.append_assistant_block({"type": "thinking", "thinking": ""}, cbs, parts, 3) == 3
    # Empty text → no append, seq unchanged.
    assert blocks.append_assistant_block({"type": "text", "text": ""}, cbs, parts, 3) == 3
    # Typeless block → returns seq unchanged (final fallthrough).
    assert blocks.append_assistant_block({}, cbs, parts, 3) == 3
    assert cbs == []
    # Unknown type → preserved, seq advances.
    assert blocks.append_assistant_block({"type": "redacted", "d": 1}, cbs, parts, 3) == 4
    assert cbs[0]["type"] == "redacted" and cbs[0]["raw"]["d"] == 1


def test_assistant_content_skips_non_dict_blocks() -> None:
    """assistant_content ignores non-dict entries in a list content."""
    cbs, parts = blocks.assistant_content([
        "not a dict",
        {"type": "text", "text": "kept"},
        None,
    ])
    assert parts == ["kept"]
    assert [b["type"] for b in cbs] == ["text"]


def test_assistant_content_non_str_non_list_yields_empty() -> None:
    """assistant_content with content that is neither a list nor a string (a
    malformed dict / None) yields no blocks and no text."""
    assert blocks.assistant_content({"weird": 1}) == ([], [])
    assert blocks.assistant_content(None) == ([], [])


def test_dedup_compaction_replays_drops_identical_replays() -> None:
    """A user/assistant message replayed with a new uuid but identical
    (role, text, created_at) is deduped to its first occurrence; system rows and
    distinct model_change markers are never collapsed."""
    msgs = [
        {"role": "user", "content_text": "hi", "created_at": "t1",
         "provider_message_id": "u1"},
        {"role": "user", "content_text": "hi", "created_at": "t1",
         "provider_message_id": "u1-replay"},  # dup → dropped
        {"role": "assistant", "content_text": "yo", "created_at": "t2",
         "provider_message_id": "a1"},
        {"role": "system", "content_text": "s", "created_at": "t3",
         "provider_message_id": "sy1"},
        {"role": "system", "content_text": "s", "created_at": "t3",
         "provider_message_id": "sy2"},  # system never deduped
    ]
    out = blocks.dedup_compaction_replays(msgs)
    ids = [m["provider_message_id"] for m in out]
    assert ids == ["u1", "a1", "sy1", "sy2"]


def test_dedup_keeps_distinct_model_change_at_same_timestamp() -> None:
    """Two same-timestamp empty-text user turns are NOT collapsed when one bears
    a model_change marker (the marker is part of the dedup key)."""
    msgs = [
        {"role": "user", "content_text": "", "created_at": "t1",
         "provider_message_id": "cmd", "content_blocks": []},
        {"role": "user", "content_text": "", "created_at": "t1",
         "provider_message_id": "stdout",
         "content_blocks": [{"type": "model_change", "to_model": "fable"}]},
    ]
    out = blocks.dedup_compaction_replays(msgs)
    assert [m["provider_message_id"] for m in out] == ["cmd", "stdout"]


def test_apply_derived_title_truncates_multiline_and_long() -> None:
    """apply_derived_title derives from the first user message: a single short
    line is used verbatim; a multi-line / >100-char first message is truncated
    with an ellipsis and stamped on every message."""
    short = [
        {"role": "user", "content_text": "hello there"},
        {"role": "assistant", "content_text": "hi"},
    ]
    blocks.apply_derived_title(short)
    assert all(m["conversation_title"] == "hello there" for m in short)

    multiline = [{"role": "user", "content_text": "first line\nsecond line"}]
    blocks.apply_derived_title(multiline)
    assert multiline[0]["conversation_title"] == "first line..."

    longmsg = [{"role": "user", "content_text": "w" * 150}]
    blocks.apply_derived_title(longmsg)
    assert longmsg[0]["conversation_title"] == "w" * 100 + "..."


def test_apply_derived_title_no_user_message_noop() -> None:
    """With no user message carrying text, no title is applied."""
    msgs = [{"role": "assistant", "content_text": "hi"}]
    blocks.apply_derived_title(msgs)
    assert "conversation_title" not in msgs[0]


def test_build_chain_indices_groups_siblings_and_skips_idless() -> None:
    """build_chain_indices groups multiple children under a shared parent and
    ignores messages without an id (in by_id) / parent (in children_of)."""
    msgs = [
        {"provider_message_id": "p", "role": "user"},
        {"provider_message_id": "c1", "provider_parent_id": "p", "role": "assistant"},
        {"provider_message_id": "c2", "provider_parent_id": "p", "role": "assistant"},
        {"role": "assistant"},  # no id, no parent → contributes to neither index
    ]
    by_id, children_of = blocks.build_chain_indices(msgs)
    assert set(by_id) == {"p", "c1", "c2"}
    assert [m["provider_message_id"] for m in children_of["p"]] == ["c1", "c2"]


def test_find_chain_roots_variants() -> None:
    """find_chain_roots: assistant with no parent, assistant whose parent isn't
    in the session, and assistant whose parent is a user are all roots; an
    assistant whose parent is an assistant is NOT a root."""
    msgs = [
        {"provider_message_id": "u1", "role": "user"},
        {"provider_message_id": "a_no_parent", "role": "assistant"},
        {"provider_message_id": "a_missing", "provider_parent_id": "ghost",
         "role": "assistant"},
        {"provider_message_id": "a_child_of_user", "provider_parent_id": "u1",
         "role": "assistant"},
        {"provider_message_id": "a_child_of_asst",
         "provider_parent_id": "a_child_of_user", "role": "assistant"},
    ]
    by_id, _ = blocks.build_chain_indices(msgs)
    roots = {m["provider_message_id"] for m in blocks.find_chain_roots(msgs, by_id)}
    assert roots == {"a_no_parent", "a_missing", "a_child_of_user"}


def test_merge_chain_early_return_for_missing_or_removed_root() -> None:
    """merge_chain no-ops for a root with no id, or one already marked removed."""
    # No id → early return, no crash.
    blocks.merge_chain({"role": "assistant"}, {}, set())
    # Already-removed root → early return, blocks untouched.
    root = {"provider_message_id": "r", "content_blocks": [{"type": "text"}]}
    blocks.merge_chain(root, {}, {"r"})
    assert root["content_blocks"] == [{"type": "text"}]


def test_merge_chain_bfs_skips_nonassistant_and_cycles() -> None:
    """merge_chain BFS collects descendant assistant blocks, skips a
    non-assistant child, and tolerates a cyclic parent reference without looping
    or double-merging."""
    root = {"provider_message_id": "r", "role": "assistant",
            "content_blocks": [{"type": "text", "text": "r"}]}
    a2 = {"provider_message_id": "a2", "role": "assistant",
          "content_blocks": [{"type": "text", "text": "a2"}]}
    sysrow = {"provider_message_id": "sy", "role": "system",
              "content_blocks": [{"type": "x"}]}
    a3 = {"provider_message_id": "a3", "role": "assistant",
          "content_blocks": [{"type": "text", "text": "a3"}]}
    children_of = {
        "r": [a2],
        "a2": [sysrow, a3],
        "a3": [a2],  # cycle back to a2 (already removed) → skipped
    }
    to_remove: set = set()
    blocks.merge_chain(root, children_of, to_remove)
    assert to_remove == {"a2", "a3"}  # system row not merged, cycle not re-added
    texts = [b.get("text") for b in root["content_blocks"] if b["type"] == "text"]
    assert texts == ["r", "a2", "a3"]


def test_coalesce_assistant_chains_merges_linked_turn() -> None:
    """coalesce_assistant_chains folds a parentUuid-linked assistant chain into a
    single message with combined blocks, dropping the merged children."""
    msgs = [
        {"provider_message_id": "u1", "role": "user", "content_blocks": []},
        {"provider_message_id": "a1", "provider_parent_id": "u1",
         "role": "assistant", "content_blocks": [{"type": "thinking", "text": "t"}]},
        {"provider_message_id": "a2", "provider_parent_id": "a1",
         "role": "assistant", "content_blocks": [{"type": "text", "text": "body"}]},
        {"provider_message_id": "a3", "provider_parent_id": "a2",
         "role": "assistant",
         "content_blocks": [{"type": "tool_use", "name": "Bash"}]},
    ]
    out = blocks.coalesce_assistant_chains(msgs)
    ids = [m["provider_message_id"] for m in out]
    assert ids == ["u1", "a1"]  # a2/a3 merged into a1
    merged = next(m for m in out if m["provider_message_id"] == "a1")
    assert [b["type"] for b in merged["content_blocks"]] == [
        "thinking", "text", "tool_use",
    ]
