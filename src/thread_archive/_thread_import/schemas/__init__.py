"""
JSON Schema validation for provider export formats.

Provides versioned schema validation with automatic version detection.
Schemas are stored as JSON files in provider-specific subdirectories.

Usage:
    from apps.chat_import.schemas import validate_export, detect_schema_version

    # Auto-detect and validate
    errors, warnings = validate_export(data, provider="chatgpt")

    # Or detect version explicitly
    version = detect_schema_version(data, provider="chatgpt")
    errors, warnings = validate_export(data, provider="chatgpt", version=version)
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


SCHEMA_DIR = Path(__file__).parent


def get_available_versions(provider: str) -> List[str]:
    """Get list of available schema versions for a provider."""
    provider_dir = SCHEMA_DIR / provider
    if not provider_dir.exists():
        return []

    versions = []
    for schema_file in provider_dir.glob("v*.json"):
        versions.append(schema_file.stem)  # e.g., "v1", "v2"

    return sorted(versions)


def load_schema(provider: str, version: str) -> Optional[Dict[str, Any]]:
    """Load a specific schema version."""
    schema_path = SCHEMA_DIR / provider / f"{version}.json"
    if not schema_path.exists():
        return None

    with open(schema_path) as f:
        return json.load(f)


def detect_schema_version(data: Any, provider: str) -> str:
    """
    Auto-detect schema version from data structure.

    Uses heuristics specific to each provider to determine which
    schema version the data matches.

    Returns the detected version string (e.g., "v1", "v2") or "v1" as default.
    """
    if provider == "chatgpt":
        return _detect_chatgpt_version(data)
    elif provider == "claude":
        return _detect_claude_version(data)
    elif provider == "claude-code":
        return _detect_claude_code_version(data)
    elif provider == "cursor":
        return _detect_cursor_version(data)

    return "v1"


def _detect_chatgpt_version(data: Any) -> str:
    """Detect ChatGPT export version.

    v2: Has thinking blocks (content_type: "thoughts")
    v1: Original format
    """
    if not isinstance(data, list):
        data = [data]

    for conv in data:
        mapping = conv.get("mapping", {})
        for node in mapping.values():
            msg = node.get("message")
            if msg:
                metadata = msg.get("metadata", {})
                content_type = metadata.get("content_type")
                if content_type in ("thoughts", "analysis", "reasoning_recap"):
                    return "v2"

    return "v1"


def _detect_claude_version(data: Any) -> str:
    """Detect Claude export version.

    v1: Current format (only version so far)
    """
    return "v1"


def _detect_claude_code_version(data: Any) -> str:
    """Detect Claude Code session version.

    v1: Current JSONL format (only version so far)
    """
    return "v1"


def _detect_cursor_version(data: Any) -> str:
    """Detect Cursor export version.

    v2: Has toolFormerData field
    v1: Original format
    """
    if isinstance(data, dict):
        bubbles = data.get("bubbles", [])
    elif isinstance(data, list):
        bubbles = data
    else:
        return "v1"

    for bubble in bubbles:
        if bubble.get("toolFormerData"):
            return "v2"

    return "v1"


def validate_export(
    data: Any,
    provider: str,
    version: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    """
    Validate export data against JSON Schema.

    Args:
        data: The export data to validate
        provider: Provider name (chatgpt, claude, claude-code, cursor)
        version: Schema version (auto-detected if not specified)

    Returns:
        Tuple of (errors, warnings) where:
        - errors: List of validation error messages
        - warnings: List of validation warning messages (unknown fields, etc.)
    """
    if not HAS_JSONSCHEMA:
        # Return empty if jsonschema not installed
        return [], ["jsonschema library not installed, skipping validation"]

    if version is None:
        version = detect_schema_version(data, provider)

    schema = load_schema(provider, version)
    if schema is None:
        # No schema file for this version - not an error, just a warning
        available = get_available_versions(provider)
        if available:
            return [], [f"No schema for {provider} {version}, available: {available}"]
        else:
            return [], [f"No schemas defined for provider {provider}"]

    errors: List[str] = []
    warnings: List[str] = []

    try:
        # Validate against schema
        validator = jsonschema.Draft7Validator(schema)

        for error in validator.iter_errors(data):
            # Categorize errors
            if error.validator == "additionalProperties":
                # Unknown field - warning not error
                warnings.append(f"Unknown field at {_format_path(error.path)}: {error.message}")
            elif error.validator == "required":
                # Missing required field - error
                errors.append(f"Missing required field at {_format_path(error.path)}: {error.message}")
            elif error.validator == "type":
                # Type mismatch - error
                errors.append(f"Type mismatch at {_format_path(error.path)}: {error.message}")
            else:
                # Other validation errors
                errors.append(f"Validation error at {_format_path(error.path)}: {error.message}")

    except Exception as e:
        errors.append(f"Schema validation failed: {str(e)}")

    return errors, warnings


def _format_path(path) -> str:
    """Format a JSON path for display."""
    if not path:
        return "root"
    parts = []
    for p in path:
        if isinstance(p, int):
            parts.append(f"[{p}]")
        else:
            parts.append(f".{p}")
    return "".join(parts).lstrip(".")


def get_schema_violations(
    data: Any,
    provider: str,
    version: Optional[str] = None,
) -> Dict[str, List[str]]:
    """
    Get detailed schema violations.

    Returns:
        Dict with keys:
        - unknown_fields: Fields not in schema
        - missing_fields: Required fields that are missing
        - type_mismatches: Fields with wrong types
        - other_errors: Other validation errors
    """
    errors, warnings = validate_export(data, provider, version)

    return {
        "unknown_fields": [w for w in warnings if "Unknown field" in w],
        "missing_fields": [e for e in errors if "Missing required" in e],
        "type_mismatches": [e for e in errors if "Type mismatch" in e],
        "other_errors": [e for e in errors if "Missing required" not in e and "Type mismatch" not in e],
    }
