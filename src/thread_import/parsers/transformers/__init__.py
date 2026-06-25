"""
Transformers for the parser pipeline.

Transformers modify RawMessage lists between parse and normalize phases.
Common uses:
- Coalescing tool messages into parents (ChatGPT)
- Merging thinking blocks (Claude Code)
- Computing active paths (ChatGPT)
- Extracting IDE context (Claude Code)
"""

from .active_path import ActivePathTransformer
from .coalescing import ToolCoalescingTransformer
from .ide_context import IDEContextTransformer
from .thinking_merge import ThinkingMergeTransformer

__all__ = [
    "ToolCoalescingTransformer",
    "ActivePathTransformer",
    "ThinkingMergeTransformer",
    "IDEContextTransformer",
]
