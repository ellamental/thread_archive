"""Rebuilders and their gates: the integrity scan, ``reindex`` (JSONL → SQLite,
build-and-swap, fail-closed on committed-content loss), and
``rebuild_truth_from_store`` (the one sanctioned store → truth re-emit, behind
its containment pre-flights). Storage-model overview: :mod:`.jsonl_log`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy import insert, select

from .._store import (
    ArchiveSession,
    Event,
    ImportState,
    KgEvent,
    Thread,
    build_engine,
    get_engine,
    get_session,
    init_db,
    use_engine,
)
from .layout import (
    _CROSS_THREAD,
    KG_EVENTS_FILE,
    THREADS_SUBDIR,
    TRUTH_FORMAT_VERSION,
    _classify_parse_errors,
    _coerce,
    _depth_for,
    _fsync_dir,
    _iter_jsonl,
    _json_default,
    _now_iso,
    _row_dict,
    _shard_depth,
    _thread_file,
    log_dir,
    update_manifest,
)
from .locks import _hold_reindex_lock, _truth_write_lock
from .maintenance import _write_snapshot

logger = logging.getLogger(__name__)

# ── integrity scan (the primitive behind `archive verify`) ───────────────────
def scan_truth_counts(
    *, event_id_max: int | None = None, thread_id_max: int | None = None,
    kg_event_id_max: int | None = None, truth_dir: Path | None = None,
) -> dict:
    """Count threads + event lines across the truth directory — the per-thread
    files *and* the curatorial log (``kg_events.jsonl``) — tallying any JSON
    parse errors. The integrity primitive behind ``archive verify``: a clean
    archive has these match the SQLite projection's thread/event/kg-event counts
    (the JSONL ⊇ SQLite invariant) with zero parse errors.

    The truth is append-only, so it legitimately accumulates superseded lines the
    projection collapses: a re-appended line for an id it already holds (a crash
    between a rebalance merge-copy and its unlink), and a same-content twin under
    a fresh id (a re-import after the original's commit was lost — same
    ``dedup_key``). ``events`` counts raw lines; ``events_effective`` counts what
    the projection materializes — distinct on id, then distinct on ``dedup_key``
    (falling back to id when NULL). The index is compared against
    ``events_effective``; the superseded remainder is reported, not drift.

    A thread is counted once per distinct file *stem*, and the collapse runs
    across every file sharing that stem — so a thread present at two shard
    depths (a both-layouts backup mirror, a mid-migration crash) counts as one
    thread and its duplicated lines as superseded, matching what a reindex of
    that directory materializes.

    ``kg_events`` counts the curatorial log's distinct ids (the file is
    append-only, so a crash-merge can legitimately duplicate a line; the
    projection materializes one row per id). Its unparseable lines fold into the
    same ``parse_errors`` tally and torn-tail/interior split as the per-thread
    files — the curation truth deserves the same daily scan the conversation
    truth gets, not a weekly one.

    ``event_id_max`` / ``thread_id_max`` / ``kg_event_id_max`` bound the scan to
    ids at or below a stable watermark, so a verify racing live ingest (truth
    lines land *before* their commit; the index keeps growing while the scan
    reads files) compares the same committed prefix on both sides instead of
    false-alarming.

    ``truth_dir`` scans an explicit directory instead of the live archive's —
    the primitive behind the backup-side check (``archive verify --backup``)."""
    d = truth_dir if truth_dir is not None else log_dir()
    threads_dir = d / THREADS_SUBDIR
    n_events = n_effective = 0
    dup_id_lines = dup_content_lines = 0
    parse_error_locs: list[tuple[str, int]] = []
    files_by_stem: dict[str, list[Path]] = {}
    if threads_dir.exists():
        for path in threads_dir.rglob("*.jsonl"):
            if thread_id_max is not None:
                try:
                    if int(path.stem) > thread_id_max:
                        continue
                except ValueError:  # pragma: no cover — stray file
                    pass
            files_by_stem.setdefault(path.stem, []).append(path)
    for paths in files_by_stem.values():
        seen_ids: set = set()
        seen_keys: set = set()
        for path in paths:
            for rec in _iter_jsonl(path, errors=parse_error_locs):
                if rec.get("type", "event") != "event":
                    continue
                ev_id = rec.get("id")
                if event_id_max is not None and ev_id is not None and ev_id > event_id_max:
                    continue
                n_events += 1
                if ev_id in seen_ids:
                    dup_id_lines += 1
                    continue
                seen_ids.add(ev_id)
                key = rec.get("dedup_key") or ("id", ev_id)
                if key in seen_keys:
                    dup_content_lines += 1
                    continue
                seen_keys.add(key)
                n_effective += 1
    # The curatorial log: distinct kg-event ids at or below the watermark, its
    # parse errors folded into the same tally (and torn/interior split) so a
    # damaged curation line fails the daily verify, not just the weekly deep one.
    kg_ids: set = set()
    for rec in _iter_jsonl(d / KG_EVENTS_FILE, errors=parse_error_locs):
        kg_id = rec.get("id")
        if kg_id is None or (kg_event_id_max is not None and kg_id > kg_event_id_max):
            continue
        kg_ids.add(int(kg_id))
    # Same split reindex reports: a torn tail (the file's final non-empty line —
    # the residue of a crash mid-append, waiting for `archive repair`) vs. interior
    # damage (a fragment later appends isolated, or corruption of a formerly-good
    # line — repair quarantines it and restores any committed event it shadowed).
    torn, interior = _classify_parse_errors(parse_error_locs)
    parse_error_sample = [f"{p}:{lineno}" for p, lineno in parse_error_locs[:10]]
    return {
        "threads": len(files_by_stem),
        "events": n_events,
        "events_effective": n_effective,
        "kg_events": len(kg_ids),
        "duplicate_id_lines": dup_id_lines,
        "duplicate_content_lines": dup_content_lines,
        "parse_errors": len(parse_error_locs),
        "parse_errors_torn_tail": len(torn),
        "parse_errors_interior": len(interior),
        "parse_error_sample": parse_error_sample,
    }


def _insert_or_replace(conn, table, rows: list[dict]) -> None:
    """Bulk ``INSERT OR REPLACE`` a batch whose rows need not share a key-set.

    Core executemany compiles one statement from the first row's keys and binds
    every row against it, so a batch mixing dicts with different keys raises
    ``StatementError``. That mix is real: :func:`_coerce` keeps only the keys a
    record actually carries, so a synthesized minimal thread stub (``{id, name}``)
    lands next to a full thread record, and a record written under a since-changed
    schema lands next to a current one. Group by key-set so each executemany is
    homogeneous — every column absent from a group still gets its model default.
    Each id appears at most once per load, so the OR-REPLACE result is
    order-independent across groups."""
    stmt = insert(table).prefix_with("OR REPLACE")
    groups: dict[frozenset[str], list[dict]] = {}
    for row in rows:
        groups.setdefault(frozenset(row), []).append(row)
    for group in groups.values():
        conn.execute(stmt, group)


def _load_table(
    model: type, path: Path, engine, batch: int = 5000,
    *, errors: list[tuple[str, int]] | None = None,
) -> int:
    """Bulk-load a snapshot file (one row per line) into its table."""
    table = model.__table__  # type: ignore[attr-defined]
    total = 0
    buf: list[dict] = []

    def _flush() -> None:
        nonlocal total
        if not buf:
            return
        with engine.begin() as conn:
            _insert_or_replace(conn, table, buf)
        total += len(buf)
        buf.clear()

    for row in _iter_jsonl(path, errors=errors):
        buf.append(_coerce(model, row))
        if len(buf) >= batch:
            _flush()
    _flush()
    return total


def thread_file_load_order(d: Path) -> list[Path]:
    """Every ``threads/**/*.jsonl`` in the order reindex loads them: path-sorted,
    then any file at its thread's *canonical* (manifest-depth) location moved
    last. Loads are last-wins (OR REPLACE by id; latest thread record), so when a
    thread has files at more than one shard depth — a both-layouts backup mirror,
    a mid-migration crash — the canonical file's records must win: it is the one
    live writers append to, so it is at least as fresh as any stale twin. Plain
    path order would let a stale flat twin load *after* its sharded home
    (``threads/00/…`` sorts before ``threads/12345.jsonl``) and shadow it."""
    threads_dir = d / THREADS_SUBDIR
    if not threads_dir.exists():
        return []
    depth = _shard_depth(d)

    def _canonical(path: Path) -> bool:
        try:
            return path == _thread_file(d, int(path.stem), depth)
        except ValueError:
            return False

    paths = sorted(threads_dir.rglob("*.jsonl"))
    paths.sort(key=_canonical)  # stable: non-canonical first, canonical last
    return paths


def load_thread_files(
    d: Path, engine, batch: int = 5000,
    *, errors: list[tuple[str, int]] | None = None,
) -> tuple[int, int]:
    """Load every ``threads/**/<id>.jsonl`` into the threads + events tables.

    Per file: the last ``type:thread`` record is the metadata (latest wins), every
    ``type:event`` record is an event. Files load in :func:`thread_file_load_order`
    (canonical-depth file last), so a thread with a stale twin at another shard
    depth materializes the canonical file's records. A file with events but no
    thread record (a crash between an import commit and its checkpoint) gets a
    synthesized minimal thread so its events aren't dropped. FK enforcement is off
    on the loader, so the interleaved thread/event inserts need no ordering."""
    thread_buf: list[dict] = []
    event_buf: list[dict] = []
    nt = ne = 0

    def _flush() -> None:
        nonlocal nt, ne
        if thread_buf:
            with engine.begin() as conn:
                _insert_or_replace(conn, Thread.__table__, thread_buf)
            nt += len(thread_buf)
            thread_buf.clear()
        if event_buf:
            with engine.begin() as conn:
                _insert_or_replace(conn, Event.__table__, event_buf)
            ne += len(event_buf)
            event_buf.clear()

    have_meta: set[int] = set()  # tids with a real thread record already loaded
    for path in thread_file_load_order(d):
        last_thread: dict | None = None
        events: list[dict] = []
        for rec in _iter_jsonl(path, errors=errors):
            kind = rec.pop("type", "event")
            if kind == "thread":
                last_thread = rec
            else:
                events.append(rec)
        if last_thread is None:
            try:
                tid = int(path.stem)
            except ValueError:  # pragma: no cover
                continue
            if tid in have_meta:
                # A twin of this thread already supplied its real record; a
                # synthesized stub loading after it would clobber real metadata.
                last_thread = None
            else:
                last_thread = {"id": tid, "name": f"thread:{tid}"}
                logger.warning("reindex: %s had no thread record — synthesized minimal", path.name)
        else:
            try:
                have_meta.add(int(last_thread.get("id")))
            except (TypeError, ValueError):  # pragma: no cover — malformed record
                pass
        if last_thread is not None:
            thread_buf.append(_coerce(Thread, last_thread))
        for ev in events:
            event_buf.append(_coerce(Event, ev))
        if len(thread_buf) >= batch or len(event_buf) >= batch:
            _flush()
    _flush()
    return nt, ne


def _carry_import_state(index_path: Path, engine) -> int:
    """Carry the source-import watermarks from the previous index into the rebuild.

    ``import_state`` is operational state, not truth — the JSONL doesn't contain it,
    so a plain rebuild would wipe every source cursor. The next poll would then
    re-adopt each event-bearing source at its current EOF (``adopt_if_unwatermarked``),
    permanently skipping any source lines appended since its last import — reindexing
    with live sessions writing would silently lose their tails. The previous live
    index is the freshest copy of the cursors, so it overlays the (possibly stale)
    ``import_state.jsonl`` snapshot seed loaded before this. Rows pointing at threads
    the truth no longer holds are pruned. Returns rows carried."""
    if not index_path.exists():
        return 0
    build_cols = {c.key for c in ImportState.__table__.columns}
    try:
        src = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # pragma: no cover — unreadable old index
        return 0
    try:
        try:
            cur = src.execute("SELECT * FROM import_state")
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):  # pragma: no cover — unreadable old index
                logger.warning("reindex: could not read import_state from old index: %s", e)
            return 0
        src_cols = [d[0] for d in cur.description]
        keep = [i for i, c in enumerate(src_cols) if c in build_cols]
        cols = [src_cols[i] for i in keep]
        rows = [tuple(r[i] for i in keep) for r in cur.fetchall()]
    finally:
        src.close()
    if rows:
        stmt = (
            f"INSERT OR REPLACE INTO import_state ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})"
        )
        with engine.begin() as conn:
            conn.exec_driver_sql(stmt, rows)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM import_state WHERE thread_id IS NOT NULL "
            "AND thread_id NOT IN (SELECT id FROM threads)"
        )
    return len(rows)


def _replay_kg_events(
    d: Path, engine, *, errors: list[tuple[str, int]] | None = None,
) -> int:
    """Fold the curatorial event log (``kg_events.jsonl``) onto the knowledge projection.

    Loads the log into the ``kg_events`` table and replays each event in ``id`` order
    through the materializer, mutating ``thread_links`` / ``topic_messages`` on top of
    whatever legacy snapshot seed was already loaded. The materializer is upsert +
    tombstone, so a delta that re-touches a seeded row (or deletes one) reconciles
    cleanly and the replay is idempotent and order-stable. A no-op when the log is
    absent — a pre-librarian (or purely lexical) archive simply has no curation to fold.
    Runs through an ORM ``Session`` so the fold can use the materializer, but it never
    *stages* truth (only :func:`append_kg_event` does), so the before-commit drain is a
    no-op here and the rebuild can't re-write the log it is reading."""
    from .._knowledge.materialize import apply_event

    rows = list(_iter_jsonl(d / KG_EVENTS_FILE, errors=errors))
    for r in rows:
        r.pop("type", None)
    rows.sort(key=lambda r: r.get("id") or 0)
    if not rows:
        return 0
    coerced = [_coerce(KgEvent, r) for r in rows]
    with engine.begin() as conn:
        conn.execute(insert(KgEvent.__table__).prefix_with("OR REPLACE"), coerced)
    with ArchiveSession(engine) as s:
        for r in coerced:
            apply_event(s, KgEvent(**r))
        s.commit()
    return len(rows)


