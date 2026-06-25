"""Filesystem discovery and loading of Claude Code session files.

Split out of ``claude_code.py``: these two helpers locate session JSONL files
under ``~/.claude/projects/`` and read one into a lines/parse-errors dict. They
are independent of ``ClaudeCodeParser`` (no class state) and are re-exported by
``claude_code`` so existing ``claude_code.<name>`` access keeps resolving.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def find_claude_code_sessions(
    base_path: Optional[str] = None,
    include_subagents: bool = True,
) -> List[Path]:
    """
    Find all Claude Code session files.

    Args:
        base_path: Optional base path. Defaults to ~/.claude/projects/
        include_subagents: Whether to include agent-*.jsonl files (default: True)

    Returns:
        List of paths to session JSONL files
    """
    if base_path:
        base = Path(base_path).expanduser()
    else:
        base = Path.home() / ".claude" / "projects"

    if not base.exists():
        return []

    sessions = []
    for project_dir in base.iterdir():
        if project_dir.is_dir():
            for session_file in project_dir.glob("*.jsonl"):
                is_subagent = session_file.name.startswith("agent-")
                if is_subagent and not include_subagents:
                    continue
                sessions.append(session_file)

    return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)


def load_claude_code_session(path: Path) -> Dict[str, Any]:
    """
    Load a Claude Code session file.

    Args:
        path: Path to the session JSONL file

    Returns:
        Dict with session_id, lines, parse_errors, and is_subagent flag
    """
    lines = []
    parse_errors = []
    session_id = path.stem  # Use filename without extension as session ID
    is_subagent = path.name.startswith("agent-")

    # Extract parent session ID from agent filename if applicable
    # Format: agent-{parent_session_id}.jsonl
    parent_session_id = None
    if is_subagent and session_id.startswith("agent-"):
        parent_session_id = session_id[6:]  # Remove "agent-" prefix

    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
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

    return {
        "session_id": session_id,
        "path": str(path),
        "lines": lines,
        "parse_errors": parse_errors,
        "is_subagent": is_subagent,
        "parent_session_id": parent_session_id,
    }
