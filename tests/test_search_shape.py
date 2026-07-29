"""Result shape: every matching message is its own row.

A search is not grouped, folded, or collapsed by thread. A conversation matching
eight times returns eight rows, each with its own snippet and context, and
``limit`` counts messages. These pin that, and the one fold that remains —
which is deduplication rather than grouping:

- ``rank.collapse_same_anchor`` folds rows sharing one (thread_id, event_id): a
  thread-meta title doc and the event it anchors to are one message, and
  two rows that would open identically in ``thread_read``
- ``search`` returns a row per match under every scope — a ``thread_id`` scope,
  the structural shapes, and the ordinary ranked query alike
- near-identical text in different threads (a fork, a fleet of agents carrying
  one prompt) is not redundancy to remove: those are different conversations
- ``format_results`` counts rows in messages, and names the conversations they
  came from
"""

from __future__ import annotations

import json

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import search
from thread_archive._retrieval.format import format_results
from thread_archive._retrieval.rank import collapse_same_anchor
from thread_archive._store import init_db


def _hit(eid, tid, content, ct="user"):
    return {
        "event_id": eid, "thread_id": tid, "thread_title": f"t{tid}",
        "event_type": "message", "content_type": ct,
        "snippet": content, "full_content": content, "occurred_at": None,
    }


def _write_cc(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _cc_user(uid, text, day, hhmm="10:00"):
    return {"type": "user", "uuid": uid, "timestamp": f"2026-01-0{day}T{hhmm}:00Z",
            "sessionId": "s", "message": {"role": "user", "content": text}}


def _seed(archive_home):
    """Thread A: three matching user turns; thread B: one; threads C+D: the same
    prompt verbatim (a fork / fleet copy)."""
    init_db()
    a = archive_home / "a.jsonl"
    _write_cc(a, [_cc_user("a1", "the tachyon condenser leaks", 1),
                  _cc_user("a2", "tachyon flow reversed after the patch", 1, "11:00"),
                  _cc_user("a3", "third tachyon reading is stable", 1, "12:00")])
    import_session_incremental(a, "proj:a")

    b = archive_home / "b.jsonl"
    _write_cc(b, [_cc_user("b1", "does the tachyon budget cover q3", 2)])
    import_session_incremental(b, "proj:b")

    c = archive_home / "c.jsonl"
    _write_cc(c, [_cc_user("c1", "calibrate the flux capacitor against drift", 3)])
    import_session_incremental(c, "proj:c")

    d = archive_home / "d.jsonl"
    _write_cc(d, [_cc_user("d1", "calibrate the flux capacitor against drift", 4)])
    import_session_incremental(d, "proj:d")


def test_collapse_same_anchor_keeps_better_placed_row() -> None:
    """The one surviving fold: two rows on one (thread_id, event_id) are one
    message, and would open identically in thread_read."""
    hits = [_hit(10, 1, "Tachyon condenser notes", ct="title"),
            _hit(10, 1, "the tachyon condenser leaks"),
            _hit(11, 1, "another event")]
    out = collapse_same_anchor(hits)
    assert [(h["event_id"], h["content_type"]) for h in out] == [(10, "title"), (11, "user")]


def test_every_match_gets_its_own_row(archive_home) -> None:
    """Thread A matches three times and gets three rows. A fold to one-row-per-
    thread would return two rows here and drop two of the four real matches — the
    hits a reader searched for."""
    _seed(archive_home)
    rows = search("tachyon")
    assert len(rows) == 4
    per_thread = {}
    for r in rows:
        per_thread.setdefault(r["thread_id"], []).append(r)
    assert sorted(len(v) for v in per_thread.values()) == [1, 3]


def test_each_row_carries_its_own_match(archive_home) -> None:
    """Three rows from one thread are three *different* messages, not one message
    repeated — which is the whole point of not folding them."""
    _seed(archive_home)
    rows = [r for r in search("tachyon") if r["thread_id"] == search("tachyon")[0]["thread_id"]]
    texts = {r["full_content"] for r in rows}
    assert len(texts) == len(rows)


def test_forked_threads_each_keep_a_row(archive_home) -> None:
    """A forked prompt produces two threads carrying identical text. They are two
    conversations that went on to do different work, so both keep a row: removing
    one answers "which threads mention this" with 1 when it is 2."""
    _seed(archive_home)
    rows = search("flux capacitor")
    assert len({r["thread_id"] for r in rows}) == 2


def test_a_thread_scope_returns_every_hit(archive_home) -> None:
    _seed(archive_home)
    tid = search("tachyon")[0]["thread_id"]
    scoped = search("tachyon", thread_id=tid)
    assert len(scoped) == 3


def test_the_structural_shapes_return_every_hit(archive_home) -> None:
    _seed(archive_home)
    assert len(search("tachyon", sort="oldest")) == 4
    assert len(search("tachyon", output="count")) == 4


def test_limit_counts_messages(archive_home) -> None:
    """Not threads: a limit of 2 is two matching messages, which may well be two
    matches from one conversation."""
    _seed(archive_home)
    assert len(search("tachyon", limit=2)) == 2


def test_format_counts_rows_in_messages_and_names_the_conversations(archive_home) -> None:
    """A header reading "2 thread(s)" over four matches counts the right number in
    the wrong unit, which sends a caller looking for hits that are already on the
    page. Both numbers are reported because they answer different questions."""
    _seed(archive_home)
    rendered = format_results(search("tachyon"), "tachyon")
    assert '4 result(s) in 2 thread(s) for "tachyon"' in rendered
    assert "the tachyon condenser leaks" in rendered


def test_format_carries_no_fold_annotations(archive_home) -> None:
    """Nothing is folded, so nothing renders a fold note — a "+N more in thread"
    line would promise hits that are already rows of their own."""
    _seed(archive_home)
    rendered = format_results(search("tachyon"), "tachyon")
    assert "more in thread" not in rendered
    assert "same content in thread" not in rendered
    assert "grouped" not in rendered