def _committed_regression(index_path: Path, tmp_path: Path) -> dict | None:
    """Committed records the rebuild would lose, diffed against the current index.

    The gate behind fail-closed publication: every event and kg-event id the
    live index holds must survive into the build — an event may instead survive
    as a same-content twin (same ``(thread_id, dedup_key)`` under another id;
    dedup collapse legitimately re-keys those). Threads are checked by id too:
    an event-less thread lost to a name-conflict ``OR REPLACE`` (or a deleted
    truth file) has no event row to trip the event diff. Anything else missing
    means the truth lost committed content (a damaged line, a deleted file) and
    the swap would make the loss permanent-by-default. Returns ``None`` when
    there is no readable previous index to diff against (a fresh restore has no
    baseline)."""
    if not index_path.exists():
        return None
    conn = sqlite3.connect(tmp_path)
    try:
        try:
            conn.execute("ATTACH DATABASE ? AS old", (str(index_path),))
            lost_threads = conn.execute(
                "SELECT count(*) FROM old.threads o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.threads n WHERE n.id = o.id)"
            ).fetchone()[0]
            thread_sample = [r[0] for r in conn.execute(
                "SELECT o.id FROM old.threads o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.threads n WHERE n.id = o.id) "
                "ORDER BY o.id LIMIT 10"
            ).fetchall()] if lost_threads else []
            lost_events = conn.execute(
                "SELECT count(*) FROM old.events o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.events n WHERE n.id = o.id) "
                "AND (o.dedup_key IS NULL OR NOT EXISTS("
                "  SELECT 1 FROM main.events n "
                "  WHERE n.thread_id = o.thread_id AND n.dedup_key = o.dedup_key))"
            ).fetchone()[0]
            sample = [r[0] for r in conn.execute(
                "SELECT o.id FROM old.events o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.events n WHERE n.id = o.id) "
                "AND (o.dedup_key IS NULL OR NOT EXISTS("
                "  SELECT 1 FROM main.events n "
                "  WHERE n.thread_id = o.thread_id AND n.dedup_key = o.dedup_key)) "
                "ORDER BY o.id LIMIT 10"
            ).fetchall()] if lost_events else []
            lost_kg = conn.execute(
                "SELECT count(*) FROM old.kg_events o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.kg_events n WHERE n.id = o.id)"
            ).fetchone()[0]
        except sqlite3.Error as e:
            # An unreadable / pre-schema old index is no baseline — the rebuild
            # IS the recovery. Never let the gate itself block it.
            logger.warning("reindex: cannot diff against the previous index (%s)", e)
            return None
    finally:
        conn.close()
    return {
        "events": int(lost_events), "event_sample": sample,
        "kg_events": int(lost_kg),
        "threads": int(lost_threads), "thread_sample": thread_sample,
    }


