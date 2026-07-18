"""Provider session-id → archive thread-id resolution.

The one place that knows how a provider session id relates to a stored
``source_id``. Two tables can hold the mapping, and neither is a superset:

- ``Thread.source_id`` — every imported conversation (export importers never
  write ``ImportState``);
- ``ImportState.source_id`` — the watcher's watermarks, which outlive merges:
  a claude-code compaction continuation skips ``create_thread`` (its events
  land in the original thread) but still upserts its own watermark, so the
  continuation's session uuid exists *only* here.

Both the MCP reader (``resolve_thread_ref``) and the web viewer's
``resolve_archive_link`` resolve through this union; keeping them on one
function is what stops them answering differently for the same uuid.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ImportState, Thread

# The separator-suffix forms the watcher stores: claude-code ``{project}:{uuid}``
# (":"), codex ``rollout-{ts}-{uuid}`` ("-"); others store the bare uuid.
_SESSION_ID_SEPARATORS = (":", "-")


def source_id_matches(col, ref: str):
    """SQL predicate: ``col`` equals ``ref`` or ends in ``<separator><ref>``.

    LIKE wildcards in the ref are escaped so it matches literally — an
    unescaped ``_`` would let a ref silently resolve to the wrong thread."""
    cond = col == ref
    escaped = ref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    for sep in _SESSION_ID_SEPARATORS:
        cond = cond | col.like(f"%{sep}{escaped}", escape="\\")
    return cond


def resolve_session_source_id(
    s: Session, ref: str, *, source: Optional[str] = None
) -> Optional[int]:
    """The thread id a provider session id refers to, or None.

    ``Thread.source_id`` first (newest thread wins), then the ``ImportState``
    watermarks (newest import wins). ``source`` narrows both to one provider —
    an editor knows its own; omit it to resolve across all of them."""
    by_thread = (
        select(Thread.id)
        .where(source_id_matches(Thread.source_id, ref))
        .order_by(Thread.updated_at.desc())
    )
    if source:
        by_thread = by_thread.where(Thread.source == source)
    tid = s.execute(by_thread).scalars().first()
    if tid is not None:
        return tid

    by_watermark = (
        select(ImportState.thread_id)
        .where(ImportState.thread_id.isnot(None))
        .where(source_id_matches(ImportState.source_id, ref))
        .order_by(ImportState.last_import_at.desc())
    )
    if source:
        by_watermark = by_watermark.where(ImportState.source == source)
    return s.execute(by_watermark).scalars().first()
