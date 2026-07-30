"""The conversation schema: Thread, Event, EventFts, ImportState.

The event log is the spine. ``Event`` is the append-only source of truth — a
conversation's content lives in ``Event.payload`` (JSON); everything a reader sees
is reconstructed from events. ``EventFts`` is the lexical search shadow (the FTS5
virtual table is built over it). ``ImportState`` carries per-source
watermarks so incremental import is idempotent.

Column types and server defaults use their SQLite-native forms (``_types``,
``_defaults``). ``thoughts`` / ``content_blocks`` / ``tool_calls`` /
``api_requests`` — the chat-harness materialization (live cognition) — are
deliberately *not* modeled here: the archive imports transcripts into events, it
does not run the chat loop.

Index ``__table_args__`` carry ``postgresql_where`` / ``postgresql_using`` kwargs;
SQLAlchemy ignores dialect-prefixed index options on SQLite, so they are inert (a
partial/GIN index degrades to a plain index).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base
from ._defaults import now_default, text_default
from ._types import ARRAY, JSONB, REAL, BigIntPK
from .ulid import mint_ulid


class Thread(Base):
    """An imported conversation, or a topic.

    ``thread_type`` has two kinds this package creates: 'conversation' (a chat
    session — the default, what importers create) and 'system' (a
    subagent/machinery run — captured, but out of default search and browse;
    retrieval's ``agents``/``types`` controls reach it). Legacy imports carry
    other type strings (canvas, patch, outliner, …), and the truth format's
    extension region mints its own ('topic' — see
    :mod:`thread_archive._knowledge`); readers treat the column as an open
    vocabulary. An extension modeling its nodes *as* threads is why: it puts
    them in one id space with the conversations they reference.

    ``id`` is a ULID (26-char Crockford base32; see :mod:`.ulid`) minted at
    creation — lexicographic order is start-time order, and ids are globally
    unique so archives can merge without rewriting. ``legacy_id`` carries the
    integer id a thread had before ULIDs; it is a permanent alias, not a
    transition shim — archived conversations are full of pasted integer ids, and
    ``resolve_thread_ref`` keeps them resolvable forever. New threads have none.
    ``source`` tracks origin
    ('claude-code', 'cursor', 'codex', ...). ``source_metadata`` (JSON) carries branching
    info (branched_from, branch_event_id, quoted_event_id, ...).
    """

    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=mint_ulid)
    legacy_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    name: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(Text, default=None)
    thread_type: Mapped[str] = mapped_column(
        Text, default="conversation", server_default=text_default("conversation")
    )
    description: Mapped[str | None] = mapped_column(Text, default=None)
    search_description: Mapped[str | None] = mapped_column(Text, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    indexed_summary: Mapped[str | None] = mapped_column(Text, default=None)

    source: Mapped[str | None] = mapped_column(Text, default=None)
    source_id: Mapped[str | None] = mapped_column(Text, default=None)
    source_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    thought_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    user_id: Mapped[str | None] = mapped_column(Text, default=None)
    experiment_id: Mapped[str | None] = mapped_column(Text, default=None)

    archived: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    # Per-thread search blacklist: when true, the thread's events are dropped from
    # search results (filtered post-federation). Independent of `archived`; the
    # thread stays reachable by id and title. A discovery filter, not access control.
    exclude_from_search: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )

    # Workspace isolation: 'current' = live data; 'legacy' = frozen, read-only.
    workspace: Mapped[str] = mapped_column(
        "workspace", String(16), nullable=False, server_default=text("'current'")
    )

    # Topic kind + epistemological type — knowledge-layer fields kept as plain
    # nullable columns here; the vocabularies/validators live in the knowledge layer.
    topic_kind: Mapped[str | None] = mapped_column(default=None)
    epistemological_type: Mapped[str | None] = mapped_column(default=None)

    inserted_at: Mapped[datetime] = mapped_column(
        "inserted_at", DateTime(timezone=True), nullable=False, server_default=now_default()
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=now_default()
    )

    __table_args__ = (
        Index("idx_threads_source", "source", "source_id"),
        Index("idx_threads_type", "thread_type"),
        Index("idx_threads_updated", text("updated_at DESC")),
        Index("idx_threads_user", "user_id"),
        Index("idx_threads_experiment", "experiment_id"),
        Index("idx_threads_archived", "archived"),
        Index(
            "idx_threads_exclude_from_search",
            "exclude_from_search",
            postgresql_where=text("exclude_from_search = true"),
        ),
        Index("idx_threads_source_metadata", "source_metadata", postgresql_using="gin"),
        Index("idx_threads_workspace_type", "workspace", "thread_type"),
        Index("idx_threads_epistemic_type", "epistemological_type"),
        Index("idx_threads_topic_kind", "topic_kind"),
        # Unique among the threads that have one; NULLs (post-ULID threads) are
        # exempt, and SQLite unique indexes permit any number of NULLs.
        Index("uq_threads_legacy_id", "legacy_id", unique=True),
    )


class Event(Base):
    """A single event in the append-only event log — the source of truth.

    ``id`` is the global monotonic sequence number. ``payload`` (JSON) holds
    event-type-specific content (text deltas, tool calls, message bodies, ...).
    ``dedup_key`` is a deterministic, timestamp-free natural identity (see the
    importer's ``compute_dedup_key``); NULL when an event lacks a stable identity.
    It is bare — dedup is thread-scoped by the importer's ``WHERE thread_id = ...``
    clause, so the key carries no ``{thread_id}:`` prefix. A partial UNIQUE index
    on ``(thread_id, dedup_key)`` enforces that identity at the DB level: the
    importer's membership check is advisory (it reads only what SQLite holds, so
    a truth append whose commit was lost re-imports the same content under a
    fresh id), and the reindex loader's INSERT OR REPLACE collapses such
    same-content twins to one row instead of materializing both.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
    stream_id: Mapped[str] = mapped_column(Text)
    api_call_id: Mapped[str | None] = mapped_column(Text, default=None)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        "recorded_at", DateTime(timezone=True), nullable=False, server_default=now_default()
    )
    caused_by_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, default=None)
    dedup_key: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        Index("idx_events_thread_id", "thread_id", "id"),
        Index("idx_events_type", "event_type"),
        Index("idx_events_type_thread", "event_type", "thread_id"),
        Index("idx_events_occurred", "occurred_at"),
        Index("idx_events_caused_by", "caused_by_event_id"),
        Index("idx_events_api_call_id", "api_call_id"),
        Index("idx_events_api_call", "api_call_id", postgresql_where=text("(api_call_id IS NOT NULL)")),
        Index("idx_events_correlation", "correlation_id", postgresql_where=text("(correlation_id IS NOT NULL)")),
        Index("idx_events_stream_seq", "stream_id", "id"),
        # DB-level dedup: one row per (thread, natural identity). Partial — NULL
        # dedup_keys (events with no stable identity) are exempt. On a live index
        # created before this index existed, enforcement arrives with the next
        # reindex (create_all does not retrofit indexes onto existing tables).
        Index(
            "uq_events_thread_dedup",
            "thread_id",
            "dedup_key",
            unique=True,
            sqlite_where=text("dedup_key IS NOT NULL"),
            postgresql_where=text("(dedup_key IS NOT NULL)"),
        ),
        # AUTOINCREMENT: keep a persistent id high-water (sqlite_sequence) that DELETE
        # does NOT reset. reindex clears + reloads with explicit ids; without this a
        # concurrent writer mid-reindex gets a low rowid (max+1 of the partially loaded
        # table) that collides with a not-yet-reloaded historical row. With it, new ids
        # always continue past the high-water and are never reused. The live watcher
        # mints event ids on insert, so a reindex running against a live watcher must
        # not be able to recycle a historical id. See truth.jsonl_log.reindex.
        # (Thread ids are ULIDs minted in application code, so threads need none of
        # this machinery.)
        {"sqlite_autoincrement": True},
    )