def _parsed_equal(a: object, b: object) -> bool:
    """True when two stored payload texts encode the same object. Raw-text
    inequality alone must not count — the two stores may serialize one object
    differently across code versions — so candidates are confirmed by parsed
    comparison. An unparseable side is a real disagreement (that copy is
    damaged)."""
    if a == b:
        return True
    try:
        pa = json.loads(a) if isinstance(a, str) else a
        pb = json.loads(b) if isinstance(b, str) else b
    except ValueError:
        return False
    return pa == pb


def _content_divergence(index_path: Path, tmp_path: Path) -> dict | None:
    """Same-id events whose content disagrees between the current index and the
    rebuild — publication *visibility*, deliberately not a gate.

    The truth is authoritative: on publish the rebuild's content wins, and the
    projection disagreeing must not block the swap. But every sanctioned payload
    change lands in both stores inside one commit (an amendment's superseding
    truth line drains before its index COMMIT), so a same-id disagreement
    between them means one copy is damaged —
    and for an event with no hash-tailed ``dedup_key`` the live index row can be
    the last good copy of a truth line rotted in place. Cross-store parity
    (``verify --hashes``) can only see the disagreement while both copies still
    exist; the moment the swap lands they agree and the rot is laundered.
    Publication is therefore the last observable moment of the overwrite: count
    it and name the ids, so an operator (or the agent that just ran ``archive
    reindex`` as a routine fix) can adjudicate against a backup generation
    before the next backup run propagates the new content. Returns ``None``
    with no readable previous index, else ``{"events": n, "sample": [...]}``."""
    if not index_path.exists():
        return None
    conn = sqlite3.connect(tmp_path)
    try:
        try:
            conn.execute("ATTACH DATABASE ? AS old", (str(index_path),))
            # Raw-text inequality is the cheap SQL prefilter; each candidate is
            # confirmed in Python (see _parsed_equal). Streamed, not materialized.
            cur = conn.execute(
                "SELECT o.id, o.event_type, o.payload, n.event_type, n.payload "
                "FROM old.events o JOIN main.events n ON n.id = o.id "
                "WHERE o.payload IS NOT n.payload OR o.event_type IS NOT n.event_type"
            )
            diverged = 0
            sample: list[int] = []
            for ev_id, o_type, o_payload, n_type, n_payload in cur:
                if o_type == n_type and _parsed_equal(o_payload, n_payload):
                    continue
                diverged += 1
                if len(sample) < 10:
                    sample.append(int(ev_id))
        except sqlite3.Error as e:
            # No readable old index — nothing to diverge from; the rebuild IS
            # the recovery.
            logger.warning("reindex: cannot content-diff against the previous index (%s)", e)
            return None
    finally:
        conn.close()
    return {"events": diverged, "sample": sample}


