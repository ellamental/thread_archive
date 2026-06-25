"""Query classification — the shared FTS query-mode classifier.

Just the query-mode classification; the SQLite arm builds its own SQL in
:mod:`.fts`. A query classifies into one of: ``browse`` (empty), ``or``
(pipe-separated), ``code`` (identifiers), or ``tsquery`` (natural-language /
boolean / quoted-phrase).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_OPERATORS = ("AND", "OR", "NOT")
_BOOLEAN_RE = re.compile(r"\b(AND|OR|NOT)\b")
# A quoted span (kept whole) OR a run of non-whitespace.
_TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def _tokenize(query: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(query):
        tok = m.group(0)
        if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
            inner = tok[1:-1].strip()
            if inner:
                tokens.append(("PHRASE", inner))
        elif tok in _OPERATORS:
            tokens.append(("OP", tok))
        else:
            tokens.append(("WORD", tok))
    return tokens


def has_boolean_operators(query: str) -> bool:
    """True iff the query uses an AND/OR/NOT operator outside any quoted phrase."""
    if not query or not _BOOLEAN_RE.search(query):
        return False
    return any(kind == "OP" for kind, _ in _tokenize(query))


def classify_query(query: str) -> tuple[str, bool]:
    """Classify ``query`` into ``(mode, is_boolean)``.

    Precedence: a boolean (AND/OR/NOT) query wins over the pipe-OR and
    code-identifier paths so its operators aren't treated as literal text.
    """
    if not query or not query.strip():
        return "browse", False
    is_boolean = has_boolean_operators(query)
    if is_boolean:
        return "tsquery", True
    if "|" in query:
        return "or", False
    if bool(re.search(r"[_]|::|(?<=\w)\.(?=\w)", query)):
        return "code", False
    return "tsquery", False


def resolve_relative_date(value: str) -> str:
    """Resolve a relative ``<N>d`` window (e.g. '7d') to an ISO timestamp N days
    before now; any other value passes through unchanged."""
    if value.endswith("d") and value[:-1].isdigit():
        return (datetime.now(timezone.utc) - timedelta(days=int(value[:-1]))).isoformat()
    return value