class EventFts(Base):
    """Full-text search shadow for events.

    Populated by application code as searchable events land; the FTS5 virtual
    table (``event_search``) is an external-content index over these rows — this
    table IS the stored corpus, mirrored into the index by the sync triggers (see
    ``_retrieval.fts``). ``event_id`` references ``events.id`` (a soft reference,
    no FK by design). ``occurred_at`` holds the store's canonical timestamp text
    so the search surface can filter time lexicographically.
    """

    __tablename__ = "events_fts"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(Text, default=None)
    tool_name: Mapped[str | None] = mapped_column(Text, default=None)
    occurred_at: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        Index("idx_events_fts_event_id", "event_id"),
        Index("idx_events_fts_thread_id", "thread_id"),
        Index("idx_events_fts_tool_name", "tool_name"),
        # The embed drain's pending-doc select groups this table by
        # ``(event_id, content_type)`` and walks it newest-first, taking a batch off the
        # top (``_retrieval.vectors.index_events_local``). Both halves of that matter:
        # the pair is the group key, so an index over it means SQLite groups by walking
        # rather than by building a temp b-tree over every indexed line in the corpus —
        # and DESC on *both* columns is what lets the same walk satisfy the ORDER BY,
        # which is what lets the LIMIT stop early. Ordered rather than plain because a
        # sorted grouping the query then re-sorts is a full scan either way: the drain
        # runs every poll and needs one batch, so the difference is work bounded by the
        # batch against work bounded by the corpus. ``_retrieval.vectors.ensure_index``
        # also creates this on demand — the drain is hot enough that waiting for a
        # reindex to retrofit it is the wrong trade.
        Index("idx_events_fts_pending", text("event_id DESC"), text("content_type DESC")),
        # The thread-meta docs (titles/summaries) are a ~1% slice of this table that
        # the maintenance sync reads in full every pass. Unindexed, finding them is a
        # scan of the whole shadow — every indexed line of every conversation — to
        # return a few thousand rows, and it grows with the corpus rather than with
        # what changed. Mirrors ``idx_events_type`` on ``events``. Plain rather than
        # partial on purpose: the reader binds the type as a parameter, and SQLite
        # cannot match a partial index's predicate against a bound value, so a
        # partial one would be built and then never used.
        Index("idx_events_fts_event_type", "event_type"),
    )


