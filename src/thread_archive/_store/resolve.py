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

How a session id sits inside a ``source_id`` is the provider's own knowledge —
claude-code stores ``{project}:{uuid}``, codex ``rollout-{ts}-{uuid}``, most
providers the bare uuid — so the shapes are read from the provider registry
rather than kept here. A provider that never declares a separator is resolvable
only by its exact ``source_id``, which is correct: there is no prefix to skip.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ImportState, Thread


def _like_literal(text: str) -> str:
    """Escape LIKE wildcards so ``text`` matches itself and nothing else.

    An unescaped ``_`` matches any character, which would let a ref silently
    resolve to the wrong thread. Separators are escaped alongside refs because a
    provider is free to declare ``_`` as its own separator."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def source_id_matches(col, ref: str, separators: "tuple[str, ...]" = ()):
    """SQL predicate: ``col`` equals ``ref``, or ends in ``<separator><ref>``.

    ``separators`` are the ones the provider declares (see
    ``Provider.session_id_separators``). Empty — the default — is exact match
    only, which is the whole predicate for a provider whose ``source_id`` is the
    session id."""
    cond = col == ref
    escaped = _like_literal(ref)
    for sep in separators:
        cond = cond | col.like(f"%{_like_literal(sep)}{escaped}", escape="\\")
    return cond


def _separators_for(source: Optional[str]) -> tuple[str, ...]:
    """The declared separators for ``source``, or the union across providers.

    Imported here rather than at module scope: the registry builds every
    provider's watcher and importer, which import this package. It is cached
    after the first read, so the cost lands once per process.
    """
    from .._providers import session_id_separators

    return session_id_separators(source)


def resolve_session_source_id(
    s: Session, ref: str, *, source: Optional[str] = None
) -> Optional[int]:
    """The thread id a provider session id refers to, or None.

    ``Thread.source_id`` first (newest thread wins), then the ``ImportState``
    watermarks (newest import wins). ``source`` narrows both to one provider —
    an editor knows its own; omit it to resolve across all of them.

    Naming the source narrows the *shape* too: only that provider's declared
    separators are tried, instead of every separator any provider declares. A
    bare uuid is ambiguous by construction, so the caller that knows where it
    came from gets an unambiguous answer."""
    separators = _separators_for(source)
    by_thread = (
        select(Thread.id)
        .where(source_id_matches(Thread.source_id, ref, separators))
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
        .where(source_id_matches(ImportState.source_id, ref, separators))
        .order_by(ImportState.last_import_at.desc())
    )
    if source:
        by_watermark = by_watermark.where(ImportState.source == source)
    return s.execute(by_watermark).scalars().first()
