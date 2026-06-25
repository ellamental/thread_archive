"""
Pipeline infrastructure for parser composition.

The pipeline pattern separates parsing into distinct phases:
1. Parse: Raw provider data -> RawMessage (intermediate representation)
2. Transform: RawMessage[] -> RawMessage[] (coalescing, merging, etc.)
3. Normalize: RawMessage -> NormalizedMessage (canonical format)
4. Validate: NormalizedMessage[] -> validation results

Each phase has a Protocol definition for pluggability.
"""

from .base import ParserPipeline
from .interfaces import (
    Normalizer,
    Parser,
    RawMessage,
    Transformer,
    Validator,
)

__all__ = [
    "RawMessage",
    "Parser",
    "Transformer",
    "Normalizer",
    "Validator",
    "ParserPipeline",
]
