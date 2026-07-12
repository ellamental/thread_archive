"""SQLite-native server defaults for the archive ORM.

These compile to their SQLite forms only (there are no postgres
``@compiles(..., "postgresql")`` variants):

    now_default()        -> CURRENT_TIMESTAMP
    text_default("x")    -> 'x'
    empty_text_array()   -> '[]'   (a JSON empty array)

Use via ``mapped_column(..., server_default=now_default())``.
"""

from __future__ import annotations

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression


class now_default(expression.FunctionElement):
    """Current timestamp: ``CURRENT_TIMESTAMP``."""

    inherit_cache = True


@compiles(now_default)
def _now_default(element, compiler, **kw):
    return "CURRENT_TIMESTAMP"


class text_default(expression.FunctionElement):
    """A text-literal default: ``'value'``."""

    # Carries per-instance state, so it is not safe to share a cached compilation.
    inherit_cache = False

    def __init__(self, value: str, cast: str = "text"):
        self.value = value
        # `cast` is accepted for call-site parity with a `::text`-cast spelling;
        # unused on SQLite (and not stored: it would shadow FunctionElement.cast()).
        del cast
        super().__init__()


@compiles(text_default)
def _text_default(element, compiler, **kw):
    return f"'{element.value}'"


class empty_text_array(expression.FunctionElement):
    """Empty-array default: ``'[]'`` (a JSON array)."""

    inherit_cache = True


@compiles(empty_text_array)
def _empty_text_array(element, compiler, **kw):
    return "'[]'"
