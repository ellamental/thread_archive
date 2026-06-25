"""
IDE context transformer for Claude Code.

Claude Code embeds IDE context (opened files, selections) as XML-like
tags in user messages. This transformer extracts them as structured
ide_context content blocks.
"""

import re
from typing import Any, Dict, List, Optional, Tuple, cast

from ..base import ContentBlock
from ..pipeline.interfaces import RawMessage

# Regex patterns for IDE context tags
_IDE_OPENED_FILE_PATTERN = re.compile(
    r"<ide_opened_file>(.*?)</ide_opened_file>", re.DOTALL
)
_IDE_SELECTION_PATTERN = re.compile(
    r"<ide_selection>(.*?)</ide_selection>", re.DOTALL
)


class IDEContextTransformer:
    """Extracts IDE context from user messages.

    Claude Code embeds IDE state in user messages as XML-like tags:
    - <ide_opened_file>...</ide_opened_file>
    - <ide_selection>...</ide_selection>

    This transformer:
    1. Finds IDE context tags in message content
    2. Extracts them as ide_context content blocks
    3. Updates the message text to remove the tags

    Note: This operates on text content, so should run after
    basic content block extraction.
    """

    def transform(self, messages: List[RawMessage]) -> List[RawMessage]:
        """Extract IDE context from messages.

        Args:
            messages: Messages to transform

        Returns:
            Messages with IDE context extracted
        """
        for msg in messages:
            if msg.role != "user":
                continue

            self._extract_ide_context(msg)

        return messages

    def _extract_ide_context(self, msg: RawMessage) -> None:
        """Extract IDE context from a user message.

        Modifies msg.content_blocks in place.
        """
        # Find text blocks to process
        text_blocks = [
            (i, b)
            for i, b in enumerate(msg.content_blocks)
            if b.get("type") == "text"
        ]

        if not text_blocks:
            # Try raw content if no blocks yet
            if isinstance(msg.content, str):
                cleaned, ide_blocks = self._extract_from_text(msg.content)
                if ide_blocks:
                    # Update content
                    msg.content = cleaned
                    # Add ide_context blocks
                    base_seq = len(msg.content_blocks)
                    for i, block in enumerate(ide_blocks):
                        block["seq"] = base_seq + i
                        msg.content_blocks.append(cast(ContentBlock, block))
                    # Update existing text block if present
                    if msg.content_blocks and msg.content_blocks[0].get("type") == "text":
                        cast(Dict[str, Any], msg.content_blocks[0])["text"] = cleaned
            return

        # Process each text block
        ide_blocks_to_add: List[Dict[str, Any]] = []
        for idx, block in text_blocks:
            text = block.get("text", "")
            if not text:
                continue

            cleaned, extracted = self._extract_from_text(text)

            # Update the text block
            cast(Dict[str, Any], block)["text"] = cleaned

            # Collect IDE blocks
            ide_blocks_to_add.extend(extracted)

        # Add IDE blocks at the end
        if ide_blocks_to_add:
            base_seq = len(msg.content_blocks)
            for i, block in enumerate(ide_blocks_to_add):
                block["seq"] = base_seq + i
                msg.content_blocks.append(cast(ContentBlock, block))

    def _extract_from_text(
        self, text: str
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Extract IDE context tags from text.

        Returns:
            Tuple of (cleaned_text, list of ide_context blocks)
        """
        ide_blocks: List[Dict[str, Any]] = []

        # Extract <ide_opened_file> tags
        for match in _IDE_OPENED_FILE_PATTERN.finditer(text):
            content = match.group(1).strip()
            file_path = self._extract_file_path(content)

            ide_blocks.append({
                "type": "ide_context",
                "context_type": "opened_file",
                "file_path": file_path,
                "raw_content": content,
                "seq": 0,  # Will be renumbered
            })

        # Extract <ide_selection> tags
        for match in _IDE_SELECTION_PATTERN.finditer(text):
            content = match.group(1).strip()

            ide_blocks.append({
                "type": "ide_context",
                "context_type": "selection",
                "raw_content": content,
                "seq": 0,  # Will be renumbered
            })

        # Remove tags from text
        cleaned = _IDE_OPENED_FILE_PATTERN.sub("", text)
        cleaned = _IDE_SELECTION_PATTERN.sub("", cleaned)
        cleaned = cleaned.strip()

        return cleaned, ide_blocks

    def _extract_file_path(self, content: str) -> Optional[str]:
        """Extract file path from opened file content.

        Content is typically: "The user opened the file {path} in the IDE..."
        """
        if "opened the file " in content:
            path_match = re.search(r"opened the file ([^\s]+)", content)
            if path_match:
                return path_match.group(1)
        return None