def _build_fk_violations(tmp_path: Path) -> list[tuple]:
    """``PRAGMA foreign_key_check`` over the whole build — the relational gate
    behind fail-closed publication. The loader runs FK-OFF with blanket
    ``INSERT OR REPLACE``, so a load-order accident can delete a parent row
    while its children survive (``threads.name`` is UNIQUE: two thread records
    sharing a name make OR REPLACE silently drop one thread and orphan its
    events). ``quick_check`` is page-level and cannot see that. Every declared
    FK targets ``threads`` — deliberately-soft references (citations to
    events) carry no FK, so this gate can never refuse a rebuild over the
    dangling citations ``verify --deep`` tolerates by design. Returns up to 20
    ``(table, rowid, parent, fkid)`` rows."""
    conn = sqlite3.connect(tmp_path)
    try:
        return conn.execute("PRAGMA foreign_key_check").fetchmany(20)
    finally:
        conn.close()


def _files_for_thread(d: Path, thread_id: int) -> list[Path]:
    """Every truth file for ``thread_id`` — its canonical-depth file plus any
    stale twin at another shard depth."""
    threads_dir = d / THREADS_SUBDIR
    if not threads_dir.exists():
        return []
    return list(threads_dir.rglob(f"{int(thread_id)}.jsonl"))


def _reconcile_collapsed_citations(d: Path, engine) -> dict:
    """Post-load reconciliation of citation → event references in the build.

    Dedup collapse (OR REPLACE + the ``(thread_id, dedup_key)`` unique index)
    keeps one row per content identity; when the *discarded* twin's id was cited
    (``topic_messages.event_id``), the citation would dangle even though the
    content it cites survived under the other id. The truth still holds the
    discarded id's line, so its ``dedup_key`` recovers the surviving row: the
    citation is repointed to it — or dropped when the topic already cites the
    survivor (same content, same topic, one citation). A citation whose event
    has no surviving twin is left dangling for ``verify --deep`` to report —
    but only while its *thread* still exists. When the cited thread is gone
    from the build too (a quarantined contamination, a deliberately removed
    thread), nothing in the store anchors the row: there is no twin to repoint
    to and no parent for its declared ``thread_id`` FK, so the relational gate
    would refuse the rebuild over it. Such a citation is dropped, archived or
    not — the gate stays reserved for genuine loader accidents.

    Citations whose recorded ``thread_id`` disagrees with the cited event's
    actual thread are aligned to the event — the event row is authoritative and
    the column is derived (a wrong value came from an unvalidated write or a
    stale snapshot seed).

    Both repairs are deterministic functions of the truth directory, so
    re-running them on every reindex lands on the same projection; the truth
    log itself is never rewritten here. Returns the (nonzero) counts."""
    with engine.begin() as conn:
        dangling = conn.exec_driver_sql(
            "SELECT m.id, m.topic_id, m.event_id, m.thread_id FROM topic_messages m "
            "WHERE m.archived_at IS NULL "
            "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = m.event_id)"
        ).fetchall()
    repointed = dropped = 0
    if dangling:
        # dedup_key of each discarded id, recovered from its thread's truth
        # file(s). dedup_key is thread-scoped (same content in two threads
        # shares a key), so the survivor lookup stays scoped to the thread.
        wanted_by_thread: dict[int, set[int]] = {}
        for _, _, ev_id, tid in dangling:
            wanted_by_thread.setdefault(int(tid), set()).add(int(ev_id))
        keys: dict[int, str] = {}
        for tid, wanted in wanted_by_thread.items():
            for path in _files_for_thread(d, tid):
                for rec in _iter_jsonl(path):
                    if (
                        rec.get("type", "event") == "event"
                        and rec.get("id") in wanted and rec.get("dedup_key")
                    ):
                        keys[int(rec["id"])] = rec["dedup_key"]
        with engine.begin() as conn:
            for row_id, topic_id, ev_id, tid in dangling:
                key = keys.get(int(ev_id))
                if not key:
                    continue
                survivor = conn.exec_driver_sql(
                    "SELECT id FROM events WHERE thread_id = ? AND dedup_key = ?",
                    (int(tid), key),
                ).fetchone()
                if survivor is None:
                    continue
                already = conn.exec_driver_sql(
                    "SELECT 1 FROM topic_messages WHERE topic_id = ? AND event_id = ?",
                    (int(topic_id), int(survivor[0])),
                ).fetchone()
                if already is not None:
                    conn.exec_driver_sql(
                        "DELETE FROM topic_messages WHERE id = ?", (int(row_id),)
                    )
                    dropped += 1
                else:
                    conn.exec_driver_sql(
                        "UPDATE topic_messages SET event_id = ? WHERE id = ?",
                        (int(survivor[0]), int(row_id)),
                    )
                    repointed += 1
    with engine.begin() as conn:
        unanchored = conn.exec_driver_sql(
            "DELETE FROM topic_messages "
            "WHERE thread_id IS NOT NULL "
            "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = topic_messages.event_id) "
            "AND NOT EXISTS(SELECT 1 FROM threads t WHERE t.id = topic_messages.thread_id)"
        ).rowcount
    with engine.begin() as conn:
        aligned = conn.exec_driver_sql(
            "UPDATE topic_messages SET thread_id = "
            "(SELECT e.thread_id FROM events e WHERE e.id = topic_messages.event_id) "
            "WHERE EXISTS(SELECT 1 FROM events e WHERE e.id = topic_messages.event_id "
            "AND e.thread_id != topic_messages.thread_id)"
        ).rowcount
    out: dict = {}
    if repointed:
        out["citations_repointed"] = repointed
    if dropped:
        out["citations_dropped"] = dropped
    if unanchored:
        out["citations_dropped_unanchored"] = unanchored
    if aligned:
        out["citations_thread_aligned"] = aligned
    if out:
        logger.warning("reindex: citation reconciliation %s", out)
    return out


