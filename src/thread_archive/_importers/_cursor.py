"""The append-proving cursor for the JSONL line-stream importers.

A file watermark has to answer one question: *are the lines I already imported still
the first lines of this file?* Size alone cannot answer it. A rewritten line of the
same serialized length, a truncate-and-regrow between polls, or a malformed line
repaired in place all leave the size equal or larger while the content underneath the
cursor changed — and a line-count cursor then indexes into content that no longer
means what it meant, skipping the difference for good.

So the watermark carries a **digest of the exact bytes it was computed over**
(``ImportState.last_content_hash``, the sha256 of the whole file as of that import).
On the next poll we hash the current file's first ``last_file_size`` bytes: match ⇒
the file was only appended to, and the line cursor is still valid; mismatch (or a
file now shorter than the watermark) ⇒ the source was rewritten, so we rewind to line
0 and re-import the whole thing. The ``dedup_key`` membership check collapses
everything already held, so a rewind costs work, never duplicates.

That one check subsumes several silent-loss modes at once: same-size rewrites, a
shrink, and — the sharp one — the *logical* line cursor drifting out of step with the
file. ``read_session_lines`` drops malformed lines, so the cursor counts parsed lines,
not physical ones; repairing a malformed line in the middle of a file shifts every
later line's index and the tail slice would silently skip a turn. A repair changes the
prefix bytes, so it rewinds instead.

A torn final line — a poll catching the writer mid-append — is *not* a rewrite: the
partial bytes are a prefix of the completed line, so the digest still matches and the
unparsed tail simply imports on the next poll.

Watermarks written before this hash existed carry ``last_content_hash = None``: they
can't be verified, so they keep the old size-only behavior for exactly one poll and
are backfilled with a digest as they go (a blanket rewind of every source would be a
correct but pointlessly expensive way to learn nothing).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from .._store import ImportState

logger = logging.getLogger(__name__)


def content_digest(data: bytes) -> str:
    """The watermark's proof-of-content: sha256 of the bytes the cursor covers."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SourceCursor:
    """Where to resume in a re-read source file, and the digest to watermark it with.

    ``unchanged`` = byte-identical to the last import (nothing to do). ``rewound`` =
    the source was rewritten under us, so ``start_line`` is 0 and the whole file
    re-imports.
    """

    start_line: int
    content_hash: str
    unchanged: bool = False
    rewound: bool = False


def resolve_source_cursor(
    state: Optional[ImportState], data: bytes, *, source: str, source_id: str
) -> SourceCursor:
    """Resolve where this poll should resume in ``data`` (see the module docstring)."""
    size = len(data)
    digest = content_digest(data)

    if state is None:
        return SourceCursor(start_line=0, content_hash=digest)

    start_line = state.last_line_count
    last_size = state.last_file_size

    if not state.last_content_hash:
        # Unverifiable pre-digest watermark: trust size for one more poll, and stamp a
        # digest so the next one is provable.
        if size == last_size:
            return SourceCursor(start_line, digest, unchanged=True)
        if size < last_size:
            return _rewind(source, source_id, f"source shrank ({last_size} → {size} bytes)", digest)
        return SourceCursor(start_line, digest)

    if size == last_size and digest == state.last_content_hash:
        return SourceCursor(start_line, digest, unchanged=True)
    if size < last_size:
        return _rewind(source, source_id, f"source shrank ({last_size} → {size} bytes)", digest)
    if content_digest(data[:last_size]) != state.last_content_hash:
        return _rewind(source, source_id, "content under the cursor changed", digest)
    return SourceCursor(start_line, digest)


def _rewind(source: str, source_id: str, why: str, digest: str) -> SourceCursor:
    logger.warning(
        "%s %s: %s — rewinding cursor, re-importing from line 0 (dedup collapses what we hold)",
        source, source_id, why,
    )
    return SourceCursor(start_line=0, content_hash=digest, rewound=True)
