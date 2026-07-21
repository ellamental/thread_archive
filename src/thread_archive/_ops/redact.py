"""Crypto-shredding redaction: get content out of the archive without deleting history.

:func:`redact_events` replaces the payloads of selected events with a marker
envelope (``{"_redacted": {"key_id": ..., "at": ...}}``) everywhere the content
lives:

* the thread's truth file — every line carrying a target event id is rewritten,
  superseded duplicate lines included, so no plaintext byte survives in the file;
* citation quotes — ``topic_messages`` rows, the ``topic_messages.jsonl``
  snapshot, and ``kg_events`` evidence payloads that quote a target event are
  scrubbed to ``"[redacted]"``;
* the index — ``events.payload``, the ``events_fts`` shadow + ``event_search``
  FTS5 docs, and ``event_vectors`` rows (live table and the ``vectors.sqlite``
  sidecar — an embedding of a secret is recoverable enough to count as the
  secret);
* the blob store — ``truth/blobs/`` files holding the events' extracted binary
  content (screenshots, documents; see :mod:`.._truth.blobs`), deleted unless
  another live event still references the same content hash. The recovery
  bundle carries that content reconstituted inline, so unredact restores it
  without the file.

The plaintext is not destroyed. It is AES-256-GCM-encrypted into a *recovery
bundle* on the redaction record (``truth/redactions.jsonl``, append-only), keyed
by a fresh per-redaction key held in ``<home>/keyring.json`` — deliberately
OUTSIDE the truth directory, so the truth mirror and its dated generations hold
ciphertext only. The keyring itself rides ``archive backup``'s *head-only*
``.recovery`` bundle by default (losing the live home must not crypto-erase
every active redaction); because that bundle is never snapshotted into
generations, a
``--forget`` leaves the backup on the next run too. ``{"backup":
{"include_keyring": false}}`` in config.json keeps backups ciphertext-only for
operators who escrow keys elsewhere.
Three states fall out of one mechanism:

* **redacted** — key in the keyring; :func:`unredact` restores losslessly;
* **escrowed** — key exported (``--show-key``) and removed (``--forget``); the
  machine can no longer produce the plaintext, the operator still can
  (``--restore-key``, then ``unredact``);
* **forgotten** — key destroyed everywhere; the ciphertext is noise
  (crypto-erasure) while the truth log keeps its shape: event ids, dedup keys,
  and the redaction record itself remain honest recorded history.

Crash ordering: the key and the redaction record land fsynced *before* any
plaintext is touched, so every later failure point is recoverable; truth
rewrites and index updates then run under the exclusive reindex lock (writers
are quiescent — the same discipline as repair). A crash mid-way leaves either
intact plaintext (re-run the redact; the orphaned record's bundle is dead
weight, not damage) or a consistent redacted state; ``archive reindex`` always
converges the index to the truth. The store updates here commit through plain
sessions with nothing staged, so the truth drain never fires inside the lock.

Deliberate limits, surfaced to the operator rather than hidden: the thread's
title/summary are not scrubbed, and the ORIGINAL provider store (the file the
watcher imported from) still holds the plaintext — the result carries both
notes. Re-ingest cannot resurrect content: the marker row keeps its
``dedup_key``, so the importer's identity check skips the source lines.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text as sa_text

from .._config import resolve_paths
from .._store import Event, KgEvent, Thread, TopicMessage, get_session
from .._truth.jsonl_log import (
    KG_EVENTS_FILE,
    REDACTED_PAYLOAD_KEY,
    _fsync_dir,
    _hold_reindex_lock,
    _iter_jsonl,
    _json_default,
    _shard_depth,
    _thread_file,
    _truth_write_lock,
    is_redacted_payload,
    log_dir,
    reset_handles,
)
from .._truth.layout import require_current_format

logger = logging.getLogger(__name__)

REDACTIONS_FILE = "redactions.jsonl"
KEYRING_FILE = "keyring.json"
TOPIC_MESSAGES_FILE = "topic_messages.jsonl"
QUOTE_PLACEHOLDER = "[redacted]"

# Thread-meta fields that are routinely *derived from* message content (the
# auto-title is the first user message; summaries paraphrase or quote it), so a
# redaction must consider them carriers of the redacted content.
_THREAD_META_FIELDS = ("title", "summary", "description", "search_description", "indexed_summary")


def _norm(text: str) -> str:
    """Containment-comparison form: lowercased, whitespace collapsed, a trailing
    truncation ellipsis dropped (auto-titles are truncated prefixes)."""
    t = " ".join(text.split()).lower()
    return t.rstrip(".").rstrip("…").strip()


def _leaked_meta_fields(meta: dict, blob_norm: str, *, whole_thread: bool) -> dict[str, str]:
    """The thread-meta fields a redaction must scrub: on a whole-thread redaction
    every non-empty field (the fields ARE derived conversation content); on a
    partial one, fields whose text appears inside the redacted payloads."""
    out: dict[str, str] = {}
    for field in _THREAD_META_FIELDS:
        val = meta.get(field)
        if not isinstance(val, str) or not val.strip():
            continue
        if whole_thread or (_norm(val) and _norm(val) in blob_norm):
            out[field] = val
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _marker(key_id: str, at: str) -> dict:
    return {REDACTED_PAYLOAD_KEY: {"key_id": key_id, "at": at}}


def _marker_key_id(payload: object) -> str | None:
    if not is_redacted_payload(payload):
        return None
    env = payload[REDACTED_PAYLOAD_KEY]  # type: ignore[index]
    return env.get("key_id") if isinstance(env, dict) else None


# ── keyring ──────────────────────────────────────────────────────────────────
def _keyring_path() -> Path:
    return resolve_paths().home / KEYRING_FILE


def _load_keyring() -> dict:
    p = _keyring_path()
    if not p.exists():
        return {"version": 1, "keys": {}}
    kr = json.loads(p.read_text(encoding="utf-8"))
    kr.setdefault("keys", {})
    return kr


def _save_keyring(kr: dict) -> None:
    """Atomic replace at mode 0600 — the keyring is the one file whose loss (or
    exposure) changes what redaction means, so it gets the manifest's durability
    and tighter permissions."""
    p = _keyring_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(kr, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
    os.chmod(p, 0o600)
    _fsync_dir(p.parent)


# ── redactions log ───────────────────────────────────────────────────────────
def _redactions_path(d: Path) -> Path:
    return d / REDACTIONS_FILE


def _append_redaction_record(d: Path, rec: dict) -> None:
    p = _redactions_path(d)
    is_new = not p.exists()
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=_json_default))
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    if is_new:
        _fsync_dir(d)


def load_redactions(d: Path | None = None) -> list[dict]:
    """Every record in the redactions log, in append order."""
    return list(_iter_jsonl(_redactions_path(d or log_dir())))


def redaction_statuses() -> list[dict]:
    """One row per redaction: id, scope, and lifecycle state.

    ``status`` is ``active`` or ``unredacted`` (from the log); ``key`` is
    ``present`` or ``absent`` (from the keyring — absent on an active redaction
    means escrowed or forgotten; the keyring cannot tell which)."""
    keys = _load_keyring()["keys"]
    rows: dict[str, dict] = {}
    for rec in load_redactions():
        kid = rec.get("key_id")
        if not kid:
            continue
        if rec.get("type") == "redaction":
            rows[kid] = {
                "key_id": kid,
                "thread_id": rec.get("thread_id"),
                "event_ids": rec.get("event_ids", []),
                "reason": rec.get("reason"),
                "redacted_at": rec.get("redacted_at"),
                "status": "active",
            }
        elif rec.get("type") == "unredaction" and kid in rows:
            rows[kid]["status"] = "unredacted"
    for row in rows.values():
        row["key"] = "present" if row["key_id"] in keys else "absent"
    return list(rows.values())


# ── crypto ───────────────────────────────────────────────────────────────────
def _encrypt_bundle(key: bytes, key_id: str, bundle: dict) -> tuple[str, str]:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(
        nonce,
        json.dumps(bundle, ensure_ascii=False, default=_json_default).encode("utf-8"),
        key_id.encode("utf-8"),
    )
    return base64.b64encode(nonce).decode(), base64.b64encode(ct).decode()


def _decrypt_bundle(key: bytes, key_id: str, rec: dict) -> dict:
    nonce = base64.b64decode(rec["nonce"])
    ct = base64.b64decode(rec["ciphertext"])
    plain = AESGCM(key).decrypt(nonce, ct, key_id.encode("utf-8"))
    return json.loads(plain.decode("utf-8"))


# ── truth rewrites ───────────────────────────────────────────────────────────
def _rewrite_jsonl(path: Path, transform) -> int:
    """Atomically rewrite ``path``: ``transform(rec) -> dict | None`` (None keeps
    the line byte-exact — unparseable lines always pass through untouched; they
    are repair's jurisdiction, not redaction's). Returns lines rewritten."""
    tmp = path.with_name(path.name + ".redact")
    changed = 0
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        for raw in src:
            out = None
            line = raw.strip()
            if line:
                try:
                    rec = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    rec = None
                if isinstance(rec, dict):
                    out = transform(rec)
            if out is None:
                dst.write(raw if raw.endswith(b"\n") else raw + b"\n")
            else:
                changed += 1
                dst.write(
                    json.dumps(out, ensure_ascii=False, default=_json_default).encode("utf-8")
                    + b"\n"
                )
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)
    return changed


def _rewrite_thread_truth(
    path: Path, payload_by_id: dict[int, dict], meta_fields: dict[str, str]
) -> int:
    """Set the payload of EVERY line carrying a target event id — superseded
    duplicates included, since latest-wins loading hides but does not remove
    them — and scrub ``meta_fields`` (field → replacement) on every thread
    metadata line, superseded ones included (older titles/summaries derive from
    the same content)."""

    def tx(rec: dict):
        kind = rec.get("type", "event")
        if kind == "thread" and meta_fields:
            hit = False
            for field, replacement in meta_fields.items():
                val = rec.get(field)
                if isinstance(val, str) and val.strip() and val != replacement:
                    rec[field] = replacement
                    hit = True
            return rec if hit else None
        if kind != "event" or rec.get("id") is None:
            return None
        new = payload_by_id.get(int(rec["id"]))
        if new is None:
            return None
        rec["payload"] = new
        return rec

    return _rewrite_jsonl(path, tx)


def _rewrite_kg_quotes(path: Path, quote_by_id: dict[int, str]) -> int:
    def tx(rec: dict):
        if rec.get("id") is None or int(rec["id"]) not in quote_by_id:
            return None
        payload = rec.get("payload")
        if not isinstance(payload, dict) or "quote" not in payload:
            return None
        payload["quote"] = quote_by_id[int(rec["id"])]
        return rec

    return _rewrite_jsonl(path, tx)


def _rewrite_tm_quotes(path: Path, quote_by_id: dict[int, str]) -> int:
    def tx(rec: dict):
        if rec.get("id") is None or int(rec["id"]) not in quote_by_id:
            return None
        rec["quote"] = quote_by_id[int(rec["id"])]
        return rec

    return _rewrite_jsonl(path, tx)


# ── index updates ────────────────────────────────────────────────────────────
def _ids_clause(ids: list[int]) -> str:
    return "(" + ",".join(str(int(i)) for i in ids) + ")"


def _table_exists(s, name: str) -> bool:
    return bool(
        s.execute(sa_text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": name}).scalar()
    )


def _purge_search_docs(s, event_ids: list[int]) -> None:
    """Drop the events' search docs from the FTS shadow; the sync triggers cascade
    the deletes into the ``event_search`` index. Thread-meta docs (title/summary,
    anchored to a real event id) are content of the thread, not of the event —
    they survive."""
    ids = _ids_clause(event_ids)
    s.execute(sa_text(
        f"DELETE FROM events_fts WHERE event_id IN {ids} AND event_type != 'thread_meta'"
    ))


def _purge_vectors(s, d: Path, event_ids: list[int]) -> None:
    """Delete the events' embeddings from the live table and the sidecar cache;
    both restore paths (``INSERT OR IGNORE`` from the sidecar, the embed cohost)
    would otherwise resurrect a vector of the redacted content."""
    ids = _ids_clause(event_ids)
    if _table_exists(s, "event_vectors"):
        s.execute(sa_text(f"DELETE FROM event_vectors WHERE event_id IN {ids}"))
    side = d / "vectors.sqlite"
    if side.exists():
        con = sqlite3.connect(side)
        try:
            has = con.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'event_vectors'"
            ).fetchone()
            if has:
                con.execute("PRAGMA secure_delete=ON")
                con.execute(f"DELETE FROM event_vectors WHERE event_id IN {ids}")
                con.commit()
        finally:
            con.close()
    from .._retrieval.vectors import _bump_version

    _bump_version()  # a long-lived process's matrix cache must not serve the dead rows


# ── redact ───────────────────────────────────────────────────────────────────
def redact_events(thread_id: str, event_ids: list[int] | None = None, *, reason: str | None = None) -> dict:
    """Redact ``event_ids`` of ``thread_id`` (default: every event in the thread).

    Runs under the exclusive reindex lock — writers are quiescent, same
    discipline as repair. Returns counts plus the new ``key_id`` and the
    operator notes (source-store plaintext, backup generations, title/summary)."""
    require_current_format()
    with _hold_reindex_lock():
        # Resolve any crashed drain first (its rollback trims a partial batch this
        # scan would otherwise read as content).
        with _truth_write_lock():
            pass
        return _redact_locked(str(thread_id), event_ids, reason)


def _redact_locked(thread_id: str, event_ids: list[int] | None, reason: str | None) -> dict:
    d = log_dir()
    path = _thread_file(d, thread_id, _shard_depth(d))
    if not path.exists():
        raise ValueError(f"thread {thread_id} has no truth file ({path})")

    latest: dict[int, dict] = {}
    for rec in _iter_jsonl(path, log=False):
        if rec.get("type", "event") == "event" and rec.get("id") is not None:
            latest[int(rec["id"])] = rec.get("payload") or {}

    targets = sorted({int(e) for e in event_ids}) if event_ids else sorted(latest)
    missing = [e for e in targets if e not in latest]
    if missing:
        raise ValueError(f"events not in thread {thread_id}'s truth: {missing}")
    targets = [e for e in targets if not is_redacted_payload(latest[e])]
    if not targets:
        return {"thread_id": thread_id, "events_redacted": 0, "notes": ["nothing to do: already redacted"]}

    # Citation quotes that carry the events' content into other files/tables.
    ids = _ids_clause(targets)
    with get_session() as s:
        tm_quotes = {
            int(r[0]): r[1]
            for r in s.execute(sa_text(
                f"SELECT id, quote FROM topic_messages WHERE event_id IN {ids} AND quote IS NOT NULL"
            ))
        }
        kg_quotes = {
            int(r[0]): r[1]
            for r in s.execute(sa_text(
                "SELECT id, json_extract(payload, '$.quote') FROM kg_events "
                f"WHERE json_extract(payload, '$.event_id') IN {ids} "
                "AND json_extract(payload, '$.quote') IS NOT NULL"
            ))
        }
        t = s.get(Thread, thread_id)
        source_note = (
            f"the original provider store still holds this content (source={t.source!r}, "
            f"source_id={t.source_id!r})" if t is not None and t.source else
            "the original provider store (if any) still holds this content"
        )
        # Thread meta (auto-titles, summaries) is routinely derived from message
        # content — the most common leak. Scrub whichever fields carry it.
        blob_norm = _norm(json.dumps([latest[e] for e in targets], ensure_ascii=False, default=_json_default))
        meta = {f: getattr(t, f) for f in _THREAD_META_FIELDS} if t is not None else {}
        meta_scrub = _leaked_meta_fields(meta, blob_norm, whole_thread=event_ids is None)
        has_meta = bool(
            not meta_scrub
            and t is not None
            and ((t.title or "").strip() or (t.summary or "").strip())
        )

    # Blob-extracted payloads (see _truth.blobs) enter the bundle *reconstituted* —
    # inline base64 restored from the blob files — so the bundle is self-contained:
    # unredact restores full content even after the blob files are shredded below.
    # A ref whose blob file is already gone stays a ref (noted in the result).
    from .._truth.blobs import blob_file, collect_blob_hashes, reconstitute_blobs

    target_blob_hashes: set[str] = set()
    bundle_events: dict[str, dict] = {}
    blobs_unrecoverable = 0
    for e in targets:
        target_blob_hashes |= collect_blob_hashes(latest[e])
        restored_payload, blob_missing = reconstitute_blobs(latest[e])
        blobs_unrecoverable += blob_missing
        bundle_events[str(e)] = restored_payload

    bundle = {
        "format": 1,
        "thread_id": thread_id,
        "events": bundle_events,
        "topic_message_quotes": {str(i): q for i, q in tm_quotes.items()},
        "kg_event_quotes": {str(i): q for i, q in kg_quotes.items()},
        "thread_meta": meta_scrub,
    }

    # Key + record first, fsynced: from here every failure point is recoverable —
    # either the plaintext is still on disk (re-run) or the marker state is complete.
    now = _now_iso()
    key_id = secrets.token_hex(8)
    key = AESGCM.generate_key(bit_length=256)
    nonce_b64, ct_b64 = _encrypt_bundle(key, key_id, bundle)
    kr = _load_keyring()
    kr["keys"][key_id] = {"key": base64.b64encode(key).decode(), "created_at": now}
    _save_keyring(kr)
    _append_redaction_record(d, {
        "type": "redaction", "key_id": key_id, "thread_id": thread_id,
        "event_ids": targets, "reason": reason, "redacted_at": now,
        "alg": "AES-256-GCM", "nonce": nonce_b64, "ciphertext": ct_b64,
    })

    # Truth rewrites. Cached appenders must not span an atomic replace.
    reset_handles()
    marker = _marker(key_id, now)
    lines = _rewrite_thread_truth(
        path, {e: marker for e in targets}, {f: QUOTE_PLACEHOLDER for f in meta_scrub}
    )
    if kg_quotes:
        _rewrite_kg_quotes(d / KG_EVENTS_FILE, {i: QUOTE_PLACEHOLDER for i in kg_quotes})
    if tm_quotes and (d / TOPIC_MESSAGES_FILE).exists():
        _rewrite_tm_quotes(d / TOPIC_MESSAGES_FILE, {i: QUOTE_PLACEHOLDER for i in tm_quotes})

    # Index catch-up (plain session, nothing staged — the drain stays silent).
    # A crash before this commit is converged by the next `archive reindex`.
    with get_session() as s:
        # Without secure_delete the old row images survive in index.db's free
        # pages — recoverable plaintext in a file this operation claims to scrub.
        s.execute(sa_text("PRAGMA secure_delete=ON"))
        for eid in targets:
            ev = s.get(Event, eid)
            if ev is not None and str(ev.thread_id) == thread_id:
                ev.payload = dict(marker)
        for tm_id in tm_quotes:
            tm = s.get(TopicMessage, tm_id)
            if tm is not None:
                tm.quote = QUOTE_PLACEHOLDER
        for kg_id in kg_quotes:
            kg = s.get(KgEvent, kg_id)
            if kg is not None and isinstance(kg.payload, dict):
                kg.payload = {**kg.payload, "quote": QUOTE_PLACEHOLDER}
        if meta_scrub:
            t = s.get(Thread, thread_id)
            if t is not None:
                for field in meta_scrub:
                    setattr(t, field, QUOTE_PLACEHOLDER)
            s.flush()
            from .._retrieval.fts import index_thread_meta

            # Diff-based sync: replaces the title/summary search docs (and drops
            # their stale vectors) now that the fields read "[redacted]".
            index_thread_meta(s, [thread_id])
        _purge_search_docs(s, targets)
        _purge_vectors(s, d, targets)
        s.commit()
        # The zeroed pages sit in the WAL until a checkpoint publishes them;
        # truncate so the plaintext row images don't outlive the redaction in
        # index.db-wal. Best-effort — a concurrent reader makes it partial, and
        # the next natural checkpoint finishes the job.
        s.connection().connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # Blob files carrying the targets' binary content (extracted refs, and any
    # lazily-materialized copy of inline base64 — collect_blob_hashes covers
    # both). Content-addressed files are shared across events and threads, so a
    # file is deleted only when no live event still references its hash; an
    # event that still holds the same content *inline* keeps its content either
    # way (the file would just be re-materialized from it on the next read).
    blobs_shredded: list[str] = []
    if target_blob_hashes:
        with get_session() as s:
            for h in sorted(target_blob_hashes):
                still_referenced = s.execute(
                    sa_text("SELECT 1 FROM events WHERE payload LIKE :pat LIMIT 1"),
                    {"pat": f"%{h}%"},
                ).first()
                if still_referenced is not None:
                    continue
                bp = blob_file(h)
                if bp is not None:
                    bp.unlink(missing_ok=True)
                    blobs_shredded.append(h)

    logger.warning(
        "redact: thread %s — %d event(s) redacted under key %s (%d truth line(s), "
        "%d topic quote(s), %d kg quote(s))",
        thread_id, len(targets), key_id, lines, len(tm_quotes), len(kg_quotes),
    )
    notes = [
        source_note,
        "existing backups (and their .generations) still hold the plaintext until re-mirrored/pruned",
        "the next `archive backup` may need --allow-shrink (the rewrite shrank truth files)",
    ]
    if meta_scrub:
        notes.append(f"thread meta scrubbed (derived from the content): {', '.join(sorted(meta_scrub))}")
    elif has_meta:
        notes.append("the thread's title/summary were kept (no overlap detected) — check them if they paraphrase the content")
    if blobs_unrecoverable:
        notes.append(
            f"{blobs_unrecoverable} blob ref(s) had no blob file to bundle — that binary "
            "content was already gone and cannot be restored by unredact"
        )
    return {
        "thread_id": thread_id, "key_id": key_id, "events_redacted": len(targets),
        "truth_lines_rewritten": lines, "topic_quotes_scrubbed": len(tm_quotes),
        "kg_quotes_scrubbed": len(kg_quotes), "thread_meta_scrubbed": sorted(meta_scrub),
        "blobs_shredded": len(blobs_shredded),
        "notes": notes,
    }


# ── unredact ─────────────────────────────────────────────────────────────────
def unredact(key_id: str) -> dict:
    """Restore a redaction's content from its encrypted bundle. Requires the key
    to be present in the keyring (``--restore-key`` first if it was escrowed)."""
    require_current_format()
    with _hold_reindex_lock():
        with _truth_write_lock():
            pass
        return _unredact_locked(key_id)


def _unredact_locked(key_id: str) -> dict:
    d = log_dir()
    rec = next(
        (r for r in load_redactions(d) if r.get("type") == "redaction" and r.get("key_id") == key_id),
        None,
    )
    if rec is None:
        raise ValueError(f"no redaction record for key id {key_id!r}")
    status = next((r for r in redaction_statuses() if r["key_id"] == key_id), None)
    if status is not None and status["status"] == "unredacted":
        raise ValueError(f"redaction {key_id} is already unredacted")
    entry = _load_keyring()["keys"].get(key_id)
    if entry is None:
        raise ValueError(
            f"key {key_id} is not in the keyring (escrowed or forgotten) — "
            "restore it with `archive redact --restore-key` first"
        )
    bundle = _decrypt_bundle(base64.b64decode(entry["key"]), key_id, rec)

    thread_id = str(bundle["thread_id"])
    payloads = {int(e): p for e, p in bundle.get("events", {}).items()}
    tm_quotes = {int(i): q for i, q in bundle.get("topic_message_quotes", {}).items()}
    kg_quotes = {int(i): q for i, q in bundle.get("kg_event_quotes", {}).items()}
    thread_meta = bundle.get("thread_meta", {})

    path = _thread_file(d, thread_id, _shard_depth(d))
    reset_handles()

    # Restore only what this key redacted — an event since re-redacted under a
    # newer key keeps its newer marker; a meta field since re-scrubbed or edited
    # keeps its current value (only the placeholder is replaced).
    def tx(line_rec: dict):
        kind = line_rec.get("type", "event")
        if kind == "thread" and thread_meta:
            hit = False
            for field, original in thread_meta.items():
                if line_rec.get(field) == QUOTE_PLACEHOLDER:
                    line_rec[field] = original
                    hit = True
            return line_rec if hit else None
        if kind != "event" or line_rec.get("id") is None:
            return None
        eid = int(line_rec["id"])
        if eid not in payloads or _marker_key_id(line_rec.get("payload")) != key_id:
            return None
        line_rec["payload"] = payloads[eid]
        return line_rec

    lines = _rewrite_jsonl(path, tx) if path.exists() else 0
    if kg_quotes and (d / KG_EVENTS_FILE).exists():
        _rewrite_kg_quotes(d / KG_EVENTS_FILE, kg_quotes)
    if tm_quotes and (d / TOPIC_MESSAGES_FILE).exists():
        _rewrite_tm_quotes(d / TOPIC_MESSAGES_FILE, tm_quotes)

    restored: list[int] = []
    with get_session() as s:
        events = []
        for eid, payload in payloads.items():
            ev = s.get(Event, eid)
            if ev is not None and _marker_key_id(ev.payload) == key_id:
                ev.payload = payload
                events.append(ev)
                restored.append(eid)
        for tm_id, quote in tm_quotes.items():
            tm = s.get(TopicMessage, tm_id)
            if tm is not None and tm.quote == QUOTE_PLACEHOLDER:
                tm.quote = quote
        for kg_id, quote in kg_quotes.items():
            kg = s.get(KgEvent, kg_id)
            if kg is not None and isinstance(kg.payload, dict) and kg.payload.get("quote") == QUOTE_PLACEHOLDER:
                kg.payload = {**kg.payload, "quote": quote}
        if thread_meta:
            t = s.get(Thread, thread_id)
            if t is not None:
                for field, original in thread_meta.items():
                    if getattr(t, field) == QUOTE_PLACEHOLDER:
                        setattr(t, field, original)
            s.flush()
            from .._retrieval.fts import index_thread_meta

            index_thread_meta(s, [thread_id])
        if restored:
            # Defensive: a crashed earlier attempt may have left docs behind.
            _purge_search_docs(s, restored)
            from .._retrieval.fts import index_events

            index_events(s, events)
        s.commit()

    _append_redaction_record(d, {"type": "unredaction", "key_id": key_id, "at": _now_iso()})
    logger.warning(
        "unredact: thread %s — %d event(s) restored under key %s", thread_id, len(restored), key_id
    )
    return {
        "thread_id": thread_id, "key_id": key_id, "events_restored": len(restored),
        "truth_lines_rewritten": lines,
        "notes": ["embeddings for the restored events regrow on the next embed pass"],
    }


# ── key lifecycle ────────────────────────────────────────────────────────────
def show_key(key_id: str) -> str:
    """The base64 key material, for escrow (a password manager, paper, anywhere
    off this machine)."""
    entry = _load_keyring()["keys"].get(key_id)
    if entry is None:
        raise ValueError(f"key {key_id} is not in the keyring")
    return entry["key"]


def forget_key(key_id: str) -> dict:
    """Remove a key from the keyring. If it was never escrowed this is
    crypto-erasure: the redaction's ciphertext is permanently unreadable."""
    kr = _load_keyring()
    if key_id not in kr["keys"]:
        raise ValueError(f"key {key_id} is not in the keyring")
    del kr["keys"][key_id]
    _save_keyring(kr)
    logger.warning("redact: key %s removed from the keyring", key_id)
    return {"key_id": key_id, "forgotten": True}


def restore_key(key_id: str, key_b64: str) -> dict:
    """Put an escrowed key back so ``unredact`` can use it. Validated against the
    redaction record's ciphertext before it is accepted."""
    rec = next(
        (r for r in load_redactions() if r.get("type") == "redaction" and r.get("key_id") == key_id),
        None,
    )
    if rec is None:
        raise ValueError(f"no redaction record for key id {key_id!r}")
    try:
        _decrypt_bundle(base64.b64decode(key_b64), key_id, rec)
    except Exception as exc:
        raise ValueError(f"key does not decrypt redaction {key_id}: wrong key material") from exc
    kr = _load_keyring()
    kr["keys"][key_id] = {"key": key_b64, "created_at": _now_iso(), "restored": True}
    _save_keyring(kr)
    return {"key_id": key_id, "restored": True}
