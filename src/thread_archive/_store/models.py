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


class Thread(Base):
    """An imported conversation, or a curated topic.

    ``thread_type`` is one of just two kinds: 'conversation' (a chat session — the
    default, what every importer creates) or 'topic' (a curated knowledge node in the
    topic graph; see :mod:`thread_archive._knowledge`). Modeling a topic *as* a thread is
    deliberate, not leftover polymorphism: it gives topics thread ids, so the graph's
    edges and citations reference a single id space. ``source`` tracks origin
    ('claude-code', 'cursor', 'codex', ...). ``source_metadata`` (JSON) carries branching
    info (branched_from, branch_event_id, quoted_event_id, ...).
    """

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(primary_key=True)
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
        # AUTOINCREMENT: keep a persistent id high-water (sqlite_sequence) that DELETE
        # does NOT reset. reindex clears + reloads with explicit ids; without this a
        # concurrent writer mid-reindex gets a low rowid (max+1 of the partially loaded
        # table) that collides with a not-yet-reloaded historical row. With it, new ids
        # always continue past the high-water and are never reused. See
        # truth.jsonl_log.reindex.
        {"sqlite_autoincrement": True},
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
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
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
        # Persistent id high-water across DELETE — see the Thread note. This is the
        # column that bit us: the live watcher mints event ids on insert, so a reindex
        # running against a live watcher must not be able to recycle a historical id.
        {"sqlite_autoincrement": True},
    )


class EventFts(Base):
    """Full-text search shadow for events.

    Populated by application code as searchable events land; the FTS5 virtual table
    (``event_search``) is built over these rows. ``event_id`` references
    ``events.id`` (a soft reference, no FK by design).
    """

    __tablename__ = "events_fts"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(Text, default=None)
    tool_name: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        Index("idx_events_fts_event_id", "event_id"),
        Index("idx_events_fts_thread_id", "thread_id"),
        Index("idx_events_fts_tool_name", "tool_name"),
    )


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
    thread_id: Mapped[int | None] = mapped_column(
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


# ── Knowledge layer ──────────────────────────────────────────────────────────
# Topics are threads (``thread_type='topic'``). ThreadLink is the topic→topic
# edge set the networkx graph projects over; TopicMessage is message→topic
# evidence. Both are now **projections of the curatorial event log** (``KgEvent`` /
# ``kg_events.jsonl``): a librarian write appends an event and folds it into these
# tables (see :mod:`thread_archive._knowledge.materialize`). A legacy snapshot of the
# tables may still exist as a reindex seed, which the event replay reconciles on top.


class ThreadLink(Base):
    """A directed link between two topics (or topic↔conversation). The edge set
    the in-process topic graph projects over. ``strength`` (0.0–1.0) is confidence;
    ``link_type`` drives the community projection's edge weighting."""

    __tablename__ = "thread_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
    target_thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
    link_type: Mapped[str] = mapped_column(Text, default="related", server_default=text_default("related"))
    strength: Mapped[float] = mapped_column(REAL, default=1.0, server_default=text("1.0"))
    created_by: Mapped[str] = mapped_column(Text, default="auto", server_default=text_default("auto"))
    created_by_thread_id: Mapped[int | None] = mapped_column(
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
    topic_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"))
    quote: Mapped[str] = mapped_column(Text)
    created_by_thread_id: Mapped[int | None] = mapped_column(default=None)
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


class KgEvent(Base):
    """Append-only curatorial event — the event-sourced spine of the topic graph.

    Every topic/link/evidence mutation is recorded here *first* (durable truth:
    ``kg_events.jsonl``) and then folded into the ``thread_links`` / ``topic_messages``
    projections by :mod:`thread_archive._knowledge.materialize`. The log is the source
    of truth for curation; the projection tables are rebuildable from it (replayed in
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
    actor: Mapped[str] = mapped_column(
        Text, default="librarian", server_default=text_default("librarian")
    )
    actor_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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
        # Persistent id high-water across DELETE — see the Thread note. The librarian
        # mints kg-event ids on insert; without this, a reindex that emptied the table
        # let the next curation write restart ids from 1 and collide (the kg_events
        # id=1 dup that aborted reindex).
        {"sqlite_autoincrement": True},
    )
