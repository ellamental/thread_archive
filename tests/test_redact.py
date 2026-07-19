"""Redaction: crypto-shredding content out of the archive without deleting history.

The contract under test: after ``redact``, the plaintext exists NOWHERE under the
archive home except inside the AES-GCM ciphertext on the redaction record — not
in the truth file, not in ``index.db`` (live rows or free pages), not in the FTS
surface, not in citation quotes — while the truth log keeps its shape (event
ids, dedup keys, verify green). ``unredact`` restores losslessly while the key
is held; forgetting the key makes the ciphertext permanently dead.
"""

from __future__ import annotations

import stat

import pytest
from sqlalchemy import text

from tests.helpers import cc_assistant, cc_user, write_jsonl
from thread_archive import _api as ta
from thread_archive._ops import redact as rd
from thread_archive._store import get_session
from thread_archive._truth.jsonl_log import _iter_jsonl, is_redacted_payload

SECRET = "xyzzy-hunter2-4a6772f0-super-secret"


def _import_secret_session(tmp_path, name: str = "sess"):
    """One-turn session whose user message carries SECRET; returns
    ``(source_file, user_event_id, thread_id)``."""
    f = tmp_path / f"{name}.jsonl"
    write_jsonl(f, [cc_user(name, content=f"my api key is {SECRET}"), cc_assistant(name)])
    ta.import_path(f)
    with get_session() as s:
        row = s.execute(text(
            "SELECT id, thread_id FROM events WHERE event_type = 'user_message_sent'"
        )).first()
    return f, int(row[0]), row[1]


def _files_holding(home, needle: bytes) -> list:
    return [
        p for p in home.rglob("*")
        if p.is_file() and needle in p.read_bytes()
    ]


def _fts_hits(needle: str) -> int:
    with get_session() as s:
        return s.execute(
            text("SELECT count(*) FROM events_fts WHERE content LIKE :n"),
            {"n": f"%{needle}%"},
        ).scalar()


def _event_payload(event_id: int):
    with get_session() as s:
        ev = s.execute(
            text("SELECT payload FROM events WHERE id = :e"), {"e": event_id}
        ).scalar()
    import json

    return json.loads(ev) if isinstance(ev, str) else ev


def test_redact_scrubs_everywhere(tmp_path, archive_home):
    _, eid, tid = _import_secret_session(tmp_path)
    assert _fts_hits(SECRET) > 0  # baseline: the content was searchable

    res = ta.redact(tid, [eid], reason="test secret")

    assert res["events_redacted"] == 1
    kid = res["key_id"]
    # Nowhere under the home holds the plaintext — truth, index.db (secure_delete),
    # WAL, snapshots. The ciphertext on the record is the only surviving copy.
    assert _files_holding(archive_home, SECRET.encode()) == []
    assert is_redacted_payload(_event_payload(eid))
    assert _fts_hits(SECRET) == 0
    # The record and the key exist; the keyring is operator-only.
    recs = rd.load_redactions()
    assert [r["key_id"] for r in recs if r.get("type") == "redaction"] == [kid]
    keyring = archive_home / "keyring.json"
    assert keyring.exists()
    assert stat.S_IMODE(keyring.stat().st_mode) == 0o600
    # The reader shows a visible placeholder, never the content or a silent gap.
    text_out = ta.read_thread(tid)
    assert SECRET not in text_out
    assert "[redacted]" in text_out


def test_verify_green_and_reindex_preserves(tmp_path, archive_home):
    _, eid, tid = _import_secret_session(tmp_path)
    ta.redact(tid, [eid])

    from thread_archive._ops.verify import verify

    v = verify(deep=True)
    assert v["ok"], v
    # The index is a projection: rebuilding it from truth must keep the marker
    # and must not resurrect a single searchable byte.
    ta.reindex()
    assert is_redacted_payload(_event_payload(eid))
    assert _fts_hits(SECRET) == 0
    assert _files_holding(archive_home, SECRET.encode()) == []
    assert verify(deep=True)["ok"]


