"""Integrity verification: the JSONL truth parses cleanly and matches the index.

:func:`verify` is the daily shallow pass (counts, parse errors, FTS parity,
schema parity, ``quick_check``); ``deep=True`` adds the id-level truth↔index
diff, ``hashes=True`` the content-level self-validation and cross-store payload
parity, and ``backup=<dest>`` the mirror parse-scan. A red run names its failed
components and appends its full evidence to ``<home>/verify-failures.jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .._config import resolve_paths
from .health import read_health, record_health, stamp_heartbeat


def verify(
    *,
    home: Optional[str] = None,
    deep: bool = False,
    hashes: bool = False,
    backup: Optional[str] = None,
) -> dict:
    """Integrity check: the JSONL truth parses cleanly and matches the SQLite index.

    Scans every per-thread truth file and compares to the projection's counts.
    The index is compared against ``events_effective`` — the truth's line count
    after collapsing superseded lines (re-appended ids, same-content twins), which
    is exactly what a reindex materializes; the raw line count and the superseded
    remainder are reported alongside. The curatorial log gets the same daily
    treatment: ``kg_events.jsonl`` is parse-scanned and its distinct-id count
    compared to the ``kg_events`` table below a kg watermark. ``ok`` is True only
    when the effective counts align (events, threads, and kg events), nothing
    failed to parse, the search surface is in parity (shadow ↔
    FTS5 row counts match with no orphan rows — silently unsearchable content is
    loss in effect, so it's checked on this daily cadence too), and the live
    index carries the full declared schema (:func:`_verify_schema` — ``create_all``
    never retrofits columns/indexes/constraints onto existing tables, so an
    under-enforced index must be seen and reindexed). A negative event
    drift (truth > index) is the *safe* direction — ``archive reindex`` rebuilds
    the index from truth; a positive drift (index > truth) or any parse error is a
    real integrity problem. Parse errors split into ``parse_errors_torn_tail``
    (the residue of a crash mid-append) and ``parse_errors_interior``; either kind
    is cleared by ``archive repair``, which quarantines the damaged lines and
    restores any committed content they shadowed from the index.

    **A red verify names its cause and keeps its evidence.** The result carries
    ``failed_components`` (exactly which checks failed — ``ok`` is derived from
    it), the health record carries the same list, and the *full* result of any
    failing run is appended to ``<home>/verify-failures.jsonl`` — samples
    included — so a red observed hours later is diagnosable from what it wrote,
    not from re-running an expensive pass against a store that has moved on.

    Every run records its outcome (``verify_last``: timestamp, ok, drift, the
    failed components) in ``<home>/health.json``, surfaced by ``archive
    status`` — an integrity check that silently stops running must look stale,
    not healthy. The escalated tiers' records (``verify_deep_last`` /
    ``verify_hashes_last``) carry each tier's *own* verdict, so the nightly age
    gate re-runs the tier that actually failed rather than both.

    ``deep=True`` adds an id-level comparison (both directions, below a stable id
    watermark so in-flight ingest can't false-alarm), dedup_key parity for ids on
    both sides, knowledge-layer parity, and dangling-reference checks. Slower — it
    re-reads the whole truth directory (twice) and queries the index per thread —
    but it sees what count parity can't: missing content masked by compensating
    errors, and exactly which events drifted.

    ``hashes=True`` adds content-level self-validation on both stores: each
    event's ``dedup_key`` ends in a hash of its payload's semantic content, so
    re-hashing the stored payload and comparing detects silent payload corruption
    (bit rot, a bad write) with no extra state. The hash covers only the payload's
    *semantic content keys* (``_DEDUP_CONTENT_KEYS``) — corruption in other payload
    fields (model names, metadata) is invisible to it. Events with no dedup_key
    at all carry nothing to self-validate against, so the same pass also runs a
    **cross-store payload parity** check: for every event id present on both
    sides, the truth line's payload and the index row's payload are canonically
    hashed and compared (``cross``). Writes flow one way (truth before index;
    nothing mutates a payload after commit), so the two stores are redundant
    copies and any disagreement is corruption on one side — this is what makes
    rot in an *unkeyed* payload detectable at all, instead of parsing clean,
    passing every count, and being promoted over the good index row by the next
    reindex. A *new* mismatch (key-hash or cross-store) fails
    ``ok``: each run's counts are persisted in ``manifest.json`` and diffed
    against the previous run's baseline — an increase (or any mismatch on a
    baseline-less first run) means content changed underneath its key since the
    last look, and health must go red until it's seen. The stamped baseline
    absorbs the count, so an acknowledged (e.g. legitimately-repaired-in-place)
    mismatch fails exactly one run rather than pinning verify red forever — and
    the failure ledger keeps its samples either way.
    CPU-heavy (re-hashes every payload on both stores).

    ``backup`` scans a backup mirror of the truth directory with the same
    parse-and-count pass as the live truth (no watermark bound — the mirror is a
    point-in-time copy) and reports its counts against the live ones. The backup
    check fails ``ok`` on parse errors in the mirror, and on a *shrinking
    mirror*: each run's effective count is recorded in health, and a count lower
    than the previous run's for the same destination means the mirror lost
    content between looks (the drill's coverage floor only catches drops ≥2%; a
    slow leak needs the run-over-run diff). A lower count than the *live* truth
    is expected staleness (the mirror ages between runs) and is reported as
    ``coverage`` for trending. Combined with ``hashes``, the mirror gets the
    content-level hash scan too — an unchanged destination file is never
    re-copied, so rot at rest is otherwise invisible forever. A mirror mismatch
    count above the previous run's for the same destination (or any mismatch on
    a first, baseline-less look) fails ``ok`` (``backup_hashes``), with the same
    fails-once absorption as the live scan.
    ``restore_drill`` is the step beyond this: actually rebuild an index from
    the mirror.

    The SQLite file itself gets a ``PRAGMA quick_check`` — page-level index
    corruption is otherwise invisible until a query happens to touch a bad page.
    On the ``hashes`` cadence this upgrades to the full ``integrity_check``,
    which also verifies b-tree index content against the tables (the only check
    that catches a corrupted index silently returning wrong query results).
    The index is rebuildable, so a failure here means ``archive reindex``, not
    data loss — but it must be *seen*.

    The shallow comparison is watermark-bounded on both sides too (ids at or
    below the index maxima captured up front), so verify can run against a live
    watcher without racing its ingest. An *empty* index gets no bound — a
    restored-but-not-yet-reindexed archive must show its full drift, not a
    vacuous OK.
    """
    from .._api import open_archive

    open_archive(home)
    from sqlalchemy import func, select

    from .._store import Event, KgEvent, Thread, get_session
    from .._truth import scan_truth_counts

    with get_session() as s:
        watermark = s.execute(select(func.max(Event.id))).scalar() or 0
        thread_watermark = s.execute(select(func.max(Thread.id))).scalar() or 0
        kg_watermark = s.execute(select(func.max(KgEvent.id))).scalar() or 0
    truth = scan_truth_counts(
        event_id_max=watermark or None, thread_id_max=thread_watermark or None,
        kg_event_id_max=kg_watermark or None,
    )
    with get_session() as s:
        thread_q = select(func.count()).select_from(Thread)
        if thread_watermark:
            thread_q = thread_q.where(Thread.id <= thread_watermark)
        idx_threads = s.execute(thread_q).scalar() or 0
        event_q = select(func.count()).select_from(Event)
        if watermark:
            event_q = event_q.where(Event.id <= watermark)
        idx_events = s.execute(event_q).scalar() or 0
        kg_q = select(func.count()).select_from(KgEvent)
        if kg_watermark:
            kg_q = kg_q.where(KgEvent.id <= kg_watermark)
        idx_kg = s.execute(kg_q).scalar() or 0
    drift_threads = int(idx_threads) - truth["threads"]
    drift_events = int(idx_events) - truth["events_effective"]
    drift_kg = int(idx_kg) - truth["kg_events"]
    # Self-check of the index file. The daily form is quick_check (reads every
    # page but skips index-content verification — a torn page still shows); the
    # ``hashes`` cadence upgrades to the full integrity_check, which also
    # verifies b-tree index content against the tables — the only check that
    # catches a corrupted index silently returning wrong query results.
    #
    # The pragma runs on its own just-opened connection, never a pooled one.
    # FTS5's integrity check consults per-connection segment-structure state,
    # and a connection that has lived across another process's writes (the
    # watcher rewrites ``event_search`` segments continuously) can spuriously
    # report "malformed inverted index" for a healthy index — the same pragma
    # on a fresh connection to the same file passes. A private connection
    # checks the same committed bytes without that hazard; it reads the file
    # the engine is actually bound to, so it judges the same database every
    # other component of this verify ran against.
    check_pragma = "integrity_check(10)" if hashes else "quick_check(10)"
    import sqlite3

    from .._store import get_engine

    index_file = get_engine().url.database
    qconn = sqlite3.connect(index_file, timeout=5.0)
    try:
        # Some page-level damage comes back as result rows, some as a raised
        # DatabaseError — both are the finding, not a crash: the check must
        # fail closed with the message, or verify dies on exactly the state
        # it exists to report. OperationalError (locked / can't open) stays
        # an error: environmental trouble must not impersonate corruption.
        try:
            qc_rows = qconn.execute(f"PRAGMA {check_pragma}").fetchall()
        except sqlite3.OperationalError:
            raise
        except sqlite3.DatabaseError as exc:
            qc_rows = [(str(exc),)]
    finally:
        qconn.close()
    quick_check = "ok" if [r[0] for r in qc_rows] == ["ok"] else "; ".join(
        str(r[0]) for r in qc_rows
    )
    # Search-surface parity, on the daily cadence (deep re-checks it with more
    # detail): the FTS shadow and the FTS5 table commit in the same transaction
    # as their events, so below the watermark the two row counts must match and
    # no shadow row may point at a missing event. Both are index-internal drift
    # — a reindex rebuilds them — but silently unsearchable content is loss in
    # effect, so it must be *seen* daily, not only on the deep cadence.
    fts_shadow = fts5 = fts_orphans = 0
    with get_session() as s:
        conn = s.connection().connection
        has_fts = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name IN ('events_fts', 'event_search')"
        ).fetchone()[0] == 2
        if has_fts and watermark:
            fts_shadow = conn.execute(
                "SELECT count(*) FROM events_fts WHERE event_id <= ?", (watermark,)
            ).fetchone()[0]
            fts5 = conn.execute(
                "SELECT count(*) FROM event_search WHERE event_id <= ?", (watermark,)
            ).fetchone()[0]
            fts_orphans = conn.execute(
                "SELECT count(*) FROM events_fts f WHERE f.event_id <= ? "
                "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = f.event_id)",
                (watermark,),
            ).fetchone()[0]
    # Declared-schema parity (cheap PRAGMA introspection): a live index that
    # predates a model change runs under-enforced until a reindex — that gap
    # must be seen on the daily cadence, not discovered from its consequences.
    schema = _verify_schema()
    # ``ok`` is derived from this list at the end — the components and the
    # verdict cannot disagree, and a red run always names its cause.
    failed: list[str] = []
    if drift_threads != 0:
        failed.append("drift_threads")
    if drift_events != 0:
        failed.append("drift_events")
    if drift_kg != 0:
        failed.append("drift_kg_events")
    if truth["parse_errors"]:
        failed.append("parse_errors")
    if quick_check != "ok":
        failed.append("integrity_check" if hashes else "quick_check")
    if fts_shadow != fts5 or fts_orphans:
        failed.append("fts_parity")
    if not schema["ok"]:
        failed.append("schema")
    result = {
        "ok": not failed,
        "schema": schema,
        "truth": truth,
        "index": {
            "threads": int(idx_threads),
            "events": int(idx_events),
            "kg_events": int(idx_kg),
            "quick_check": quick_check,
            "check": "integrity_check" if hashes else "quick_check",
        },
        "drift": {"threads": drift_threads, "events": drift_events, "kg_events": drift_kg},
        "fts": {
            "shadow_rows": int(fts_shadow),
            "fts5_rows": int(fts5),
            "orphan_rows": int(fts_orphans),
        },
    }
    if deep:
        result["deep"] = _verify_deep(watermark)
        if not result["deep"]["ok"]:
            failed.append("deep")
    new_mismatches = False
    if hashes:
        result["hashes"] = _verify_hashes(watermark)
        # Detected corruption must fail health, not just be reported: any *new*
        # mismatch since the previous baseline (or any mismatch at all on a
        # first, baseline-less run) fails ``ok``. The baseline this run stamps
        # absorbs the count, so the failure fires once and the delta signal
        # stays meaningful — a legitimately-repaired payload's stale hash
        # doesn't keep verify red forever, but it is *seen* red once (and the
        # failure ledger below keeps its samples).
        h, delta = result["hashes"], result["hashes"].get("delta")
        if delta is not None:
            new_mismatches = any(v > 0 for v in delta.values())
        else:
            new_mismatches = bool(
                h["truth"]["mismatched"] or h["index"]["mismatched"]
                or h["cross"]["mismatched"]
            )
        result["hashes"]["new_mismatches"] = new_mismatches
        if new_mismatches:
            failed.append("hashes")
    if backup is not None:
        result["backup"] = _verify_backup(Path(backup).expanduser(), truth)
        if not result["backup"]["ok"]:
            failed.append("backup")
        if hashes and "scan" in result["backup"]:
            # Content-level rot detection on the mirror too: an unchanged
            # destination file is never re-copied (size+mtime skip), so silent
            # corruption at rest would otherwise persist forever while the
            # parse-and-count scan stays green. No watermark — the mirror is a
            # point-in-time copy. Same baseline-delta semantics as the live
            # hashes pass: a mismatch count *above* the previous run's for this
            # destination (or any mismatch on a baseline-less first run) fails
            # ``ok``; recording the new count absorbs it, so the failure fires
            # once and the ledger keeps its samples.
            dest_path = Path(backup).expanduser()
            bh = _hash_scan_truth_dir(dest_path, watermark=None)
            result["backup"]["hashes"] = bh
            prev = read_health().get("backup_hashes_last") or {}
            prev_count = (
                prev.get("mismatched") if prev.get("dest") == str(dest_path) else None
            )
            bh["new_mismatches"] = (
                bh["mismatched"] > int(prev_count)
                if prev_count is not None else bool(bh["mismatched"])
            )
            if bh["new_mismatches"]:
                failed.append("backup_hashes")
            record_health("backup_hashes_last", {
                "dest": str(dest_path), "mismatched": bh["mismatched"],
            })
    result["ok"] = not failed
    result["failed_components"] = failed
    if failed:
        # Keep the evidence: the counts and samples of a failing run exist only
        # in this dict, and health records booleans. Without the ledger, a red
        # observed later is undiagnosable except by re-running the whole pass
        # against a store that has moved on.
        ledger = _append_verify_failure(result)
        if ledger:
            result["failure_log"] = ledger

    # Record the outcome in the home's health file so `archive status` (and
    # anything watching it) can see when integrity was last checked and how it
    # went — a verify that silently stops running is indistinguishable from a
    # healthy one otherwise. Staleness of the timestamp is the primary signal: a
    # crash mid-verify leaves the previous record standing, and its age says so.
    # `deep` / `hashes` / `backup` are the run's TIER, not decoration: they are
    # what `_stage_recovered` compares against the tier of a verify that failed,
    # so a basic pass can never retire a deep-tier red. `backup` records whether
    # the mirror was parse-scanned — the check a bare `archive verify` skips.
    record_health("verify_last", {
        "ok": bool(result["ok"]),
        "deep": bool(deep),
        "hashes": bool(hashes),
        "backup": bool(backup),
        "drift_events": drift_events,
        "drift_threads": drift_threads,
        "parse_errors": truth["parse_errors"],
        "failed": failed,
    })
    # The escalated tiers get their own records: ``verify_last`` is overwritten
    # by every shallow run, so these are what age-gated schedulers (``nightly``)
    # read to know when a deep / hashes pass last actually happened. Each
    # carries its own tier's verdict — a red caused by another component must
    # not force the expensive tiers to re-run every night.
    if deep:
        record_health("verify_deep_last", {"ok": bool(result["deep"]["ok"])})
    if hashes:
        record_health("verify_hashes_last", {"ok": not new_mismatches})
    # A verify run on its own is how a failed nightly's verify stage gets retired
    # — republish the verdict so a proven fix reaches the monitor now, not at 04:00.
    stamp_heartbeat()
    return result


def _append_verify_failure(result: dict) -> Optional[str]:
    """Append the full result of a failing verify to ``<home>/verify-failures.jsonl``
    — the durable evidence a red run leaves behind (health.json holds booleans;
    the counts and samples live only in the result dict). Append-only and
    fail-soft: the ledger is advisory, so a write error is logged, never raised.
    Returns the ledger path, or None when it could not be written."""
    from datetime import datetime, timezone

    p = resolve_paths().home / "verify-failures.jsonl"
    try:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"at": datetime.now(timezone.utc).isoformat(), **result},
                default=str,
            ))
            fh.write("\n")
        return str(p)
    except OSError:
        import logging

        logging.getLogger(__name__).exception("could not append to verify-failures.jsonl")
        return None


def _verify_schema() -> dict:
    """Live index schema vs the declared models — the under-enforcement check.

    Startup schema provisioning is ``create_all`` only: it creates *missing
    tables* but never retrofits new columns, indexes, or constraints onto
    existing ones, so a live index created before a model change can silently
    run under-enforced (e.g. the ``(thread_id, dedup_key)`` unique index absent
    → DB-level dedup off) until the next reindex. Introspects the live SQLite
    schema against ``Base.metadata`` directly — no stored version stamp to
    drift — and reports missing columns, named indexes, and unique constraints
    (matched by column set; SQLite realizes them as auto-named unique indexes).
    Extra live-side objects are ignored: an older/foreign index must still
    open. Anything missing fails ``verify``'s ``ok`` — the fix is ``archive
    reindex``, which builds a fresh index with the full declared schema."""
    from sqlalchemy import UniqueConstraint as _UC

    from .._store import Base, get_session

    missing_tables: list[str] = []
    missing_columns: list[str] = []
    missing_indexes: list[str] = []
    missing_uniques: list[str] = []
    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3
        live_tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in Base.metadata.tables.values():
            if table.name not in live_tables:
                missing_tables.append(table.name)
                continue
            live_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table.name})")}
            missing_columns += [
                f"{table.name}.{c.name}" for c in table.columns if c.name not in live_cols
            ]
            index_list = conn.execute(f"PRAGMA index_list({table.name})").fetchall()
            live_index_names = {r[1] for r in index_list}
            missing_indexes += [
                str(idx.name) for idx in table.indexes if idx.name not in live_index_names
            ]
            live_unique_colsets = {
                frozenset(
                    r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})") if r[2]
                )
                for row in index_list
                if row[2]  # unique flag
            }
            for uc in table.constraints:
                if not isinstance(uc, _UC):
                    continue
                cols = frozenset(c.name for c in uc.columns)
                if cols not in live_unique_colsets:
                    missing_uniques.append(f"{table.name}({', '.join(sorted(cols))})")
    return {
        "ok": not (missing_tables or missing_columns or missing_indexes or missing_uniques),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "missing_unique_constraints": missing_uniques,
    }


def _verify_backup(dest: Path, live_truth: dict) -> dict:
    """Scan a backup mirror of the truth dir and compare it to the live scan.

    The restore-drill primitive: the same parse-and-count pass ``verify`` runs on
    the live truth, pointed at the mirror. Zero parse errors is the hard
    requirement (a mirror that doesn't parse doesn't restore); the effective-count
    ratio against the live truth (``coverage``) quantifies staleness — it should
    hover near 1.0 and only ever *rise* between backup runs.

    A mirror that *shrank* fails too: each run records the mirror's effective
    count in health (``backup_scan_last``), and a count below the previous run's
    for the same destination means the backup lost content between looks —
    something deleted or truncated mirror files out-of-band. The restore drill's
    coverage floor only catches drops ≥2% of the archive; the run-over-run diff
    is what sees a slow leak. Recording the new count absorbs the drop, so —
    like the hashes baseline — a deliberate shrink (an ``--allow-shrink``
    re-emit after repair) fails exactly one run instead of pinning verify red."""
    from .._truth import scan_truth_counts

    if not (dest / "manifest.json").exists() and not (dest / "threads").exists():
        return {"dest": str(dest), "ok": False, "error": "not a truth mirror"}
    scan = scan_truth_counts(truth_dir=dest)
    live_effective = live_truth["events_effective"] or 1
    prev = read_health().get("backup_scan_last") or {}
    prev_effective = (
        prev.get("events_effective") if prev.get("dest") == str(dest) else None
    )
    out = {
        "dest": str(dest),
        "ok": scan["parse_errors"] == 0,
        "scan": scan,
        "coverage": round(scan["events_effective"] / live_effective, 6),
    }
    if prev_effective is not None and scan["events_effective"] < int(prev_effective):
        out["ok"] = False
        out["effective_drop"] = {
            "previous": int(prev_effective),
            "previous_at": prev.get("at"),
            "current": scan["events_effective"],
        }
    record_health("backup_scan_last", {
        "dest": str(dest),
        "events_effective": scan["events_effective"],
    })
    return out


def _hash_key_check(payload: object, dedup_key: str) -> Optional[bool]:
    """True = the payload re-hashes to the content hash embedded in its own
    ``dedup_key`` (the last ``:``-segment; see
    ``thread_archive._thread_import.event_builder.compute_dedup_key``); False = mismatch;
    None = the key carries no hash tail (nothing to validate against)."""
    from .._truth.jsonl_log import _hash_key_check as _impl

    return _impl(payload, dedup_key)


def _hash_scan_truth_dir(truth_dir: Path, watermark: Optional[int]) -> dict:
    """Re-hash every event payload in a truth directory against its dedup_key's
    embedded content hash. The scan half of ``verify --hashes``, reusable
    against a backup mirror (``watermark=None`` — a point-in-time copy has no
    in-flight ingest to bound out). ``no_key`` counts events with no dedup_key
    at all: they carry nothing to validate against, so they are invisible to
    this check — the count keeps that coverage boundary visible."""
    from .._truth.jsonl_log import THREADS_SUBDIR, _iter_jsonl

    checked = mismatched = skipped = no_key = 0
    sample: list[int] = []
    threads_dir = truth_dir / THREADS_SUBDIR
    if threads_dir.exists():
        for path in threads_dir.rglob("*.jsonl"):
            for rec in _iter_jsonl(path):
                if rec.get("type", "event") != "event":
                    continue
                ev_id, key = rec.get("id"), rec.get("dedup_key")
                if ev_id is None or (watermark is not None and ev_id > watermark):
                    continue
                if not key:
                    no_key += 1
                    continue
                verdict = _hash_key_check(rec.get("payload"), key)
                if verdict is None:
                    skipped += 1
                    continue
                checked += 1
                if not verdict:
                    mismatched += 1
                    if len(sample) < 10:
                        sample.append(int(ev_id))
    return {
        "checked": checked, "mismatched": mismatched,
        "unhashed_keys": skipped, "no_key": no_key, "mismatch_sample": sample,
    }


def _payload_fingerprint(event_type: object, payload: object) -> str:
    """Canonical content fingerprint of one stored event — the cross-store
    comparator. Both stores' copies are parsed to Python objects first, so
    serializer differences (key order, ascii escaping) can't false-positive;
    timestamps are deliberately excluded (the two stores format them
    differently)."""
    import hashlib

    blob = json.dumps([event_type, payload], sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _verify_hashes(watermark: int) -> dict:
    """Content-level validation of every stored payload, walking truth and index
    together one thread at a time.

    Two independent checks per event:

    * **Key-hash self-validation** (``truth`` / ``index``): re-hash the stored
      payload against the content hash embedded in its own ``dedup_key`` (its
      last ``:``-segment; see ``thread_archive._thread_import.event_builder.compute_dedup_key``).
      A mismatch means the payload changed since its key was computed —
      corruption, or an in-place payload repair that didn't recompute the key.
      Events with a key whose tail isn't a hash are skipped and counted
      (``unhashed_keys``); events with no dedup_key at all are counted too
      (``no_key``).
    * **Cross-store payload parity** (``cross``): for every id present on both
      sides, compare a canonical fingerprint of (event_type, payload) between
      the truth line (last line per id — what a reindex materializes) and the
      index row. Writes flow one way and nothing mutates payloads after commit,
      so the stores are redundant copies: disagreement is corruption on one
      side. This is the only check that can see rot in an *unkeyed* payload —
      a majority of any archive that predates dedup keys — which would
      otherwise parse clean, pass every count, and be promoted over the good
      index row by the next reindex.

    The caller (``verify``) fails ``ok`` when any mismatch count *increased*
    since the previous baseline (see ``new_mismatches``); the counts themselves
    are informational."""
    import json as _json

    from .._store import get_session
    from .._truth.jsonl_log import (
        THREADS_SUBDIR,
        _iter_jsonl,
        _shard_depth,
        _thread_file,
        log_dir,
    )

    d = log_dir()
    threads_dir = d / THREADS_SUBDIR
    depth = _shard_depth(d)

    truth_checked = truth_mismatched = truth_unhashed = truth_no_key = 0
    truth_sample: list[int] = []
    index_checked = index_mismatched = index_skipped = 0
    index_sample: list[int] = []
    cross_compared = cross_mismatched = 0
    cross_sample: list[int] = []

    files_by_stem: dict[str, list[Path]] = {}
    if threads_dir.exists():
        for path in threads_dir.rglob("*.jsonl"):
            files_by_stem.setdefault(path.stem, []).append(path)
    truth_tids: set[int] = set()
    for stem in files_by_stem:
        try:
            truth_tids.add(int(stem))
        except ValueError:  # pragma: no cover — stray file
            continue

    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3 — stream, don't materialize
        index_no_key = conn.execute(
            "SELECT count(*) FROM events WHERE dedup_key IS NULL AND id <= ?",
            (watermark,),
        ).fetchone()[0]
        idx_tids = {
            int(r[0]) for r in conn.execute(
                "SELECT DISTINCT thread_id FROM events WHERE id <= ?", (watermark,)
            )
        }
        for tid in sorted(idx_tids | truth_tids):
            # Truth side: every line is key-checked; the last line per id wins
            # the fingerprint (canonical-depth file last, matching reindex).
            fingerprints: dict[int, str] = {}
            paths = sorted(files_by_stem.get(str(tid), []))
            paths.sort(key=lambda p: p == _thread_file(d, tid, depth))
            for path in paths:
                for rec in _iter_jsonl(path):
                    if rec.get("type", "event") != "event":
                        continue
                    ev_id, key = rec.get("id"), rec.get("dedup_key")
                    if ev_id is None or ev_id > watermark:
                        continue
                    payload = rec.get("payload")
                    if not key:
                        truth_no_key += 1
                    else:
                        verdict = _hash_key_check(payload, key)
                        if verdict is None:
                            truth_unhashed += 1
                        else:
                            truth_checked += 1
                            if not verdict:
                                truth_mismatched += 1
                                if len(truth_sample) < 10:
                                    truth_sample.append(int(ev_id))
                    fingerprints[int(ev_id)] = _payload_fingerprint(
                        rec.get("event_type"), payload
                    )
            # Index side of the same thread, plus the cross-store comparison.
            for ev_id, key, etype, payload_text in conn.execute(
                "SELECT id, dedup_key, event_type, payload FROM events "
                "WHERE thread_id = ? AND id <= ?", (tid, watermark)
            ):
                try:
                    payload = (
                        _json.loads(payload_text)
                        if isinstance(payload_text, str) else payload_text
                    )
                except ValueError:
                    payload = None
                if key:
                    verdict = _hash_key_check(payload, key)
                    if verdict is None:
                        index_skipped += 1
                    else:
                        index_checked += 1
                        if not verdict:
                            index_mismatched += 1
                            if len(index_sample) < 10:
                                index_sample.append(int(ev_id))
                truth_fp = fingerprints.get(int(ev_id))
                if truth_fp is not None:
                    cross_compared += 1
                    if truth_fp != _payload_fingerprint(etype, payload):
                        cross_mismatched += 1
                        if len(cross_sample) < 10:
                            cross_sample.append(int(ev_id))

    result = {
        "truth": {
            "checked": truth_checked, "mismatched": truth_mismatched,
            "unhashed_keys": truth_unhashed, "no_key": truth_no_key,
            "mismatch_sample": truth_sample,
        },
        "index": {
            "checked": index_checked, "mismatched": index_mismatched,
            "unhashed_keys": index_skipped, "no_key": int(index_no_key),
            "mismatch_sample": index_sample,
        },
        "cross": {
            "compared": cross_compared, "mismatched": cross_mismatched,
            "mismatch_sample": cross_sample,
        },
    }
    # The signal is the mismatch count *jumping* between runs, so persist this
    # run's counts in the manifest and surface the previous run's for comparison.
    # Locked read-modify-write: a checkpoint stamping its own keys concurrently
    # must not lose this baseline, nor vice versa.
    from datetime import datetime, timezone

    from .._truth.jsonl_log import update_manifest

    baseline = {
        "at": datetime.now(timezone.utc).isoformat(),
        "truth_mismatched": truth_mismatched,
        "index_mismatched": index_mismatched,
        "cross_mismatched": cross_mismatched,
    }
    previous: dict = {}

    def _stamp(m: dict) -> None:
        previous.update(m.get("hashes_baseline") or {})
        m["hashes_baseline"] = baseline

    update_manifest(log_dir(), _stamp)
    if previous:
        result["previous"] = previous
        result["delta"] = {
            "truth_mismatched": truth_mismatched - int(previous.get("truth_mismatched", 0)),
            "index_mismatched": index_mismatched - int(previous.get("index_mismatched", 0)),
            "cross_mismatched": cross_mismatched - int(previous.get("cross_mismatched", 0)),
        }
    return result


def _verify_deep(watermark: int) -> dict:
    """Id-level truth↔index comparison plus knowledge-layer checks.

    Only events with ``id <= watermark`` (committed before the scan began) are
    compared: the truth line for any such event was written *before* its commit
    (the staging invariant), so at any later read it must be present — and any
    index row at or below the watermark must have a truth line. Everything above
    the watermark is in-flight ingest and skipped.

    Truth-only ids are split into two classes: **superseded** (a same-content twin
    — equal ``dedup_key`` — exists in the index under another id; the benign
    residue of a lost-commit re-import, collapsed on reindex) and **missing**
    (no twin: content the index genuinely lacks — recoverable via reindex).
    Index-only ids are the forbidden direction and always fail.

    For ids present on both sides, the stored ``dedup_key`` values are compared
    (``events_key_mismatch``): a mismatch means the two stores disagree on an
    event's content identity — an index-only mutation the truth never received
    (a reindex would rewrite it) or corruption on one side. Reported with a
    sample, and it fails ``ok``.

    The search surface is checked too (both FTS tables are written in the same
    transaction as their events): orphan shadow rows and a shadow↔FTS5 row-count
    mismatch fail. Indexable events with no shadow row are re-extracted: one whose
    payload yields no searchable text legitimately has no row (reported as
    ``empty_extract_events``); one whose extraction yields text today is silently
    unfindable (``unindexed_events``) and fails — ``archive reindex`` rebuilds the
    surface.

    Thread *metadata* parity (title/description/summary between the winning truth
    record and the index row) is report-only: an in-flight metadata commit can
    legitimately race the scan, but a persistent mismatch means a missed re-stage —
    and the next reindex would revert the index to the stale truth record.

    The knowledge layer gets the same treatment as events: the kg log and table
    are id-diffed below a kg watermark captured before any file is read (so a
    live librarian write can't false-alarm), and ids on both sides are
    content-compared (``kg.content_mismatch``) — the log is small enough to
    fingerprint whole, and it is the curation history's only truth.
    """
    import json as _json

    from sqlalchemy import text as sa_text

    from .._store import get_session
    from .._truth.jsonl_log import (
        KG_EVENTS_FILE,
        THREADS_SUBDIR,
        _iter_jsonl,
        log_dir,
        thread_file_load_order,
    )

    d = log_dir()
    threads_dir = d / THREADS_SUBDIR

    # The kg watermark, captured before any file is read: the kg id-diff below
    # is otherwise unbounded, and a librarian write landing mid-scan (its truth
    # line is durable before its commit, but this pass may read the file first)
    # would false-alarm ``kg_index_only`` against a perfectly healthy archive.
    with get_session() as s0:
        kg_watermark = s0.execute(
            sa_text("SELECT coalesce(max(id), 0) FROM kg_events")
        ).scalar() or 0

    # Pass 1 — truth ids per thread (≤ watermark), which files hold each thread,
    # and the winning (last, in reindex's load order — canonical-depth file
    # last) thread record per id.
    truth_ids: dict[int, set[int]] = {}
    thread_files: dict[int, list] = {}
    truth_meta: dict[int, dict] = {}
    if threads_dir.exists():
        for path in thread_file_load_order(d):
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type", "event") == "thread":
                        if rec.get("id") is not None:
                            truth_meta[int(rec["id"])] = rec
                        continue
                    if rec.get("type", "event") != "event":
                        continue
                    ev_id, tid = rec.get("id"), rec.get("thread_id")
                    if ev_id is None or tid is None or ev_id > watermark:
                        continue
                    truth_ids.setdefault(int(tid), set()).add(int(ev_id))
                    files = thread_files.setdefault(int(tid), [])
                    if path not in files:
                        files.append(path)

    # Pass 2 — per-thread diff against the index.
    index_only: list[int] = []
    missing: list[int] = []
    key_mismatch: list[int] = []
    superseded = 0
    with get_session() as s:
        idx_tids = {
            r[0] for r in s.execute(sa_text(
                "SELECT DISTINCT thread_id FROM events WHERE id <= :wm"), {"wm": watermark})
        }
        for tid in sorted(set(truth_ids) | idx_tids):
            rows = s.execute(sa_text(
                "SELECT id, dedup_key FROM events WHERE thread_id = :t AND id <= :wm"),
                {"t": tid, "wm": watermark}).all()
            idx_map = {r[0]: r[1] for r in rows}
            t_set = truth_ids.get(tid, set())
            index_only.extend(sorted(set(idx_map) - t_set))
            if not t_set:
                continue
            # Re-read this thread's files for every truth id's dedup_key: the last
            # line for an id wins, matching what a reindex would materialize.
            truth_keys: dict[int, str | None] = {}
            for path in thread_files.get(tid, []):
                for rec in _iter_jsonl(path):
                    if rec.get("type", "event") == "event" and rec.get("id") in t_set:
                        truth_keys[rec["id"]] = rec.get("dedup_key")
            idx_keys = {v for v in idx_map.values() if v}
            for ev_id in sorted(t_set - set(idx_map)):
                if truth_keys.get(ev_id) and truth_keys[ev_id] in idx_keys:
                    superseded += 1
                else:
                    missing.append(ev_id)
            for ev_id in sorted(t_set & set(idx_map)):
                if truth_keys.get(ev_id) != idx_map[ev_id]:
                    key_mismatch.append(ev_id)

        # Thread-metadata parity: the winning truth record vs the index row, on the
        # stable text fields. Report-only — the truth line for a metadata update is
        # staged before its commit, so a commit landing between pass 1's file read
        # and this query legitimately shows index-newer-than-truth; a *persistent*
        # mismatch means a re-stage was missed, and the next reindex would silently
        # revert the index to the stale truth record.
        meta_mismatch: list[int] = []
        meta_rows = s.execute(sa_text(
            "SELECT id, title, description, summary FROM threads")).all()
        for tid, *idx_fields in meta_rows:
            rec = truth_meta.get(int(tid))
            if rec is None:
                continue  # count parity (shallow verify) owns missing records
            for field, idx_val in zip(("title", "description", "summary"), idx_fields):
                if (rec.get(field) or None) != (idx_val or None):
                    meta_mismatch.append(int(tid))
                    break

        # Knowledge layer: the kg truth log vs its table, by id — both sides
        # bounded by the kg watermark captured up front, so a live librarian
        # write can't false-alarm — plus content parity for ids on both sides
        # (the kg analogue of the events cross-store check; the log is small
        # enough to compare whole).
        kg_recs: dict[int, dict] = {}
        for rec in _iter_jsonl(d / KG_EVENTS_FILE):
            rid = rec.get("id")
            if rid is not None and int(rid) <= kg_watermark:
                kg_recs[int(rid)] = rec  # last line per id wins, like reindex
        kg_rows = {
            int(r[0]): tuple(r) for r in s.execute(sa_text(
                "SELECT id, event_type, entity_type, entity_id, payload "
                "FROM kg_events WHERE id <= :wm"), {"wm": kg_watermark})
        }
        kg_index_only = len(set(kg_rows) - set(kg_recs))
        kg_truth_only = len(set(kg_recs) - set(kg_rows))
        kg_content_mismatch = 0
        for rid in set(kg_recs) & set(kg_rows):
            rec, row = kg_recs[rid], kg_rows[rid]
            row_payload = row[4]
            if isinstance(row_payload, str):
                try:
                    row_payload = _json.loads(row_payload)
                except ValueError:
                    row_payload = None
            truth_fp = _payload_fingerprint(
                [rec.get("event_type"), rec.get("entity_type"), rec.get("entity_id")],
                rec.get("payload"),
            )
            if truth_fp != _payload_fingerprint([row[1], row[2], row[3]], row_payload):
                kg_content_mismatch += 1

        # Dangling references.
        dangling_links = s.execute(sa_text(
            "SELECT count(*) FROM thread_links l WHERE "
            "NOT EXISTS(SELECT 1 FROM threads t WHERE t.id = l.source_thread_id) "
            "OR NOT EXISTS(SELECT 1 FROM threads t WHERE t.id = l.target_thread_id)"
        )).scalar() or 0
        dangling_citations = s.execute(sa_text(
            "SELECT count(*) FROM topic_messages m "
            "WHERE m.archived_at IS NULL "  # tombstoned evidence is history, not a live ref
            "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = m.event_id)"
        )).scalar() or 0
        # A live citation whose recorded thread disagrees with the cited event's
        # actual thread: the column is derived from the event, so disagreement
        # means an unvalidated write or a stale snapshot seed. Reindex realigns
        # these (citation reconciliation), so a persistent count means rot.
        citation_thread_mismatch = s.execute(sa_text(
            "SELECT count(*) FROM topic_messages m JOIN events e ON e.id = m.event_id "
            "WHERE m.archived_at IS NULL AND m.thread_id != e.thread_id"
        )).scalar() or 0
        dangling_events = s.execute(sa_text(
            "SELECT count(*) FROM events e "
            "WHERE NOT EXISTS(SELECT 1 FROM threads t WHERE t.id = e.thread_id)"
        )).scalar() or 0
        dup_pairs = s.execute(sa_text(
            "SELECT count(*) FROM (SELECT 1 FROM events WHERE dedup_key IS NOT NULL "
            "GROUP BY thread_id, dedup_key HAVING count(*) > 1)"
        )).scalar() or 0

        # Search-surface parity. The FTS shadow (events_fts) and the FTS5 table
        # (event_search) are written in the same transaction as their events, so
        # below the watermark: no shadow row may point at a missing event (orphans),
        # and the two surfaces must hold the same row count. Coverage — indexable
        # events with no shadow row — is re-extracted event by event to split the
        # legitimately-empty (a payload that yields no searchable text has no row
        # by design) from the genuinely unindexed (extraction yields text today,
        # so the event is silently unfindable — real drift; `archive reindex`
        # rebuilds the surface). The gap set is small on a healthy archive, so
        # re-extracting only it stays cheap where re-extracting the corpus isn't.
        fts_orphans = fts_shadow_rows = fts5_rows = fts_empty_extract = 0
        fts_unindexed: list[int] = []
        has_fts = s.execute(sa_text(
            "SELECT count(*) FROM sqlite_master WHERE name IN ('events_fts', 'event_search')"
        )).scalar() == 2
        if has_fts:
            fts_orphans = s.execute(sa_text(
                "SELECT count(*) FROM events_fts f WHERE f.event_id <= :wm "
                "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = f.event_id)"),
                {"wm": watermark}).scalar() or 0
            fts_shadow_rows = s.execute(sa_text(
                "SELECT count(*) FROM events_fts WHERE event_id <= :wm"),
                {"wm": watermark}).scalar() or 0
            fts5_rows = s.execute(sa_text(
                "SELECT count(*) FROM event_search WHERE event_id <= :wm"),
                {"wm": watermark}).scalar() or 0
            from .._retrieval._extract import INDEXABLE_EVENT_TYPES, extract_fts_content

            types = ", ".join(f"'{t}'" for t in INDEXABLE_EVENT_TYPES)
            cur = s.connection().connection.execute(  # raw sqlite3 — stream the gap set
                f"SELECT e.id, e.event_type, e.payload FROM events e "  # noqa: S608 — types from INDEXABLE_EVENT_TYPES
                f"WHERE e.id <= ? AND e.event_type IN ({types}) "
                "AND NOT EXISTS(SELECT 1 FROM events_fts f WHERE f.event_id = e.id)",
                (watermark,),
            )
            for ev_id, etype, payload_text in cur:
                try:
                    payload = _json.loads(payload_text) if isinstance(payload_text, str) else payload_text
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and extract_fts_content(etype, payload):
                    fts_unindexed.append(int(ev_id))
                else:
                    fts_empty_extract += 1

    ok = (
        not index_only and not missing and not key_mismatch
        and kg_index_only == 0 and kg_content_mismatch == 0
        and dangling_links == 0 and dangling_citations == 0 and dangling_events == 0
        and citation_thread_mismatch == 0
        and fts_orphans == 0 and fts_shadow_rows == fts5_rows and not fts_unindexed
    )
    return {
        "ok": ok,
        "watermark": watermark,
        "events_index_only": len(index_only),
        "index_only_sample": index_only[:10],
        "events_missing_from_index": len(missing),
        "missing_sample": missing[:10],
        "events_key_mismatch": len(key_mismatch),
        "key_mismatch_sample": key_mismatch[:10],
        "events_superseded_twins": superseded,
        "thread_meta_mismatch": len(meta_mismatch),
        "thread_meta_sample": meta_mismatch[:10],
        "kg": {
            "index_only": kg_index_only, "truth_only": kg_truth_only,
            "content_mismatch": kg_content_mismatch, "watermark": int(kg_watermark),
        },
        "dangling": {
            "link_endpoints": int(dangling_links),
            "citation_events": int(dangling_citations),
            "citation_thread_mismatch": int(citation_thread_mismatch),
            "event_threads": int(dangling_events),
        },
        "duplicate_content_pairs_index": int(dup_pairs),
        "fts": {
            "orphan_rows": int(fts_orphans),
            "shadow_rows": int(fts_shadow_rows),
            "fts5_rows": int(fts5_rows),
            "unindexed_events": len(fts_unindexed),
            "unindexed_sample": fts_unindexed[:10],
            "empty_extract_events": int(fts_empty_extract),
        },
    }
