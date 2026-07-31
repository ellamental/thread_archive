"""Query classification — what shape of lexical query the caller asked for.

Classification only; :mod:`.fts` builds the SQL each mode implies. A query
classifies into one of: ``browse`` (empty), ``or`` (pipe-separated), ``code``
(identifiers), or ``tsquery`` (natural-language / boolean / quoted-phrase).
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


def canonical_time_bound(dt: datetime) -> str:
    """Format a datetime in the store's canonical timestamp form: naive UTC,
    space-separated (``YYYY-MM-DD HH:MM:SS.ffffff``) — the form SQLite renders
    DateTime columns in and the FTS ``occurred_at`` strings hold, so lexicographic
    comparison against stored values is correct. An aware datetime is converted to
    UTC; a naive one is taken as already UTC."""
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


_RELATIVE_AGE = re.compile(r"^(\d+)([hdw])$")
_AGE_UNIT_S = {"h": 3600, "d": 86400, "w": 604800}


def resolve_relative_date(value: str, *, strict: bool = False, param: str = "bound") -> str:
    """Resolve a time bound to the canonical stored form (see
    :func:`canonical_time_bound`). Accepts a relative age — ``<N>h`` / ``<N>d`` /
    ``<N>w`` (e.g. '2h', '7d') — or any ISO timestamp.

    ``strict`` is the user-input boundary: an unparseable value raises a
    ``ValueError`` naming the grammar, because the stored form compares
    lexicographically — passed through raw, a value like ``'last week'`` becomes
    a filter that silently matches nothing. Internal callers hand in timestamps
    from records they trust and keep the lenient passthrough."""
    m = _RELATIVE_AGE.match(value.strip())
    if m:
        age = int(m.group(1)) * _AGE_UNIT_S[m.group(2)]
        return canonical_time_bound(datetime.now(timezone.utc) - timedelta(seconds=age))
    try:
        return canonical_time_bound(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        if strict:
            raise ValueError(
                f"{param} must be a relative age — '2h', '7d', '2w' — or an ISO "
                f"timestamp; got {value!r}"
            ) from None
        return value