def test_unredact_restores_losslessly(tmp_path, archive_home):
    _, eid, tid = _import_secret_session(tmp_path)
    original = _event_payload(eid)
    res = ta.redact(tid, [eid])
    kid = res["key_id"]

    out = ta.unredact(kid)

    assert out["events_restored"] == 1
    assert _event_payload(eid) == original
    assert _fts_hits(SECRET) > 0  # searchable again
    tf = next((archive_home / "truth" / "threads").rglob(f"{tid}.jsonl"))
    payloads = [
        r["payload"] for r in _iter_jsonl(tf)
        if r.get("type", "event") == "event" and r.get("id") == eid
    ]
    assert payloads and payloads[-1] == original
    statuses = {r["key_id"]: r["status"] for r in rd.redaction_statuses()}
    assert statuses[kid] == "unredacted"
    with pytest.raises(ValueError, match="already unredacted"):
        ta.unredact(kid)


def test_escrow_forget_restore_roundtrip(tmp_path, archive_home):
    _, eid, tid = _import_secret_session(tmp_path)
    kid = ta.redact(tid, [eid])["key_id"]

    escrowed = ta.redact_show_key(kid)
    ta.redact_forget_key(kid)
    # Forgotten: the machine can no longer produce the plaintext.
    with pytest.raises(ValueError, match="not in the keyring"):
        ta.unredact(kid)
    assert escrowed not in (archive_home / "keyring.json").read_text()
    # Wrong key material is rejected outright.
    import base64

    with pytest.raises(ValueError, match="wrong key"):
        ta.redact_restore_key(kid, base64.b64encode(b"\x00" * 32).decode())
    # Escrow round-trip: restore the key, unredact works.
    ta.redact_restore_key(kid, escrowed)
    assert ta.unredact(kid)["events_restored"] == 1
    assert SECRET in str(_event_payload(eid))


def test_reimport_does_not_resurrect(tmp_path, archive_home):
    src, eid, tid = _import_secret_session(tmp_path)
    ta.redact(tid, [eid])
    with get_session() as s:
        before = s.execute(text("SELECT count(*) FROM events")).scalar()

    ta.import_path(src)  # the provider store still holds the plaintext

    with get_session() as s:
        after = s.execute(text("SELECT count(*) FROM events")).scalar()
    assert after == before  # dedup key on the marker row pins the identity
    assert is_redacted_payload(_event_payload(eid))
    assert _files_holding(archive_home, SECRET.encode()) == []


def test_whole_thread_redaction(tmp_path, archive_home):
    _, _, tid = _import_secret_session(tmp_path)

    res = ta.redact(tid)  # no event list: everything

    assert res["events_redacted"] >= 2  # user + assistant events
    with get_session() as s:
        rows = s.execute(
            text("SELECT payload FROM events WHERE thread_id = :t"), {"t": tid}
        ).scalars().all()
    import json

    assert all(is_redacted_payload(json.loads(p) if isinstance(p, str) else p) for p in rows)
    assert _files_holding(archive_home, SECRET.encode()) == []
    # Idempotent: a second pass finds nothing left to redact.
    assert ta.redact(tid)["events_redacted"] == 0


def test_citation_quotes_scrubbed_and_restored(tmp_path, archive_home):
    _, eid, tid = _import_secret_session(tmp_path)
    from thread_archive._knowledge import add_topic_evidence, create_topic

    topic = create_topic("Secrets", "test topic")["topic_id"]
    add_topic_evidence(topic, eid, tid, f"my api key is {SECRET}")

    res = ta.redact(tid, [eid])

    assert res["topic_quotes_scrubbed"] == 1
    assert res["kg_quotes_scrubbed"] == 1
    with get_session() as s:
        quote = s.execute(
            text("SELECT quote FROM topic_messages WHERE event_id = :e"), {"e": eid}
        ).scalar()
    assert quote == "[redacted]"
    assert _files_holding(archive_home, SECRET.encode()) == []

    ta.unredact(res["key_id"])
    with get_session() as s:
        quote = s.execute(
            text("SELECT quote FROM topic_messages WHERE event_id = :e"), {"e": eid}
        ).scalar()
    assert SECRET in quote


def test_marker_is_hash_exempt():
    from thread_archive._truth.rebuild import _hash_key_check

    marker = {"_redacted": {"key_id": "abc", "at": "2026-01-01T00:00:00Z"}}
    assert _hash_key_check(marker, "m-x:user_message_sent::" + "0" * 16) is None
    assert is_redacted_payload(marker)
    assert not is_redacted_payload({"content": "hi"})
    assert not is_redacted_payload("nope")