def _fold_wal(path: Path) -> None:
    """Fold any leftover ``-wal`` into the main file and drop the sidecars, so a
    rename moves one complete, self-contained database."""
    if Path(f"{path}-wal").exists():
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _unlink_build(tmp_path: Path) -> None:
    for p in (tmp_path, Path(f"{tmp_path}-wal"), Path(f"{tmp_path}-shm")):
        p.unlink(missing_ok=True)


def reindex(*, vectors: bool = False, salvage: bool = False) -> dict:
    """Rebuild the SQLite store from the JSONL truth directory — build-and-swap.

    The recovery primitive: build a complete new index (schema, per-thread files,
    cross-thread snapshots, kg-event replay, FTS, optionally vectors) in
    ``index.db.rebuild`` next to the live file, then ``os.replace`` it over
    ``index.db``. Atomic against a crash: a reindex killed at any point leaves the
    old index fully intact and the build file is discarded; only the rename — atomic
    on the same filesystem — publishes the new one. Ingest is quiesced for the
    duration via the reindex flock (the watcher skips passes while it's held), so no
    event lands in the truth mid-build and silently misses the new index. In-process
    readers reconnect on the next ``get_engine()`` call (the live engine's pools are
    disposed before the swap); *cross-process* readers keep serving the old inode
    until they reconnect or restart — an accepted stale-read window (long-lived
    readers converge via :func:`thread_archive._api._reconnect_if_swapped`).

    Bulk loading targets the build file through a **FK-OFF Core** loader so
    dependency-agnostic inserts need no ordering and the conversation truth-log
    listeners never fire; the kg-event replay runs through a Session on that same
    engine but stages nothing, so it likewise can't re-write the truth it reads.
    Loads are **INSERT OR REPLACE** (last-wins). The JSONL is authoritative and
    replayed as-is — including any dangling reference; integrity was the writer's
    job.

    **Publication fails closed on committed-content loss.** Unparseable truth
    lines are skipped and counted (``parse_errors``, split into
    ``parse_errors_torn_tail`` — the file's final line, a torn last append — and
    ``parse_errors_interior``): crash artifacts are expected residue (an
    isolated torn fragment stays an unparseable interior line forever) and must
    never block the recovery primitive. What must never happen instead is a
    swap that silently *loses committed records*, so before publishing the
    build is diffed against the current index (:func:`_committed_regression`):
    any event, kg-event, or thread the old index holds that the rebuild lacks —
    by id, and for events with no same-content twin surviving under another id —
    aborts the swap (the old index stays live) and the error names the loss.
    The build must also pass ``PRAGMA foreign_key_check``
    (:func:`_build_fk_violations`) — the FK-OFF OR REPLACE load can orphan
    children when conflicting parent records collide, and a structurally
    inconsistent build must not be published. Deliberately-soft references
    (citations to events) carry no FK, so the gate never blocks the tolerated
    dangling-citation case; ``salvage=True`` overrides it like the loss gate. A
    crash fragment was never committed (truth is fsynced before its COMMIT), so
    it can't trip the gate; a damaged committed line always does. ``salvage=True``
    is the deliberate override: publish the lossy rebuild anyway. With no
    readable previous index (the ``rm index.db`` recovery flow) there is no
    baseline and the gate is skipped — parse errors are still reported. The
    rebuilt file must also pass ``PRAGMA quick_check`` before the swap (never
    overridable) — a build corrupted at the page level must not replace a
    healthy index.

    **Content divergence is reported, never blocked.** A same-id event whose
    content disagrees between the old index and the build is counted into the
    result (``content_overwrites`` + sample ids) and logged at publication
    (:func:`_content_divergence`): the truth is authoritative and its content
    wins, but nothing mutates a payload after commit, so a disagreement means
    one copy is damaged — and once the swap lands the two stores agree and
    cross-store parity can no longer see it. The report is the last observable
    moment of the overwrite; adjudicate an unexpected one against a backup
    generation before the next backup run propagates the published content."""
    d = log_dir()

    engine = get_engine()
    db_path = engine.url.database
    if not db_path or db_path == ":memory:":
        raise RuntimeError("reindex needs a file-backed index (build-and-swap)")
    index_path = Path(db_path)

    # Pre-flight: the build needs room for a second copy of the index.
    if index_path.exists():
        free = shutil.disk_usage(index_path.parent).free
        need = int(index_path.stat().st_size * 1.2)
        if free < need:
            raise RuntimeError(
                f"reindex: {free / 1e9:.1f} GB free < {need / 1e9:.1f} GB needed "
                "for the temp build — free disk space first"
            )

    tmp_path = index_path.with_name(index_path.name + ".rebuild")
    counts: dict = {}
    parse_errors: list[tuple[str, int]] = []

    with _hold_reindex_lock():
        # Entering the truth-write mutex resolves any crashed drain's leftover
        # intent (a partial batch) before the rebuild reads the files.
        with _truth_write_lock():
            pass
        _unlink_build(tmp_path)  # a dead prior build is stale — start clean
        loader = build_engine(f"sqlite:///{tmp_path}", enforce_fk=False)
        try:
            init_db(loader)
            counts["threads"], counts["events"] = load_thread_files(d, loader, errors=parse_errors)
            # The loader counts rows loaded; OR REPLACE + the (thread_id, dedup_key)
            # unique index collapse superseded lines (re-appended ids, same-content
            # twins from a lost-commit re-import, a thread's stale twin at another
            # shard depth), so report what actually survived.
            with loader.begin() as conn:
                actual_events = conn.exec_driver_sql("SELECT count(*) FROM events").scalar() or 0
                actual_threads = conn.exec_driver_sql("SELECT count(*) FROM threads").scalar() or 0
            if actual_events != counts["events"]:
                counts["events_collapsed"] = counts["events"] - actual_events
                counts["events"] = int(actual_events)
            if actual_threads != counts["threads"]:
                counts["threads"] = int(actual_threads)
            for name, model in _CROSS_THREAD.items():
                counts[name] = _load_table(model, d / f"{name}.jsonl", loader, errors=parse_errors)
            # Source-import watermarks: seed from the checkpoint snapshot, then
            # overlay the previous live index's fresher rows (see _carry_import_state).
            _load_table(ImportState, d / "import_state.jsonl", loader, errors=parse_errors)
            counts["import_state"] = _carry_import_state(index_path, loader)
            counts["kg_events"] = _replay_kg_events(d, loader, errors=parse_errors)

            # Fail closed on committed-content loss (see docstring): crash
            # fragments never block recovery, but a rebuild missing records the
            # current index holds must not be published over it.
            torn_tails, interior = _classify_parse_errors(parse_errors)
            counts["parse_errors_torn_tail"] = len(torn_tails)
            counts["parse_errors_interior"] = len(interior)
            if not salvage:
                lost = _committed_regression(index_path, tmp_path)
                if lost is not None and (lost["events"] or lost["kg_events"] or lost["threads"]):
                    err_sample = ", ".join(f"{p}:{ln}" for p, ln in interior[:5])
                    raise RuntimeError(
                        f"reindex: the rebuild would lose {lost['events']} committed "
                        f"event(s), {lost['kg_events']} curation event(s) and "
                        f"{lost['threads']} thread(s) the current index holds "
                        f"(event sample: {lost['event_sample']}; thread sample: "
                        f"{lost['thread_sample']}) "
                        "— refusing to publish; the old index was left in place. "
                        + (f"Likely cause: {len(interior)} damaged truth line(s) "
                           f"({err_sample}). " if interior else "")
                        + "Repair the truth (or restore it from backup), or rerun "
                        "with --salvage to publish the lossy rebuild anyway."
                    )
            # Report-only, salvage or not: same-id content the publish will
            # overwrite in the index. The truth wins by design — this is the
            # last observable moment of the overwrite, not a gate (see
            # _content_divergence).
            diverged = _content_divergence(index_path, tmp_path)
            if diverged and diverged["events"]:
                counts["content_overwrites"] = diverged["events"]
                counts["content_overwrite_sample"] = diverged["sample"]
                logger.warning(
                    "reindex: publishing content for %d event id(s) that disagrees "
                    "with the live index (sample: %s) — the truth wins by design; "
                    "if this is unexpected, adjudicate against a backup generation "
                    "before the next backup run propagates it",
                    diverged["events"], diverged["sample"],
                )
            counts.update(_reconcile_collapsed_citations(d, loader))

            # FTS + vectors resolve their engine via get_engine(); point them at
            # the build for the block.
            with use_engine(loader):
                from .._retrieval.fts import rebuild_fts

                counts["fts"] = rebuild_fts()
                # Vectors always survive the rebuild: restore the durable sidecar
                # cache (space-key-guarded; the hours-long embed runs once, ever)
                # into the build regardless of the ``vectors`` flag — a plain
                # reindex must not silently swap away the semantic index.
                # ``vectors=True`` additionally embeds whatever the cache lacks and
                # refreshes the sidecar. Degrades to 0 without a sidecar or the
                # [embeddings] extra — the store stays lexical-only.
                from .._retrieval import vectors as _vec

                counts["vectors_restored"] = _vec.load_vectors_sidecar(d)
                # The sidecar may carry vectors for event ids the rebuild
                # collapsed away (superseded same-content twins) — prune them
                # so the vector arm never scores rows that can't hydrate.
                with loader.begin() as conn:
                    has_vec = conn.exec_driver_sql(
                        "SELECT 1 FROM sqlite_master WHERE name='event_vectors'"
                    ).scalar()
                    pruned = conn.exec_driver_sql(
                        "DELETE FROM event_vectors "
                        "WHERE event_id NOT IN (SELECT id FROM events)"
                    ).rowcount if has_vec else 0
                if pruned:
                    counts["vectors_pruned"] = pruned
                if vectors:
                    counts["vectors_embedded"] = _vec.index_events_local(rebuild=False)
                    counts["vectors_cached"] = _vec.save_vectors_sidecar(d)
        except BaseException:
            loader.dispose()
            _unlink_build(tmp_path)  # the old index was never touched
            raise
        loader.dispose()

        # Relational gate on the FINISHED build (after the reconciliation pass
        # has run its deterministic repairs): the FK-OFF OR REPLACE load can
        # orphan children when conflicting parent records collide, and a
        # structurally inconsistent build must not be published. Salvage
        # overrides, like the loss gate.
        if not salvage:
            fk_violations = _build_fk_violations(tmp_path)
            if fk_violations:
                _unlink_build(tmp_path)
                raise RuntimeError(
                    "reindex: the rebuild is relationally inconsistent — "
                    f"foreign_key_check reported {len(fk_violations)} violation(s) "
                    f"(sample: {fk_violations[:5]}) — refusing to publish; the old "
                    "index was left in place. Likely cause: conflicting thread "
                    "records in the truth (OR REPLACE dropped a parent row). "
                    "Repair the truth, or rerun with --salvage to publish anyway."
                )

        # Page-level gate: a build file corrupted on disk (a bad write during the
        # hours-long rebuild) must not replace a healthy index. Before the WAL
        # fold, and read-write: a WAL-mode database refuses a read-only open
        # once its sidecars are gone.
        qconn = sqlite3.connect(tmp_path)
        try:
            qc = [r[0] for r in qconn.execute("PRAGMA quick_check(10)").fetchall()]
        finally:
            qconn.close()
        if qc != ["ok"]:
            _unlink_build(tmp_path)
            raise RuntimeError(
                f"reindex: rebuilt index failed quick_check ({'; '.join(map(str, qc))}) "
                "— the old index was left in place"
            )
        _fold_wal(tmp_path)

        # Publish: dispose the live engine's pools first (its connections point at
        # the file being replaced), atomically rename the build over index.db, then
        # drop the old sidecars — a stale -wal must never be replayed into the new
        # database. The next get_engine() connection opens the new file.
        engine.dispose()
        os.replace(tmp_path, index_path)
        for suffix in ("-wal", "-shm"):
            Path(f"{index_path}{suffix}").unlink(missing_ok=True)

    counts["parse_errors"] = len(parse_errors)

    # The topic graph caches a projection per engine; drop it so the next read
    # rebuilds over the freshly-loaded thread_links.
    from .._knowledge import reset_cache as _reset_kg

    _reset_kg()

    logger.info("jsonl_log reindex: %s", counts)
    return counts



