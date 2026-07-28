"""Read + sanitize a provider JSONL transcript.

``errors="replace"`` so a half-written multibyte tail (a poll catching a file
mid-write) decodes to U+FFFD instead of raising; the per-line ``JSONDecodeError``
guard means one bad line is skipped, never the whole file. Each parsed line is
scrubbed of null bytes + lone UTF-16 surrogates at the ingest boundary — source
JSONL is written by external (JS/TS) processes that can slice strings on UTF-16
boundaries and leave an orphaned surrogate, which then fails ``json.dumps`` on the
write path.

The importers read the file's **bytes** once (:func:`read_source_bytes`) and parse
from that buffer (:func:`parse_session_lines`), because the same buffer is what
:mod:`._cursor` digests to prove the source was appended to rather than rewritten.
Reading twice would race a live writer: the bytes the cursor verified must be the
bytes the lines were parsed from.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

from . import _probe

logger = logging.getLogger(__name__)


def _scrub_str(s: str) -> str:
    if "\x00" in s:
        s = s.replace("\x00", "")
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogate(s): replace them rather than fail the whole import.
        s = s.encode("utf-8", "replace").decode("utf-8")
    return s


def sanitize_payload(obj: Any) -> Any:
    """Recursively scrub strings in a parsed JSON value (null bytes / lone surrogates)."""
    if isinstance(obj, str):
        return _scrub_str(obj)
    if isinstance(obj, dict):
        return {k: sanitize_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_payload(v) for v in obj]
    return obj


def read_source_bytes(session_path: Path) -> bytes:
    """The transcript's raw bytes — the one read the importers do per poll."""
    with _probe.timed("read_ms"):
        data = Path(session_path).read_bytes()
    _probe.count("bytes", len(data))
    return data


def parse_session_lines_counted(data: bytes, name: str = "<transcript>") -> tuple[list[dict], int]:
    """Parse every line of a JSONL transcript's bytes; skip (log) bad lines.

    Returns ``(lines, parse_errors)``. The count is how many source lines were
    dropped as unparseable — a dropped line is content the archive will never
    hold, so the importers carry the count out on their results (it feeds the
    watcher's health accounting) instead of it dying in the log.

    ``io.StringIO(..., newline=None)`` gives the same universal-newline line split
    as ``open()`` in text mode: only ``\\n`` (post-translation) ends a line, so a
    raw U+2028 inside a JSON string doesn't tear a valid line in half the way
    ``str.splitlines()`` would.
    """
    lines: list[dict] = []
    parse_errors = 0
    with _probe.timed("parse_ms"):
        text = data.decode("utf-8", errors="replace")
        for line_num, line in enumerate(io.StringIO(text, newline=None), 1):
            if line.strip():
                try:
                    lines.append(sanitize_payload(json.loads(line)))
                except json.JSONDecodeError as e:
                    parse_errors += 1
                    logger.warning("%s:%d — JSON parse error: %s", name, line_num, e)
    if parse_errors:
        logger.warning("%s: %d lines skipped due to parse errors", name, parse_errors)
    _probe.count("lines", len(lines))
    return lines, parse_errors


class LazyLines:
    """A transcript's parsed lines, parsed on first use.

    An importer has to read a source's *bytes* before it can say anything — the
    watermark's proof covers the whole file (:mod:`._cursor`). It does not have to
    **parse** them: the parse is only needed once that proof says the file changed
    under the cursor, and the common answer is that it didn't.

    Parsing eagerly makes that answer expensive. Every poll of every file whose
    fingerprint moved pays a full JSON pass over the whole transcript, and the
    worst case is the first poll after a restart, where the in-memory fingerprint
    cache is empty and *every* file in the archive is read and parsed to establish
    that almost none of them changed. Measured on this archive that is ~3M lines
    parsed to produce a handful of events, and it is the single largest line item
    in ingest. Deferred, an unchanged file costs a read and a digest and nothing
    else.

    ``transform`` post-processes the parsed lines for a provider whose importer
    works over a normalized shape rather than the raw ones (cowork), so its
    normalization is deferred with the parse rather than pinning it eager.

    :attr:`parse_errors` is zero until the parse actually happens, which is the
    honest reading: lines that were never parsed were never dropped. It also stops
    an unchanged file from re-reporting the same dropped lines on every poll — a
    cumulative counter that a restart storm inflates without any new loss.
    """

    __slots__ = ("_data", "_name", "_transform", "_lines", "_parse_errors")

    def __init__(self, data: bytes, name: str = "<transcript>", transform=None) -> None:
        self._data = data
        self._name = name
        self._transform = transform
        self._lines: Any = None
        self._parse_errors = 0

    def get(self) -> list[dict]:
        """The parsed lines, parsing on the first call and caching after."""
        if self._lines is None:
            lines, self._parse_errors = parse_session_lines_counted(self._data, self._name)
            self._lines = self._transform(lines) if self._transform is not None else lines
        return self._lines

    @property
    def parse_errors(self) -> int:
        """Lines dropped as unparseable — 0 until :meth:`get` has run."""
        return self._parse_errors

    @property
    def parsed(self) -> bool:
        """Whether the parse was actually needed on this pass."""
        return self._lines is not None


def parse_session_lines(data: bytes, name: str = "<transcript>") -> list[dict]:
    """:func:`parse_session_lines_counted` for callers that don't track the count."""
    return parse_session_lines_counted(data, name)[0]


def read_session_lines(session_path: Path) -> list[dict]:
    """Read + parse every line of a JSONL transcript; skip (log) bad lines."""
    session_path = Path(session_path)
    return parse_session_lines(read_source_bytes(session_path), session_path.name)