class EventPath(Base):
    """One file a tool event touched — the code axis of the archive.

    A disposable projection of the event log, like :class:`EventFts`: the paths are
    already in the events (an ``Edit``'s ``file_path``, an ``apply_patch``'s header,
    a shell command's arguments), and this table is the *structured* copy that makes
    "which conversations edited rank.py" an indexed lookup instead of a text search
    that happens to match a path. Extraction is
    :func:`thread_archive._retrieval._paths.extract_paths`; the fold is
    :mod:`thread_archive._retrieval.code`.

    ``path`` is normalized (posix separators, ``..`` collapsed, relative resolved
    against the thread's working directory) so one file has one spelling across
    sessions; ``basename`` is the same path's final segment, indexed because a bare
    name is what a caller actually types. ``op`` is the verb — ``read`` / ``edit`` /
    ``write`` / ``delete`` are direct touches, ``search`` (a grep's scope) and
    ``run`` (a path inside a command line) are weaker mentions kept distinguishable
    rather than dropped. ``event_id`` / ``thread_id`` are soft references (no FK,
    mirroring :class:`EventFts`) so the projection never constrains a reindex's bulk
    reload.
    """

    __tablename__ = "event_paths"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    basename: Mapped[str] = mapped_column(Text)
    op: Mapped[str] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(Text, default=None)
    occurred_at: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        # A bare-name query is the common one and cuts the table hardest, so it
        # leads; the path index serves prefix (directory) and exact lookups, and
        # the composite serves "what did this thread touch" without a table scan.
        Index("idx_event_paths_basename", "basename"),
        Index("idx_event_paths_path", "path"),
        Index("idx_event_paths_thread", "thread_id", "path"),
        Index("idx_event_paths_event", "event_id"),
    )