# ── truth emit (the single per-thread file writer; no-drift seam) ────────────
def emit_thread_file(d: Path, thread_id: int, depth: int, thread_record, event_records) -> int:
    """Atomically (re)write one ``threads/<id>.jsonl``: an optional ``type:thread``
    record then the ``type:event`` records, in order. The one place the on-disk
    per-thread format is produced (used by the store re-emit), so the layout can't
    drift. Returns the event count written."""
    path = _thread_file(d, thread_id, depth)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    n_ev = 0
    with open(tmp, "w", encoding="utf-8") as fh:
        if thread_record is not None:
            fh.write(json.dumps({"type": "thread", **thread_record}, default=_json_default, ensure_ascii=False))
            fh.write("\n")
        for ev in event_records:
            fh.write(json.dumps({"type": "event", **ev}, default=_json_default, ensure_ascii=False))
            fh.write("\n")
            n_ev += 1
        fh.flush()
        os.fsync(fh.fileno())  # durable before the rename makes it visible
    os.replace(tmp, path)
    _fsync_dir(path.parent)  # the rename itself must survive power loss
    return n_ev


def _hash_key_check(payload: object, dedup_key: str, *, d: "Path | None" = None) -> bool | None:
    """True = the payload re-hashes to the content hash embedded in its own
    ``dedup_key`` (the last ``:``-segment; see
    ``thread_archive._thread_import.event_builder.compute_dedup_key``); False = mismatch;
    None = the key carries no hash tail (nothing to validate against).

    A payload whose binary content was extracted into the blob store no longer
    hashes as stored — its key was computed over the inline form at parse time.
    On mismatch, blob refs are reconstituted (inline base64 restored from the
    blob files) and the hash retried, so the gate keeps validating extracted
    payloads at full strength: a missing or corrupted blob file fails the
    reconstruction and the check — lost image bytes are lost content. The plain
    hash is tried first, so the reconstitution cost is paid only by the rare
    blob-bearing events. ``d`` names the truth directory whose blob store backs
    the reconstruction — pass it when checking a mirror, so a blob missing or
    rotted *in the mirror* fails the mirror's check instead of being papered
    over by the live store's copy; default is the live truth dir."""
    import re as _re

    from thread_archive._thread_import.event_builder import compute_content_hash

    from .layout import is_redacted_payload

    if is_redacted_payload(payload):
        # A redaction marker: the key's hash names content this line deliberately
        # no longer carries (the encrypted original lives in redactions.jsonl).
        return None
    if not _re.match(r"^[0-9a-f]{16}$", dedup_key.rsplit(":", 1)[-1]):
        return None
    if not isinstance(payload, dict):
        return False
    want = dedup_key.rsplit(":", 1)[-1]
    if compute_content_hash(payload) == want:
        return True
    from .blobs import has_blob_refs, reconstitute_blobs

    if has_blob_refs(payload):
        restored, missing = reconstitute_blobs(payload, d=d)
        if not missing and compute_content_hash(restored) == want:
            return True
    return False


