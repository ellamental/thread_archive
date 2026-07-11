"""Canonical tool name mapping from provider-specific names to Thread's standard names.

Thread uses Claude Code's tool names as the canonical standard (Edit, Write, Read, Bash, etc.)
since those are the de facto standard in the database. Provider-specific names (edit_file_v2,
run_terminal_cmd, etc.) are mapped to these canonical names during import.

The original provider name is preserved as `provider_tool_name` on content blocks and events.
"""

# Provider tool name → Thread canonical name
TOOL_NAME_MAP: dict[str, str] = {
    # Cursor file tools
    "edit_file_v2": "Edit",
    "edit_file": "Edit",
    "write_file": "Write",
    "read_file": "Read",
    "read_file_v2": "Read",
    # Cursor terminal tools
    "run_terminal_cmd": "Bash",
    "run_terminal_command_v2": "Bash",
    # Cursor search/navigation tools
    "list_dir": "Glob",
    "list_dir_v2": "Glob",
    "search_files": "Grep",
    "codebase_search": "Grep",
    "grep_search": "Grep",
}

# Canonical tool names that operate on files — normalized events MUST have input.file_path
FILE_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "Read"})

# Provider-specific field name that holds the file path in the tool's input dict.
# None means the input doesn't contain the path at all (must recover from code_blocks).
PROVIDER_PATH_FIELDS: dict[str, str | None] = {
    "read_file_v2": "path",
    "read_file": "path",
    "edit_file": None,
    "edit_file_v2": None,
    "write_file": "filePath",
}


def normalize_tool_name(name: str) -> str:
    """Map a provider tool name to Thread's canonical name.

    Returns the original name unchanged if no mapping exists.
    """
    return TOOL_NAME_MAP.get(name, name)