class EventCommit(Base):
    """A commit an event's tool output shows being *created* — the other half of the
    code axis, and what closes the loop from ``git blame`` to the conversation.

    A projection like :class:`EventPath`, folded by the same pass. Only
    commit-creation output produces a row (see
    :func:`thread_archive._retrieval._paths.extract_commits`): a session that ran
    ``git log`` saw a hundred shas and authored none of them, so reading a sha is
    deliberately not provenance.

    ``sha`` is stored exactly as git printed it — usually the 7-character
    abbreviation — so lookups match on prefix in either direction. ``repo`` is the
    thread's working directory, the best available guess at which repository the
    commit landed in.
    """

    __tablename__ = "event_commits"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text)
    sha: Mapped[str] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text, default=None)
    repo: Mapped[str | None] = mapped_column(Text, default=None)
    occurred_at: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        Index("idx_event_commits_sha", "sha"),
        Index("idx_event_commits_thread", "thread_id"),
        Index("idx_event_commits_event", "event_id"),
    )


class CodeCursor(Base):
    """The code-index watermark: the highest ``events.id`` already folded into
    :class:`EventPath` / :class:`EventCommit`. A single row (``id = 1``).

    Same contract as :class:`MetricsCursor`, for the same reason — the fold is
    append-only over monotonic ids, so folding only ``id > through_event_id`` is
    exact. ``projection_version`` is what makes the extraction rules revisable: a
    fold that finds a trailing version discards both projections and rebuilds,
    because rows written under older rules cannot be added to by newer ones. A log
    that shrank below the cursor (a reindex rebuilt it) triggers the same rebuild.
    """

    __tablename__ = "code_cursor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    through_event_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    projection_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class ImportState(Base):
    """Per-source import watermark so incremental import is idempotent.

    Each ``(source, source_id)`` pair tracks how far into a session file we have
    imported (line count, file size, last message uuid) so duplicate polls do not
    re-import unchanged content.

    ``last_content_hash`` is what makes the cursor a *proof* rather than an
    observation: the sha256 of the exact file bytes the line cursor was computed
    over. A poll re-hashes the current file's first ``last_file_size`` bytes and
    resumes only if they still match — otherwise the source was rewritten under the
    cursor and the file re-imports from line 0 (see :mod:`.._importers._cursor`).
    NULL on the DB-backed sources, whose cursor is a row count, not a file offset.
    """

    __tablename__ = "import_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("threads.id", ondelete="RESTRICT"), default=None
    )
    last_line_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    last_file_size: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    last_content_hash: Mapped[str | None] = mapped_column(Text, default=None)
    last_message_uuid: Mapped[str | None] = mapped_column(Text, default=None)
    last_import_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=now_default()
    )

    __table_args__ = (
        UniqueConstraint("source", "source_id"),
        Index("idx_import_state_lookup", "source", "source_id"),
        Index("idx_import_state_thread", "thread_id"),
    )


# ── Knowledge layer (data plane) ─────────────────────────────────────────────
# Topics are threads (``thread_type='topic'``). ThreadLink is the topic-graph edge
# set and TopicMessage the message→topic evidence. Both are **projections of the
# topic-graph event log** (``KgEvent`` / ``kg_events.jsonl``): a write
# appends an event and folds it into these
# tables (see :mod:`thread_archive._knowledge.materialize`). A legacy snapshot of the
# tables may still exist as a reindex seed, which the event replay reconciles on top.


class ThreadLink(Base):
    """A directed link between two topics (or topic↔conversation). The edge set
    the in-process topic graph projects over. ``strength`` (0.0–1.0) is confidence;
    ``link_type`` drives the community projection's edge weighting."""

    __tablename__ = "thread_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
    target_thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
    link_type: Mapped[str] = mapped_column(Text, default="related", server_default=text_default("related"))
    strength: Mapped[float] = mapped_column(REAL, default=1.0, server_default=text("1.0"))
    created_by: Mapped[str] = mapped_column(Text, default="auto", server_default=text_default("auto"))
    created_by_thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("threads.id", ondelete="SET NULL"), default=None
    )
    evidence: Mapped[str | None] = mapped_column(Text, default=None)
    observation_ids: Mapped[list[int] | None] = mapped_column("observation_ids", ARRAY(Integer()), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=now_default()
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=now_default()
    )

    __table_args__ = (
        UniqueConstraint("source_thread_id", "target_thread_id", "link_type"),
        Index("idx_thread_links_source", "source_thread_id"),
        Index("idx_thread_links_target", "target_thread_id"),
        Index("idx_thread_links_type", "link_type"),
        Index("idx_thread_links_strength", text("strength DESC")),
        Index("idx_thread_links_created_by_thread", "created_by_thread_id"),
    )