def _store_rows_failing_key_hash() -> tuple[int, list[str]]:
    """Store event rows whose payload no longer re-hashes to the content hash
    embedded in their own ``dedup_key`` — the content half of the pre-flight
    behind :func:`rebuild_truth_from_store`. The containment check proves the
    store holds every truth *unit*; this proves the payloads behind those units
    are self-consistent, so a corrupted index row (rot, a bad in-place write)
    that kept its id and key can't be promoted over the good truth line by a
    re-emit. Returns ``(failing, sample)``."""
    failing = 0
    sample: list[str] = []
    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3 — stream, don't materialize
        for ev_id, tid, key, payload_text in conn.execute(
            "SELECT id, thread_id, dedup_key, payload FROM events "
            "WHERE dedup_key IS NOT NULL"
        ):
            try:
                payload = (
                    json.loads(payload_text)
                    if isinstance(payload_text, str) else payload_text
                )
            except ValueError:
                payload = None
            if _hash_key_check(payload, key) is False:
                failing += 1
                if len(sample) < 10:
                    sample.append(f"thread {tid}: event {ev_id}")
    return failing, sample


def _truth_units_missing_from_store(d: Path) -> tuple[int, list[str], dict[str, int]]:
    """Content units present in the truth but absent from the store — the
    pre-flight behind :func:`rebuild_truth_from_store`. A *unit* is an event's
    content identity: its ``dedup_key`` (thread-scoped), falling back to the
    event id when the key is NULL — the same collapse ``scan_truth_counts`` and
    the reindex loader apply. Count parity can hide compensating errors (an
    index-only event masking a missing one, swapped payloads behind equal
    totals); containment can't: every effective truth unit must exist in the
    store, or a re-emit would destroy content the index never had. Walked one
    thread at a time so memory stays bounded.

    The same walk collects ``dropped_keys``: fields on truth records (event,
    thread, kg_event) that the running code's models don't map. ``_coerce``
    drops them on reload — harmless for the projection — but a re-emit rewrites
    the truth *without* them, which turns that tolerance into permanent loss;
    the caller refuses on any. Returns ``(missing, sample, dropped_keys)``
    where ``dropped_keys`` maps ``"<kind>.<field>"`` to its occurrence count."""
    threads_dir = d / THREADS_SUBDIR
    files_by_stem: dict[str, list[Path]] = {}
    if threads_dir.exists():
        for path in threads_dir.rglob("*.jsonl"):
            files_by_stem.setdefault(path.stem, []).append(path)
    valid_keys = {
        "event": {c.key for c in Event.__table__.columns},
        "thread": {c.key for c in Thread.__table__.columns},
        "kg_event": {c.key for c in KgEvent.__table__.columns},
    }
    dropped: dict[str, int] = {}

    def _note_unknown(kind: str, rec: dict) -> None:
        for k in rec.keys() - valid_keys[kind] - {"type"}:
            dropped[f"{kind}.{k}"] = dropped.get(f"{kind}.{k}", 0) + 1

    missing = 0
    sample: list[str] = []
    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3 — stream, don't materialize
        for stem, paths in files_by_stem.items():
            try:
                tid = int(stem)
            except ValueError:  # pragma: no cover — stray file
                continue
            units: set = set()
            for path in paths:
                for rec in _iter_jsonl(path):
                    kind = rec.get("type", "event")
                    if kind in valid_keys:
                        _note_unknown(kind, rec)
                    if kind != "event" or rec.get("id") is None:
                        continue
                    units.add(rec.get("dedup_key") or ("id", int(rec["id"])))
            if not units:
                continue
            for ev_id, key in conn.execute(
                "SELECT id, dedup_key FROM events WHERE thread_id = ?", (tid,)
            ):
                units.discard(key or ("id", int(ev_id)))
            missing += len(units)
            for unit in sorted(map(str, units))[: max(0, 10 - len(sample))]:
                sample.append(f"thread {tid}: {unit}")
    if (d / KG_EVENTS_FILE).exists():
        for rec in _iter_jsonl(d / KG_EVENTS_FILE):
            _note_unknown("kg_event", rec)
    return missing, sample, dropped


