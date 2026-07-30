"""
Claude Code CLI session parser.

Parses session history from Claude Code (Anthropic's CLI tool).

## Format

Storage location: ``~/.claude/projects/<project-path>/<session-id>.jsonl``
Where ``<project-path>`` is the working directory with ``/`` replaced by ``-``.
Example: ``/home/user/project`` → ``-home-user-project``

Sessions are JSONL (one JSON object per line) with message types:

- ``type: "user"`` - User messages with ``message.content``
- ``type: "assistant"`` - Assistant responses with content blocks
- ``type: "system"`` - System events (commands, file snapshots, compaction)

## Edge Cases

Context Compaction (``compact_boundary``)
    When conversation exceeds token limit, Claude Code compacts history.
    A ``system`` message with ``subtype: "compact_boundary"`` marks where
    compaction occurred. The next ``user`` message contains a text summary
    of prior conversation. **All original messages remain in the JSONL** -
    they're just disconnected from the active parent chain.

    Key fields on compact_boundary:
    - ``parentUuid: null`` - breaks the parent chain
    - ``logicalParentUuid`` - points to last message before compaction
    - ``compactMetadata.preTokens`` - token count before compaction

Orphaned Parent References
    After compaction, messages reference parents that exist but aren't in
    the "active" chain. This is expected behavior, not data corruption.
    Validation emits warnings, not errors.

Subagent Sessions (``agent-*``)
    Claude Code spawns subagents for complex tasks. These have session IDs
    starting with ``agent-`` (e.g., ``agent-a1b2c3d``). Subagent sessions:
    - Use Haiku model (no extended thinking)
    - Are linked to parent session via naming convention
    - Should NOT fail validation for missing thinking blocks

IDE Context Tags
    User messages may contain XML-like tags for IDE state:
    - ``<ide_opened_file>...</ide_opened_file>``
    - ``<ide_selection>...</ide_selection>``
    These are extracted as ``ide_context`` content blocks.

Tool Results in User Messages
    Tool execution results appear as ``user`` messages with content blocks
    of type ``tool_result``. These are NOT coalesced (unlike ChatGPT).

## Validation

- **Thinking blocks**: Model-specific. Required for Opus models, exempt for
  Sonnet/Haiku and all ``agent-*`` sessions. See ``ProviderConfig``.
- **Parent references**: May be orphaned after compaction. Warnings only.
- **Branching**: Tree via ``uuid``/``parentUuid``, but rarely used.

## Field Mapping

- ``uuid`` → ``provider_message_id``
- ``sessionId`` → ``provider_conversation_id``
- ``parentUuid`` → ``provider_parent_id``
- ``timestamp`` → ``created_at`` (ISO 8601)
- ``message.content`` → ``content_blocks``
"""

import json
from typing import Any, Dict, List, Optional, Tuple, TypedDict, cast

from . import claude_code_blocks as _blocks
from .base import (
    ContentBlock,
    NormalizedMessage,
    ProviderParser,
)
from .claude_code_ide import (
    _extract_ide_context,
    _extract_model_change,
    _parse_iso_timestamp,
    _timestamp_to_order,
)
from .config import CLAUDE_CODE_CONFIG, ProviderConfig

# Source-line extras carried into provider_data["annotations"] (the builder
# copies that dict onto the anchor event's payload — see event_builder's
# "Annotations" convention). (source key, annotation key); present-only, so a
# line without the field contributes nothing (no null-stuffing).
_ASSISTANT_LINE_ANNOTATIONS: Tuple[Tuple[str, str], ...] = (
    ("effort", "effort"),  # reasoning effort ("medium", "xhigh", ...)
    ("attributionMcpServer", "attribution_mcp_server"),
    ("attributionMcpTool", "attribution_mcp_tool"),
    ("attributionSkill", "attribution_skill"),
    ("requestId", "request_id"),
    ("gitBranch", "git_branch"),
    ("version", "version"),
)


class _ParseError(TypedDict):
    line_number: int
    raw_text: str
    error: str

_USER_LINE_ANNOTATIONS: Tuple[Tuple[str, str], ...] = (
    ("toolDenialKind", "tool_denial_kind"),
    ("mcpMeta", "mcp_meta"),
    ("permissionMode", "permission_mode"),
    ("origin", "origin"),
    ("promptSource", "prompt_source"),
    ("todos", "todos"),
    ("thinkingMetadata", "thinking_metadata"),
)


