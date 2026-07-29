"""A Cursor scan must cost what moved, not what the store holds.

``state.vscdb`` is the editor's whole key-value store — hundreds of megabytes, the
message bodies (bubbles) being the bulk of it — and Cursor writes to it constantly,
so the watcher's mtime fingerprint advances many times an hour whether or not a
conversation did. A scan that reads every bubble to discover nothing changed makes
the idle case cost the size of the archive's largest source, on every pass, forever.

The gate is the composer blob's ``lastUpdatedAt`` against the import watermark:
known before any bubble is read, and on the common pass it clears every composer.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from sqlalchemy import select

from thread_archive._importers import import_cursor_db
from thread_archive._importers.cursor import _cursor_stale_composers, _prefix_range
from thread_archive._store import Event, get_session, init_db

# The watermark is the wall clock of the last import, compared against the provider's
# own ``lastUpdatedAt``. A composer only reads as moved when its stamp is genuinely
# later than that import — so a fixture stamp has to be real time, not an epoch
# constant, or every composer looks permanently up to date.
_UPDATED_AT = int(time.time() * 1000)


def _composer(name: str = "Scan Cost Chat", *, updated_at: int = _UPDATED_AT) -> dict:
    return {
        "name": name,
        "lastUpdatedAt": updated_at,
        "fullConversationHeadersOnly": [
            {"bubbleId": "b1", "type": 1},
            {"bubbleId": "b2", "type": 2},
        ],
    }


def _write_db(path, cid: str, composer: dict) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)", [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "the question", "createdAt": _UPDATED_AT})),
        (f"bubbleId:{cid}:b2", json.dumps({"type": 2, "text": "the answer", "createdAt": _UPDATED_AT})),
    ])
    conn.commit()
    conn.close()


def _blank_the_bubbles(path, cid: str) -> None:
    """Make every bubble unparseable, leaving the composer blob untouched.

    A NULL value raises out of ``json.loads``, which the bubble loop catches and
    *logs* — so the log is a truthful record of whether the bubbles were read at all,
    without reaching inside the importer to watch it."""
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE cursorDiskKV SET value = NULL WHERE key >= ? AND key < ?",
        _prefix_range(f"bubbleId:{cid}:"),
    )
    conn.commit()
    conn.close()


def _event_count() -> int:
    with get_session() as s:
        return len(list(s.execute(select(Event)).scalars()))


def test_unchanged_composer_is_not_read_off_disk(archive_home, caplog) -> None:
    """The scan that finds nothing to do must not touch a single message body."""
    init_db()
    db = archive_home / "state.vscdb"
    cid = "comp_idle"
    _write_db(db, cid, _composer())

    assert import_cursor_db(db).events_created > 0
    imported = _event_count()

    # Nothing about the conversation changed; only the bubbles' readability did.
    _blank_the_bubbles(db, cid)

    with caplog.at_level(logging.WARNING, logger="thread_archive._importers.cursor"):
        scan = import_cursor_db(db)

    assert "failed to parse" not in caplog.text, (
        "the scan read message bodies for a composer that had not moved"
    )
    assert scan.processed == 1, "an unread composer must still count as checked"
    assert scan.events_created == 0
    assert _event_count() == imported


def test_moved_composer_is_still_read_and_imported(archive_home) -> None:
    """The gate must open the moment the conversation actually advances."""
    init_db()
    db = archive_home / "state.vscdb"
    cid = "comp_active"
    _write_db(db, cid, _composer())

    assert import_cursor_db(db).events_created > 0
    before = _event_count()

    moved_at = int(time.time() * 1000) + 60_000
    composer = _composer(updated_at=moved_at)
    composer["fullConversationHeadersOnly"].append({"bubbleId": "b3", "type": 1})
    conn = sqlite3.connect(db)
    conn.executemany("INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)", [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b3", json.dumps(
            {"type": 1, "text": "a later follow-up question", "createdAt": moved_at}
        )),
    ])
    conn.commit()
    conn.close()

    scan = import_cursor_db(db)

    assert scan.events_created > 0
    assert _event_count() > before


def test_composer_with_no_watermark_is_stale(archive_home) -> None:
    """A store that has never seen a composer must treat it as work to do — the
    optimization may only ever skip conversations already held."""
    init_db()
    composers = {"never_seen": _composer()}

    assert _cursor_stale_composers(composers) == {"never_seen"}


def test_bubbles_of_one_composer_do_not_leak_into_another(archive_home) -> None:
    """The per-composer bubble range must not spill across the key boundary: two
    composers whose ids share a prefix are still two conversations."""
    init_db()
    db = archive_home / "state.vscdb"
    _write_db(db, "comp", _composer("Short Id"))
    _write_db(db, "comp_longer", _composer("Longer Id"))

    import_cursor_db(db)

    with get_session() as s:
        asked = [
            e.payload.get("content")
            for e in s.execute(select(Event)).scalars()
            if e.event_type == "user_message_sent"
        ]
    # Each composer contributes its own turn; neither absorbed the other's bubbles.
    assert asked == ["the question", "the question"], asked