class TopicMessage(Base):
    """Evidence that a topic appears in a conversation. ``topic_id`` is the topic
    thread; ``thread_id`` is the conversation; ``event_id`` is the message."""

    __tablename__ = "topic_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
    quote: Mapped[str] = mapped_column(Text)
    created_by_thread_id: Mapped[str | None] = mapped_column(Text, default=None)
    actor: Mapped[str] = mapped_column("actor", String, nullable=False, server_default=text("'unknown'"))
    archived_at: Mapped[datetime | None] = mapped_column("archived_at", DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=now_default()
    )

    __table_args__ = (
        UniqueConstraint("topic_id", "event_id"),
        Index("idx_topic_messages_topic", "topic_id"),
        Index("idx_topic_messages_thread", "thread_id"),
        Index("idx_topic_messages_event", "event_id"),
        Index("idx_topic_messages_created_by", "created_by_thread_id"),
    )


class ThreadMetrics(Base):
    """Per-(thread, model) token/cost rollup — a derived analytics cache for the
    viewer's stats page.

    Cost and token counts live inside each ``api_request_completed`` event's JSON
    ``payload`` (``input_tokens`` / ``cache_read_tokens`` / ``output_tokens`` /
    ``thinking_tokens`` / ``cost`` / ``model``). Surveying them straight from
    ``events`` means JSON-extracting across
    hundreds of thousands of fat payloads (each carries the full response), which is
    far too slow to do per request on a multi-GB index. This table is the standing
    aggregate: one row per (thread_id, model), accumulated **incrementally** from new
    events only (:func:`thread_archive._store._metrics.refresh_metrics` folds events
    past a global cursor), so the survey is paid once and stays cheap thereafter.

    A pure projection of the event log — rebuildable and disposable, like the FTS
    shadow. ``thread_id`` is a soft reference (no FK, mirroring :class:`EventFts`) so
    the cache never constrains the thread/event lifecycle or a reindex's bulk reload;
    aggregation joins ``threads`` and so ignores any row orphaned by a deleted thread.
    ``cost`` is summed treating a null (a route that reported none, e.g. ``local/*``)
    as zero; ``cost_requests`` counts how many folded requests actually carried a cost,
    so "no cost recorded" stays distinguishable from "$0".
    """

    __tablename__ = "thread_metrics"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # The model that produced these requests, exactly as the event payload records it
    # (e.g. 'anthropic/claude-opus-4-8', 'deepseek/deepseek-v4-pro'); '' for a request
    # whose payload named no model. Placeholder values ('', 'unknown', '<synthetic>')
    # are dropped at aggregation time, not here.
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    requests: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    thinking_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    cost: Mapped[float] = mapped_column(REAL, default=0.0, server_default=text("0"))
    cost_requests: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))

    __table_args__ = (Index("idx_thread_metrics_model", "model"),)


class RequestMetric(Base):
    """One canonical usage row per provider API request.

    Claude Code repeats a response's full usage object across every transcript row
    belonging to that response, and those rows can arrive in different watcher polls,
    so the provider request identity has to survive between incremental folds. Sources
    without a stable request id key on the event id, which is unique per row and so
    collapses nothing.

    Every token and cost figure the stats page shows is summed from here rather than
    straight off ``events``: the duplicate rows are indistinguishable from separate
    requests at the event level, so summing them there reports one response several
    times over.

    This is a disposable projection of the event log, like ``thread_metrics``.
    """

    __tablename__ = "request_metrics"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_key: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # The request's own calendar month, 'YYYY-MM' (UTC), off its ``occurred_at``. Kept
    # here rather than derived at read time because that would mean re-JSON-scanning the
    # event log — the cost this ledger exists to pay once. Null only for a row folded
    # before the column existed, which the projection version then rebuilds.
    month: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    thinking_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    # Null when the route reported no cost at all (e.g. a subscription tool). That is
    # what keeps "no cost recorded" distinguishable from a genuine $0 once summed.
    cost: Mapped[float | None] = mapped_column(REAL, nullable=True, default=None)

    __table_args__ = (
        Index("idx_request_metrics_thread_model", "thread_id", "model"),
        Index("idx_request_metrics_month", "month"),
    )