def _line_annotations(
    line: Dict[str, Any], mapping: Tuple[Tuple[str, str], ...]
) -> Dict[str, Any]:
    """Collect the line's annotation-bound extras. Present-only: absent/None
    source fields are omitted, never written as nulls."""
    return {
        dst: line[src]
        for src, dst in mapping
        if line.get(src) is not None
    }


class ClaudeCodeParser(ProviderParser):
    """Parser for Claude Code CLI session exports.

    Handles session JSONL files from `~/.claude/projects/<project>/`.

    ## Architecture

    Uses the pipeline architecture with:
    - PROVIDER_CONFIG: Claude Code-specific configuration (model_specific thinking)
    - Types: ClaudeCodeExport, ClaudeCodeSession for typed input

    ## How to Use

    1. Copy session files from `~/.claude/projects/<project-path>/*.jsonl`
    2. Create a ZIP archive or use directly
    3. Run: `chat-import --provider claude-code path/to/sessions/`

    ## Explicit Field Mappings

    Claude Code uses clear field names that mostly map directly:
    - `timestamp` -> `created_at` (ISO 8601 format)
    - `uuid` -> `provider_message_id`
    - `parentUuid` -> `provider_parent_id`
    - `sessionId` -> `provider_conversation_id`
    - `message.role` -> `role`
    - `message.content` -> structured content blocks
    """

    PROVIDER_NAME = "claude-code"

    # Provider-specific configuration (thinking/validation expectations)
    PROVIDER_CONFIG: ProviderConfig = CLAUDE_CODE_CONFIG

    def parse_export(self, data: Any) -> List[NormalizedMessage]:
        """
        Parse Claude Code session data into normalized messages.

        Args:
            data: Either:
                - A dict with 'sessions' key (bundle of multiple sessions)
                - A dict with 'lines' key (single session as parsed JSONL)
                - A list of session line dicts
                - Raw JSONL string

        Returns:
            List of NormalizedMessage dictionaries
        """
        # Handle different input formats
        if isinstance(data, str):
            # Raw JSONL string
            return self._parse_jsonl(data)

        if isinstance(data, dict):
            # Check for bundle format
            if "sessions" in data:
                return self._parse_session_bundle(data)
            # Check for single session with lines
            if "lines" in data:
                return self._parse_session_lines(data["lines"], data.get("session_id"))
            # Check for claude_code_sessions.json format
            if data.get("provider") == "claude-code":
                return self._parse_session_bundle(data)
            # Assume it's a single line/message
            return self._parse_session_lines([data], None)

        if isinstance(data, list):
            # List of session lines
            return self._parse_session_lines(data, None)

        return []

    def _parse_jsonl(
        self, jsonl: str, session_id: Optional[str] = None
    ) -> List[NormalizedMessage]:
        """Parse raw JSONL string into messages.

        JSON parse errors are stored as parse_error blocks rather than dropped.
        """
        lines = []
        parse_errors: List[_ParseError] = []

        for line_num, line in enumerate(jsonl.strip().split("\n"), start=1):
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError as e:
                    # Store parse error instead of dropping
                    parse_errors.append({
                        "line_number": line_num,
                        "raw_text": line[:1000],  # Truncate very long lines
                        "error": str(e),
                    })

        messages = self._parse_session_lines(lines, session_id)

        # Add parse errors as system messages with parse_error blocks
        for err in parse_errors:
            messages.append(self._create_parse_error_message(
                err["raw_text"],
                err["error"],
                err["line_number"],
                session_id,
            ))

        return messages

    def _create_parse_error_message(
        self,
        raw_text: str,
        error: str,
        line_number: Optional[int],
        session_id: Optional[str],
    ) -> NormalizedMessage:
        """Create a message containing a parse error block."""
        import hashlib

        # Generate a deterministic ID from the raw content
        content_hash = hashlib.sha256(raw_text.encode()).hexdigest()[:16]
        msg_id = f"parse-error-{content_hash}"

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": msg_id,
            "provider_conversation_id": session_id or "unknown",
            "role": "system",
            "content_text": f"[Parse error at line {line_number}]: {error}",
            "content_blocks": [{
                "type": "parse_error",
                "raw_text": raw_text,
                "error": error,
                "line_number": line_number,
                "seq": 0,
            }],
            "content_hash": self.hash_message(msg_id, "system", raw_text, None),
            "provider_data": {
                "parse_error": True,
                "line_number": line_number,
                "raw_text": raw_text,
                "error": error,
            },
        }

    def _parse_session_bundle(self, bundle: Dict[str, Any]) -> List[NormalizedMessage]:
        """Parse a bundle containing multiple sessions."""
        messages: List[NormalizedMessage] = []

        sessions = bundle.get("sessions", [])
        for session in sessions:
            session_id = session.get("session_id") or session.get("id")
            lines = session.get("lines", [])
            session_messages = self._parse_session_lines(lines, session_id)
            messages.extend(session_messages)

        return messages

    def _parse_session_lines(
        self, lines: List[Dict[str, Any]], session_id: Optional[str]
    ) -> List[NormalizedMessage]:
        """Parse a list of session line objects into messages."""
        messages: List[NormalizedMessage] = []

        # Track session info from first line
        inferred_session_id = session_id
        project_path = None
        git_branch = None

        for line in lines:
            line_type = line.get("type", "")

            # Extract session metadata from any line
            if not inferred_session_id:
                inferred_session_id = line.get("sessionId")
            if not project_path:
                project_path = line.get("cwd")
            if not git_branch:
                git_branch = line.get("gitBranch")

            msg = self._parse_line(line, line_type, inferred_session_id, project_path)
            if msg:
                messages.append(msg)

        messages = self._dedup_compaction_replays(messages)

        # Coalesce linked assistant messages into single messages with combined blocks
        # In Claude Code JSONL, one assistant "turn" spans multiple lines linked by parentUuid
        messages = self._coalesce_assistant_chains(messages)

        self._apply_derived_title(messages)

        return messages

    def _parse_line(
        self,
        line: Dict[str, Any],
        line_type: str,
        session_id: Optional[str],
        project_path: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Dispatch a single session line to its type-specific parser.

        ``summary``/``file-history-snapshot`` are stored (not dropped) as their
        own message kinds; ``attachment`` lines carry a ``queued_command``
        sub-kind (a steering message the user typed mid-turn) that must survive as a
        user message; unrecognized line types yield ``None``.
        """
        # Parsers taking (line, session_id, project_path).
        full_parsers = {
            "summary": self._parse_summary_message,
            "file-history-snapshot": self._parse_file_snapshot_message,
            "user": self._parse_user_message,
            "assistant": self._parse_assistant_message,
            "system": self._parse_system_message,
            "attachment": self._parse_attachment_message,
        }
        parser = full_parsers.get(line_type)
        if parser is not None:
            return parser(line, session_id, project_path)

        # Parsers taking (line, session_id) only.
        if line_type == "queue-operation":
            return self._parse_queue_operation(line, session_id)
        if line_type == "progress":
            return self._parse_progress_message(line, session_id)
        if line_type == "pr-link":
            # A pr-link naming no PR is not a PR association — fall through to
            # verbatim preservation rather than dropping the line on the floor.
            pr = self._parse_pr_link(line, session_id)
            if pr is not None:
                return pr

        # Any other (unrecognized or future) line kind is preserved verbatim
        # rather than dropped — nothing vanishes without a trace, not even a line
        # type Claude Code adds later.
        return self._parse_unknown_line(line, line_type, session_id, project_path)

    @staticmethod
    def _dedup_compaction_replays(
        messages: List[NormalizedMessage],
    ) -> List[NormalizedMessage]:
        """Drop compaction-replay duplicates (delegates to claude_code_blocks)."""
        return _blocks.dedup_compaction_replays(messages)

    @staticmethod
    def _apply_derived_title(messages: List[NormalizedMessage]) -> None:
        """Derive + apply the conversation title (delegates to claude_code_blocks)."""
        _blocks.apply_derived_title(messages)

    def _parse_user_message(
        self,
        line: Dict[str, Any],
        session_id: Optional[str],
        project_path: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Parse a user message line."""
        msg_data = line.get("message", {})
        if not msg_data:
            return None

        uuid = line.get("uuid", "")
        parent_uuid = line.get("parentUuid")
        timestamp = line.get("timestamp")
        content = msg_data.get("content", "")

        # Handle tool result content
        content_blocks: List[ContentBlock] = []
        content_text = ""
        ide_context_blocks: List[Dict[str, Any]] = []

        if isinstance(content, str):
            # Extract IDE context (opened files, selections) from the content
            cleaned_content, ide_context_blocks = _extract_ide_context(content)
            content_text = cleaned_content
            if cleaned_content:
                content_blocks.append(self.create_text_block(cleaned_content, 0))
            # A manual `/model` switch is otherwise stripped to nothing above (and the
            # turn dropped). Preserve it as a model_change block so the archive can show
            # the switch — the same marker Claude Code renders in its own transcript.
            switched_to = _extract_model_change(content)
            if switched_to:
                content_blocks.append(cast(ContentBlock, {
                    "type": "model_change",
                    "to_model": switched_to,
                    "trigger": "user",
                    "seq": len(content_blocks),
                }))
        elif isinstance(content, list):
            content_blocks, content_text, ide_context_blocks = (
                self._user_content_blocks_from_list(content)
            )

        if not content_text:
            content_text = self.extract_text_from_blocks(content_blocks)

        # Add IDE context blocks (opened files, selections) after other content
        if ide_context_blocks:
            # Renumber seq values to come after existing blocks
            base_seq = len(content_blocks)
            for i, block in enumerate(ide_context_blocks):
                block["seq"] = base_seq + i
            content_blocks.extend(cast(List[ContentBlock], ide_context_blocks))

        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0

        # The line-level toolUseResult is the STRUCTURED tool result (richer
        # than the tool_result block's text rendering). Attach it as a
        # block-level annotation on the line's tool_result block — sibling to
        # the content fields, so dedup identity is untouched. A line carries at
        # most one toolUseResult; on the rare multi-tool_result line it goes on
        # the first block.
        structured_result = line.get("toolUseResult")
        if structured_result is not None:
            for content_block in content_blocks:
                if isinstance(content_block, dict) and content_block.get("type") == "tool_result":
                    cast(Dict[str, Any], content_block)["annotations"] = {
                        "structured_result": structured_result
                    }
                    break

        annotations = _line_annotations(line, _USER_LINE_ANNOTATIONS)

        provider_data: Dict[str, Any] = {
            "line": line,
            "cwd": project_path or line.get("cwd"),
            "git_branch": line.get("gitBranch"),
            "version": line.get("version"),
            "thinking_metadata": line.get("thinkingMetadata"),
            "todos": line.get("todos"),
        }
        if annotations:
            provider_data["annotations"] = annotations

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": uuid,
            "provider_message_id_lower": uuid.lower() if uuid else None,
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "provider_parent_id": parent_uuid,
            "provider_parent_id_lower": parent_uuid.lower() if parent_uuid else None,
            "content_hash": self.hash_message(uuid, "user", content, timestamp),
            "role": "user",
            "content_text": content_text,
            "content_blocks": content_blocks,
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": not line.get("isSidechain", False),
            "provider_data": provider_data,
            "conversation_title": None,
            "conversation_metadata": {
                "project_path": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
            },
        }

    def _parse_attachment_message(
        self,
        line: Dict[str, Any],
        session_id: Optional[str],
        project_path: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Parse an ``attachment`` line.

        Claude Code writes several attachment sub-kinds. Most are context
        injections (todo reminders, tool/agent/skill listing deltas) with no
        user signal, and are dropped. The exception is ``queued_command`` — a
        message the user typed *while the agent was mid-turn* (a "steering"
        message). Its text lives only here: it is delivered to the model but
        never re-emitted as a normal ``user`` line, and the ``queue-operation``
        bookkeeping records carry no content. So if we don't reconstruct it, the
        steering turn is lost from the archive (and the viewer). Rebuild it as a
        user message, stamped with the time it was queued.
        """
        attachment = line.get("attachment") or {}
        if attachment.get("type") != "queued_command":
            # Every other attachment sub-kind (todo reminders, tool/agent/skill
            # listing deltas, context injections) is still real content Claude
            # Code fed the model — preserve it as a hidden system record rather
            # than dropping it. "Archivist, Not Filter."
            return self._preserve_attachment(line, attachment, session_id, project_path)

        prompt = attachment.get("prompt")
        content_blocks: List[ContentBlock] = []
        if isinstance(prompt, str):
            if prompt:
                content_blocks.append(self.create_text_block(prompt, 0))
        elif isinstance(prompt, list):
            for block in prompt:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        content_blocks.append(
                            self.create_text_block(text, len(content_blocks))
                        )

        content_text = self.extract_text_from_blocks(content_blocks)
        if not content_text:
            return None

        uuid = line.get("uuid", "")
        parent_uuid = line.get("parentUuid")
        # The steering message's own time lives on the attachment (when it was
        # queued); fall back to the enclosing line's timestamp.
        timestamp = attachment.get("timestamp") or line.get("timestamp")
        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": uuid,
            "provider_message_id_lower": uuid.lower() if uuid else None,
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "provider_parent_id": parent_uuid,
            "provider_parent_id_lower": parent_uuid.lower() if parent_uuid else None,
            "content_hash": self.hash_message(uuid, "user", prompt, timestamp),
            "role": "user",
            "content_text": content_text,
            "content_blocks": content_blocks,
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": not line.get("isSidechain", False),
            "provider_data": {
                "line": line,
                "cwd": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
                "version": line.get("version"),
                "queued_command": True,
                "command_mode": attachment.get("commandMode"),
            },
            "conversation_title": None,
            "conversation_metadata": {
                "project_path": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
            },
        }

    def _preserve_attachment(
        self,
        line: Dict[str, Any],
        attachment: Dict[str, Any],
        session_id: Optional[str],
        project_path: Optional[str],
    ) -> NormalizedMessage:
        """Preserve a non-``queued_command`` attachment as a hidden system record.

        These are context injections with no direct user signal, but they are
        still content the model was shown — keep a summary event (with the full
        raw attachment in provider_data) instead of dropping the line."""
        uuid = line.get("uuid", "")
        parent_uuid = line.get("parentUuid")
        timestamp = attachment.get("timestamp") or line.get("timestamp")
        atype = attachment.get("type") or "attachment"
        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0
        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": uuid,
            "provider_message_id_lower": uuid.lower() if uuid else None,
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "provider_parent_id": parent_uuid,
            "provider_parent_id_lower": parent_uuid.lower() if parent_uuid else None,
            "content_hash": self.hash_message(uuid, "system", attachment, timestamp),
            "role": "system",
            "content_text": f"[attachment: {atype}]",
            "content_blocks": [cast(ContentBlock, {
                "type": "attachment",
                "attachment_type": atype,
                "raw": attachment,
                "seq": 0,
            })],
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": not line.get("isSidechain", False),
            "is_visually_hidden": True,
            "provider_data": {
                "line": line,
                "attachment_type": atype,
                "cwd": project_path or line.get("cwd"),
            },
            "conversation_title": None,
            "conversation_metadata": {"project_path": project_path or line.get("cwd")},
        }

    def _parse_unknown_line(
        self,
        line: Dict[str, Any],
        line_type: str,
        session_id: Optional[str],
        project_path: Optional[str],
    ) -> NormalizedMessage:
        """Preserve a line whose ``type`` the parser doesn't model.

        Rather than dropping an unrecognized (or future) Claude Code line kind,
        keep it as a system record carrying the whole raw line — so nothing,
        including line kinds Anthropic adds later, vanishes without a trace."""
        uuid = line.get("uuid", "")
        parent_uuid = line.get("parentUuid")
        timestamp = line.get("timestamp")
        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0
        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": uuid,
            "provider_message_id_lower": uuid.lower() if uuid else None,
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "provider_parent_id": parent_uuid,
            "provider_parent_id_lower": parent_uuid.lower() if parent_uuid else None,
            "content_hash": self.hash_message(uuid, "system", line, timestamp),
            "role": "system",
            "content_text": f"[unrecognized line: {line_type or 'unknown'}]",
            "content_blocks": [cast(ContentBlock, {
                "type": "unknown_line",
                "line_type": line_type,
                "raw": line,
                "seq": 0,
            })],
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": not line.get("isSidechain", False),
            "provider_data": {"line": line, "line_type": line_type},
            "conversation_title": None,
            "conversation_metadata": {},
        }

    def _user_content_blocks_from_list(
        self, content: list
    ) -> "tuple[List[ContentBlock], str, List[Dict[str, Any]]]":
        """Build content blocks from a user message's list content.

        Delegates to ``claude_code_blocks.user_content_blocks_from_list``.
        Returns ``(content_blocks, content_text, ide_context_blocks)``.
        """
        return _blocks.user_content_blocks_from_list(content)

    def _parse_xml_function_calls(
        self, raw_content: str, seq: int
    ) -> Tuple[List[Dict[str, Any]], List[str], int]:
        """Parse old-format <function_calls> XML from string content.

        Delegates to ``claude_code_blocks.parse_xml_function_calls``.
        Returns ``(content_blocks, content_text_parts, updated_seq)``.
        """
        return _blocks.parse_xml_function_calls(raw_content, seq)

    def _assistant_content(
        self, raw_content: Any
    ) -> Tuple[List[ContentBlock], List[str]]:
        """Build (content_blocks, content_text_parts) for an assistant message.

        Delegates to ``claude_code_blocks.assistant_content``.
        """
        return _blocks.assistant_content(raw_content)

    def _append_assistant_block(
        self,
        block: Dict[str, Any],
        content_blocks: List[ContentBlock],
        content_text_parts: List[str],
        seq: int,
    ) -> int:
        """Append one list-shaped assistant block; return the next seq value.

        Delegates to ``claude_code_blocks.append_assistant_block``.
        """
        return _blocks.append_assistant_block(
            block, content_blocks, content_text_parts, seq
        )

    def _parse_assistant_message(
        self,
        line: Dict[str, Any],
        session_id: Optional[str],
        project_path: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Parse an assistant message line."""
        msg_data = line.get("message", {})
        if not msg_data:
            return None

        uuid = line.get("uuid", "")
        parent_uuid = line.get("parentUuid")
        timestamp = line.get("timestamp")
        raw_content = msg_data.get("content", [])
        model = msg_data.get("model", "")

        content_blocks, content_text_parts = self._assistant_content(raw_content)
        content_text = "\n".join(content_text_parts)

        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0

        # Extract usage info
        usage = msg_data.get("usage", {})

        # Annotation extras: the request/session identifiers already extracted
        # into provider_data top-level stay there (readers depend on them) AND
        # are mirrored here — annotations are what the builder persists onto the
        # api_request_completed payload, so this is what actually survives import.
        annotations = _line_annotations(line, _ASSISTANT_LINE_ANNOTATIONS)
        if msg_data.get("id") is not None:
            annotations["message_id"] = msg_data["id"]
        if msg_data.get("diagnostics") is not None:
            annotations["diagnostics"] = msg_data["diagnostics"]

        provider_data: Dict[str, Any] = {
            "line": line,
            "message_id": msg_data.get("id"),
            "model": model,
            "stop_reason": msg_data.get("stop_reason"),
            "usage": usage,
            # Per-message cost when the source records it (a pay-per-token harness
            # assistant message); None for subscription transcripts, dropped downstream.
            "cost": msg_data.get("cost"),
            "request_id": line.get("requestId"),
            "cwd": project_path or line.get("cwd"),
            "git_branch": line.get("gitBranch"),
            "version": line.get("version"),
        }
        if annotations:
            provider_data["annotations"] = annotations

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": uuid,
            "provider_message_id_lower": uuid.lower() if uuid else None,
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "provider_parent_id": parent_uuid,
            "provider_parent_id_lower": parent_uuid.lower() if parent_uuid else None,
            "content_hash": self.hash_message(uuid, "assistant", raw_content, timestamp),
            "role": "assistant",
            "content_text": content_text,
            "content_blocks": content_blocks,
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": not line.get("isSidechain", False),
            "provider_data": provider_data,
            "conversation_title": None,
            "conversation_metadata": {
                "project_path": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
                "model": model,
            },
        }

    def _parse_system_message(
        self,
        line: Dict[str, Any],
        session_id: Optional[str],
        project_path: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Parse a system message (command, event, etc.)."""
        uuid = line.get("uuid", "")
        parent_uuid = line.get("parentUuid")
        timestamp = line.get("timestamp")
        content = line.get("content", "")
        subtype = line.get("subtype", "")

        # A system line with no ``content`` string can still carry meaning in its
        # subtype / level / toolUseResult / compactMetadata (e.g. a
        # ``compact_boundary`` marker recording where compaction happened, and its
        # token counts). Synthesize a summary so the record — and its metadata in
        # provider_data — is preserved rather than dropped; only a wholly-empty
        # line (no content and no metadata at all) is skipped.
        if not content:
            carries_meta = any(
                line.get(k)
                for k in ("subtype", "level", "toolUseResult", "compactMetadata")
            )
            if not carries_meta:
                return None
            content = f"[system: {subtype}]" if subtype else "[system event]"

        content_blocks: List[ContentBlock] = [
            cast(ContentBlock, {
                "type": "system_context",
                "context_type": subtype or "system",
                "content": content,
                "seq": 0,
            })
        ]

        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": uuid,
            "provider_message_id_lower": uuid.lower() if uuid else None,
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "provider_parent_id": parent_uuid,
            "provider_parent_id_lower": parent_uuid.lower() if parent_uuid else None,
            "content_hash": self.hash_message(uuid, "system", content, timestamp),
            "role": "system",
            "content_text": content,
            "content_blocks": content_blocks,
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": not line.get("isSidechain", False),
            "is_visually_hidden": True,  # System messages are typically hidden
            "provider_data": {
                "line": line,
                "subtype": subtype,
                "level": line.get("level"),
                "cwd": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
                "version": line.get("version"),
            },
            "conversation_title": None,
            "conversation_metadata": {
                "project_path": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
            },
        }

    def _extract_tool_result_text(self, content: Any) -> str:
        """Extract text from tool result content (delegates to claude_code_blocks)."""
        return _blocks.extract_tool_result_text(content)

    def _parse_summary_message(
        self,
        line: Dict[str, Any],
        session_id: Optional[str],
        project_path: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Parse a context continuation summary message.

        When a conversation runs out of context, Claude creates a summary
        of prior messages to continue. These summaries contain valuable
        information about what was discussed and should be preserved.
        """
        uuid = line.get("uuid", "")
        parent_uuid = line.get("parentUuid")
        timestamp = line.get("timestamp")

        # Summary content is in the 'summary' field or 'content' field
        summary_text = line.get("summary", "") or line.get("content", "")
        if not summary_text and isinstance(line.get("message"), dict):
            summary_text = line["message"].get("content", "")

        # Extract which messages were summarized, if available
        summarizes = line.get("summarizes", [])
        if not summarizes and line.get("summarizedMessages"):
            summarizes = line.get("summarizedMessages", [])

        content_blocks: List[ContentBlock] = [{
            "type": "context_summary",
            "text": summary_text if isinstance(summary_text, str) else str(summary_text),
            "summarizes": summarizes if summarizes else None,
            "seq": 0,
        }]

        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": uuid or f"summary-{message_order}",
            "provider_message_id_lower": uuid.lower() if uuid else None,
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "provider_parent_id": parent_uuid,
            "provider_parent_id_lower": parent_uuid.lower() if parent_uuid else None,
            "content_hash": self.hash_message(uuid, "system", summary_text, timestamp),
            "role": "system",
            "content_text": f"[Context Summary]: {summary_text[:200]}..." if len(str(summary_text)) > 200 else f"[Context Summary]: {summary_text}",
            "content_blocks": content_blocks,
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": not line.get("isSidechain", False),
            "is_visually_hidden": False,  # Summaries may be interesting to see
            "provider_data": {
                "line": line,
                "summary_type": "context_continuation",
                "cwd": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
            },
            "conversation_title": None,
            "conversation_metadata": {
                "project_path": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
            },
        }

    def _parse_file_snapshot_message(
        self,
        line: Dict[str, Any],
        session_id: Optional[str],
        project_path: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Parse a file history snapshot message.

        Claude Code creates file snapshots to track the state of files
        at various points during a session. These are valuable for
        understanding what changed and when.
        """
        uuid = line.get("uuid", "")
        parent_uuid = line.get("parentUuid")
        timestamp = line.get("timestamp")

        # File snapshot data can be in various places
        files = line.get("files", [])
        if not files and isinstance(line.get("content"), list):
            files = line.get("content", [])
        if not files and isinstance(line.get("snapshot"), dict):
            files = line.get("snapshot", {}).get("files", [])

        content_blocks: List[ContentBlock] = [{
            "type": "file_snapshot",
            "files": files,
            "timestamp": timestamp,
            "seq": 0,
        }]

        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0

        # Create a summary text for display
        file_count = len(files) if isinstance(files, list) else 0
        content_text = f"[File Snapshot]: {file_count} file(s)"

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": uuid or f"file-snapshot-{message_order}",
            "provider_message_id_lower": uuid.lower() if uuid else None,
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "provider_parent_id": parent_uuid,
            "provider_parent_id_lower": parent_uuid.lower() if parent_uuid else None,
            "content_hash": self.hash_message(uuid, "system", str(files), timestamp),
            "role": "system",
            "content_text": content_text,
            "content_blocks": content_blocks,
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": not line.get("isSidechain", False),
            "is_visually_hidden": True,  # File snapshots are usually metadata
            "provider_data": {
                "line": line,
                "snapshot_type": "file-history",
                "cwd": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
            },
            "conversation_title": None,
            "conversation_metadata": {
                "project_path": project_path or line.get("cwd"),
                "git_branch": line.get("gitBranch"),
            },
        }

    def _parse_queue_operation(
        self,
        line: Dict[str, Any],
        session_id: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Parse a queue-operation line (enqueue/dequeue)."""
        timestamp = line.get("timestamp")
        operation = line.get("operation", "unknown")
        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": f"queue-{operation}-{message_order}",
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "content_hash": self.hash_message(f"queue-{operation}", "system", str(line), timestamp),
            "role": "system",
            "content_text": f"[Queue {operation}]",
            "content_blocks": [cast(ContentBlock, {
                "type": "queue_operation",
                "data": {
                    "operation": operation,
                    "session_id": line.get("sessionId"),
                    "timestamp": timestamp,
                },
                "seq": 0,
            })],
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": True,
            "is_visually_hidden": True,
            "provider_data": {"line_type": "queue-operation"},
        }

    def _parse_pr_link(
        self,
        line: Dict[str, Any],
        session_id: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Parse a pr-link line: the pull request this session is working on.

        Claude Code re-emits the marker on every turn the link is live, so all of
        them describe one association. The identity here is the PR itself
        (``provider_message_id``) and the payload carries no timestamp, so the
        repeats collapse to a single event under the ordinary content dedup — a
        session that opened one PR reads as one association, not as forty.

        Provenance, not chatter: this is the same class of fact as a commit an
        event's output shows being created, and it folds into the ``event_prs``
        projection behind ``thread_search(pr=...)``.
        """
        repo = line.get("prRepository")
        number = line.get("prNumber")
        url = line.get("prUrl")
        # The number is what the lookup keys on; a line without one describes no
        # PR we could find again, so it goes down the verbatim path instead.
        if number is None or not str(number).strip():
            return None
        timestamp = line.get("timestamp")
        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0
        ref = f"{repo}#{number}" if repo else f"#{number}"

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": f"pr-{ref}",
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "content_hash": self.hash_message(f"pr-{ref}", "system", ref, None),
            "role": "system",
            "content_text": f"[pull request {ref}]",
            "content_blocks": [cast(ContentBlock, {
                "type": "pr_link",
                "repo": repo,
                "number": number,
                "url": url,
                "seq": 0,
            })],
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": True,
            "is_visually_hidden": True,
            "provider_data": {"line_type": "pr-link"},
        }

    def _parse_progress_message(
        self,
        line: Dict[str, Any],
        session_id: Optional[str],
    ) -> Optional[NormalizedMessage]:
        """Parse a progress line (tool/MCP progress indicators)."""
        timestamp = line.get("timestamp")
        uuid = line.get("uuid", "")
        created_at = _parse_iso_timestamp(timestamp)
        message_order = _timestamp_to_order(timestamp) if timestamp else 0
        data = line.get("data", {})
        tool_use_id = line.get("toolUseID")

        return {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": uuid or f"progress-{message_order}",
            "provider_conversation_id": session_id or line.get("sessionId", ""),
            "content_hash": self.hash_message(uuid or f"progress-{message_order}", "system", str(data), timestamp),
            "role": "system",
            "content_text": f"[Progress: {data.get('type', 'unknown')}]",
            "content_blocks": [cast(ContentBlock, {
                "type": "progress",
                "data": data,
                "tool_use_id": tool_use_id,
                "seq": 0,
            })],
            "created_at": created_at,
            "updated_at": None,
            "message_order": message_order,
            "is_active_path": not line.get("isSidechain", False),
            "is_visually_hidden": True,
            "provider_data": {"line_type": "progress"},
        }

    def _coalesce_assistant_chains(
        self, messages: List[NormalizedMessage]
    ) -> List[NormalizedMessage]:
        """Coalesce linked assistant messages into single messages.

        Delegates to ``claude_code_blocks.coalesce_assistant_chains``.
        """
        return _blocks.coalesce_assistant_chains(messages)

    @staticmethod
    def _build_chain_indices(
        messages: List[NormalizedMessage],
    ) -> Tuple[Dict[str, NormalizedMessage], Dict[str, List[NormalizedMessage]]]:
        """Build (by_id, children_of) indices (delegates to claude_code_blocks)."""
        return _blocks.build_chain_indices(messages)

    @staticmethod
    def _find_chain_roots(
        messages: List[NormalizedMessage],
        by_id: Dict[str, NormalizedMessage],
    ) -> List[NormalizedMessage]:
        """Find chain roots (delegates to claude_code_blocks)."""
        return _blocks.find_chain_roots(messages, by_id)

    @staticmethod
    def _merge_chain(
        root: NormalizedMessage,
        children_of: Dict[str, List[NormalizedMessage]],
        to_remove: set,
    ) -> None:
        """BFS-merge a chain in place (delegates to claude_code_blocks)."""
        _blocks.merge_chain(root, children_of, to_remove)


__all__ = [
    "ClaudeCodeParser",
]