def rebuild_truth_from_store(*, force: bool = False) -> dict:
    """Re-emit the entire per-thread truth from the current SQLite store.

    The inverse of :func:`reindex`: for every thread, (re)write ``threads/<id>.jsonl``
    as its metadata record + ordered events; rewrite the cross-thread snapshots; pick
    the shard depth for the thread count; set the manifest. Used to migrate an
    older monolithic ``events.jsonl`` into per-thread files (the old monolith files,
    if present, are removed), and to finish an index-only repair pass (e.g. the
    dedup-key backfill) by making the truth match. Idempotent — safe to re-run.

    This is the ONE operation that overwrites truth from the projection — the
    reverse of the normal flow — so it protects itself: it holds the reindex lock
    **exclusive** for the duration (no writer can append truth or commit to the
    index mid-emission; in-tree writers all hold it shared), and it refuses to run
    unless the store *contains* every effective content unit the truth holds
    (:func:`_truth_units_missing_from_store` — per-unit containment, not count
    parity, so a missing event can't hide behind an index-only one). A repair
    pass that rewrites payloads in place keeps its units (the ``dedup_key``
    column carries the identity), so the intended use survives the gate.

    Two further pre-flights guard the *content* of what gets written: the truth
    must carry no fields the running code's models don't map (``_coerce``
    tolerates them on reload, but a re-emit would drop them from the truth
    forever — an older binary must not lossily rewrite newer truth), and every
    store payload with a hash-tailed ``dedup_key`` must still re-hash to it
    (:func:`_store_rows_failing_key_hash` — a corrupted index row that kept its
    id and key must not replace the good truth line).

    ``force=True`` overrides the pre-flights only (for a deliberate, understood
    shrink or drop — e.g. a duplicate-collapse repair, or an in-place payload
    repair that didn't recompute its keys); it never skips the lock."""
    d = log_dir()
    (d / THREADS_SUBDIR).mkdir(parents=True, exist_ok=True)

    with _hold_reindex_lock():
        # Resolve any crashed drain's leftover intent first: a stale intent
        # surviving past the re-emit would frame baselines that no longer
        # describe the (replaced) files. The tail check would refuse to cut
        # them anyway; resolving here keeps that guard a backstop, not a path.
        with _truth_write_lock():
            pass
        if not force:
            missing, sample, dropped = _truth_units_missing_from_store(d)
            if missing:
                raise RuntimeError(
                    f"rebuild_truth_from_store: the store lacks {missing} event(s) "
                    f"the truth holds (sample: {sample}) — re-emitting would destroy "
                    "truth content the index lacks. Run `archive reindex` first "
                    "(or pass force=True if the shrink is intended)."
                )
            if dropped:
                fields = ", ".join(f"{k} ×{v}" for k, v in sorted(dropped.items()))
                raise RuntimeError(
                    f"rebuild_truth_from_store: the truth carries field(s) the running "
                    f"code's models don't map ({fields}) — re-emitting would silently "
                    "drop them from the truth forever. Run the code version that wrote "
                    "them (or pass force=True if the drop is intended)."
                )
            failing, bad_sample = _store_rows_failing_key_hash()
            if failing:
                raise RuntimeError(
                    f"rebuild_truth_from_store: {failing} store payload(s) fail their "
                    f"own dedup-key content hash (sample: {bad_sample}) — re-emitting "
                    "would promote suspect index content over the existing truth. "
                    "Investigate with `archive verify --hashes` and repair the index "
                    "(`archive reindex`) first, or pass force=True if the payloads are "
                    "known-good (an in-place repair that didn't recompute its keys)."
                )
        return _rebuild_truth_from_store_locked(d)


def _rebuild_truth_from_store_locked(d: Path) -> dict:
    with get_session() as s:
        threads = s.execute(select(Thread).order_by(Thread.id)).scalars().all()
        depth = _depth_for(len(threads))
        nt = ne = 0
        emitted: set[int] = set()
        for t in threads:  # outer list is materialized, so the inner event stream is the only cursor
            ev_rows = (_row_dict(ev) for ev in s.execute(
                select(Event).where(Event.thread_id == t.id).order_by(Event.id)
            ).scalars())
            ne += emit_thread_file(d, t.id, depth, _row_dict(t), ev_rows)
            emitted.add(t.id)
            nt += 1

    for name, model in _CROSS_THREAD.items():
        _write_snapshot(d, name, model)
    _write_snapshot(d, "import_state", ImportState)  # cursors survive the re-emit too

    # Locked read-modify-write: the re-emit owns the layout keys, not the whole
    # manifest — a verify run's hashes baseline (or any future key) survives.
    update_manifest(d, lambda m: m.update(
        {"version": TRUTH_FORMAT_VERSION, "shard_depth": depth, "last_checkpoint_at": _now_iso()}
    ))

    # Remove stale copies of re-emitted threads left at another shard depth. Their
    # content was just fully re-emitted at ``depth``, so an old-layout copy is pure
    # duplication — and on a later reindex a stale duplicate line would shadow the
    # fresh (possibly repaired) row for the same event id. Files whose ids the store
    # does NOT hold are left untouched.
    for path in list((d / THREADS_SUBDIR).rglob("*.jsonl")):
        try:
            tid = int(path.stem)
        except ValueError:  # pragma: no cover — stray file
            continue
        if tid in emitted and path != _thread_file(d, tid, depth):
            path.unlink()

    # Drop the old monolithic files this format replaces.
    for old in ("events.jsonl", "threads.jsonl"):
        p = d / old
        if p.exists():
            p.unlink()

    result = {"threads": nt, "events": ne, "shard_depth": depth}
    logger.info("jsonl_log rebuild_truth_from_store: %s", result)
    return result