class ThreadActivity(Base):
    """When each thread was live: the first and last ``occurred_at`` across all its
    events, whatever their type.

    The stats page's time axis. ``threads.inserted_at`` cannot serve it — that is when
    the archive *ingested* a conversation, so every provider export ever imported
    collapses onto its import day and three years of history reads as one spike. The
    event timestamps are the conversation's own clock, and this table is where they
    become cheap to ask for: surveying ``MIN/MAX(occurred_at)`` per thread is a full
    scan of a multi-GB log, far too slow per request.

    Folded over *every* event type, not just ``api_request_completed`` — a source that
    records no token usage at all (a web export) still has messages, and dropping those
    threads would silently erase the archive's whole early history from the timeline.

    A disposable projection of the event log, like the metrics tables beside it, and
    rebuilt by the same cursor.
    """

    __tablename__ = "thread_activity"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    first_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_thread_activity_first", "first_at"),)


class MetricsCursor(Base):
    """The incremental-rollup watermark: the highest ``events.id`` already folded into
    :class:`ThreadMetrics`. A single row (``id = 1``).

    :func:`thread_archive._store._metrics.refresh_metrics` folds only events with
    ``id > through_event_id`` (append-only, monotonic ids → exact incremental sums),
    then advances the cursor. If the log ever shrinks below the cursor (a reindex
    rebuilt it), the refresh resets the whole cache and rebuilds from zero — the
    projection self-heals rather than trusting a stale sum.
    """

    __tablename__ = "metrics_cursor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    through_event_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    # Which definition of the projection produced the standing sums. Sums folded by an
    # older shape cannot be added to by a newer one, so a refresh that finds this
    # trailing ``PROJECTION_VERSION`` discards both projections and rebuilds.
    projection_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )


class KgEvent(Base):
    """Append-only topic-graph event — the event-sourced spine of the topic graph.

    Every topic/link/evidence mutation is recorded here *first* (durable truth:
    ``kg_events.jsonl``) and then folded into the ``thread_links`` / ``topic_messages``
    projections by :mod:`thread_archive._knowledge.materialize`. The log is the source
    of truth for the topic graph; the projection tables are rebuildable from it (replayed in
    ``id`` order on reindex). This is what restores event-sourcing to the knowledge
    layer: an unlink/archive is a tombstone event, never a silent overwrite, so the
    full operation history — when a link was made, edited, removed, by whom — survives.

    ``entity_type`` is 'topic' | 'link' | 'topic_message'; ``entity_id`` is that
    entity's natural key as a string (``str(topic_id)``, ``"src:tgt:link_type"``,
    ``"topic_id:event_id"``) so the log is queryable by entity. ``payload`` (JSON)
    carries the event-type-specific fields the materializer reads.
    """

    __tablename__ = "kg_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text, default=None)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Who performed the mutation, as the writing layer names itself ('gardener',
    # a skill, an agent harness, a backfill script). Archive authors none of these
    # records and cannot infer an identity for one that arrives without a name, so
    # the default states that rather than guessing a writer — matching the
    # ``unknown`` the ``topic_messages.actor`` projection below already carries.
    actor: Mapped[str] = mapped_column(
        Text, default="unknown", server_default=text_default("unknown")
    )
    actor_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    caused_by_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, default=None)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        "recorded_at", DateTime(timezone=True), nullable=False, server_default=now_default()
    )

    __table_args__ = (
        Index("idx_kg_events_type", "event_type"),
        Index("idx_kg_events_entity", "entity_type", "entity_id"),
        Index("idx_kg_events_occurred", "occurred_at"),
        Index("idx_kg_events_actor_thread", "actor_thread_id"),
        Index("idx_kg_events_correlation", "correlation_id", postgresql_where=text("(correlation_id IS NOT NULL)")),
        # Persistent id high-water across DELETE — see the Thread note. Topic-graph
        # writes mint kg-event ids on insert; without this, a reindex that empties the
        # table would let the next write restart ids from 1 and collide with a
        # historical id.
        {"sqlite_autoincrement": True},
    )
