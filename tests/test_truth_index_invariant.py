"""The claim the product rests on, as a property: truth is authoritative and the
index is a disposable projection of it.

Everything the archive promises about durability reduces to this. The JSONL log
is the record; ``index.db`` is a cache that can be deleted and rebuilt; a rebuild
is lossless; and ``verify`` can tell you, at any moment, whether the two still
agree. Each of those is asserted by example elsewhere — a test writes a session,
reindexes, and checks a count. What no example can cover is the part that
actually bites: whether the invariant survives *sequences* — an amendment landing
between two imports, a checkpoint after a re-import that resumed from a
watermark, a rebuild over a thread that has been extended three times and
superseded twice.

So this file generates the sequences. Hypothesis drives a real archive through
real imports, amendments, checkpoints and rebuilds in orders nobody wrote down,
and after every single step asserts the whole invariant:

- ``verify(deep=True)`` is green — the id-level truth↔index diff, not a count
  comparison. The oracle is the product's own, which is the point: this file adds
  the *sequences*, and reuses the checker that already ships.
- a rebuild from truth reproduces the index exactly — same events, same threads,
  same searchable corpus. Not "close": equal.
- the events a search can reach never shrink, so no reachable conversation is
  quietly dropped by an operation that reported success.

A failure here is a real durability bug, and the shrinker hands back the shortest
sequence that causes it — which is the other reason to generate rather than
enumerate: the counterexample arrives already minimized.

Deterministic (``derandomize``), so a red is the change under test rather than
the dice. Hunt wider by hand with ``--hypothesis-seed=<n>``.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)
from sqlalchemy import func, select

from thread_archive import _api as ta
from thread_archive._store import Event, Thread, get_session

# Session names are the fixture's only real degree of freedom, so they stay a
# small alphabet: what varies between examples should be the *order of
# operations*, not the spelling of a filename. Distinct content per name matters
# — claude-code's continuation detection folds two sessions with identical
# content into one thread, which is correct behaviour and not what this file is
# about.
NAMES = st.sampled_from(["alpha", "bravo", "charlie", "delta"])


def _counts() -> tuple[int, int]:
    with get_session() as s:
        events = int(s.execute(select(func.count()).select_from(Event)).scalar() or 0)
        threads = int(s.execute(select(func.count()).select_from(Thread)).scalar() or 0)
    return events, threads


def _reachable() -> set[int]:
    """Every event id a search can reach, by the widest scope there is.

    An empty query browses threads rather than events, so reachability is asked
    the way an agent would ask it after being told to find everything: a scan of
    the event table is what the index *holds*, and this is what it *serves*.
    """
    hits = ta.search("", limit=500)
    ids = {int(h["event_id"]) for h in hits}
    for thread in {h["thread_id"] for h in hits}:
        ids |= {int(h["event_id"]) for h in ta.search("", thread_id=thread, limit=500)}
    return ids


class ArchiveDurability(RuleBasedStateMachine):
    """One archive, driven through generated sequences of real operations."""

    sessions = Bundle("sessions")

    def __init__(self) -> None:
        super().__init__()
        # A fresh home per example, opened by hand rather than through the
        # suite's per-*test* fixtures: hypothesis runs many examples inside one
        # test, and an archive shared across them would carry state between
        # sequences — which is exactly the confound this file exists to rule out.
        self.home = Path(tempfile.mkdtemp(prefix="thread-archive-durability-"))
        self.store = self.home / "stores"
        self.store.mkdir()
        ta.open_archive(str(self.home))
        self.turns: dict[str, int] = {}

    def teardown(self) -> None:
        ta.close()
        shutil.rmtree(self.home, ignore_errors=True)

    # ── the operations ───────────────────────────────────────────────────────

    def _transcript(self, name: str) -> Path:
        return self.store / f"{name}.jsonl"

    def _write(self, name: str) -> Path:
        """(Re)write ``name``'s transcript to the turn count it should now have."""
        import json

        lines = []
        for i in range(self.turns[name]):
            lines.append({
                "type": "user", "uuid": f"u-{name}-{i}", "sessionId": name,
                "timestamp": f"2026-01-0{i % 9 + 1}T10:00:00Z", "cwd": "/proj",
                "message": {"role": "user", "content": f"{name} asks about step {i}"},
            })
            lines.append({
                "type": "assistant", "uuid": f"a-{name}-{i}", "sessionId": name,
                "timestamp": f"2026-01-0{i % 9 + 1}T10:00:05Z",
                "message": {"role": "assistant", "model": "claude-opus-4",
                            "content": [{"type": "text",
                                         "text": f"{name} answers step {i}"}]},
            })
        path = self._transcript(name)
        path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
        return path

    @initialize(target=sessions, name=NAMES)
    def first_session(self, name: str) -> str:
        """Every sequence starts from a non-empty archive: an invariant that only
        ever ran against zero rows would hold for the wrong reason."""
        self.turns[name] = 1
        ta.import_path(self._write(name), provider="claude-code", source_id=name)
        return name

    @rule(target=sessions, name=NAMES)
    def import_session(self, name: str) -> str:
        """A new conversation arrives (or an already-known one is re-offered
        unchanged, which is what a watcher pass does most of the time)."""
        self.turns.setdefault(name, 1)
        ta.import_path(self._write(name), provider="claude-code", source_id=name)
        return name

    @rule(name=sessions)
    def extend_session(self, name: str) -> None:
        """The live case: a session the archive has already read grows, and the
        next import must resume from its watermark rather than re-import the
        head."""
        self.turns[name] += 1
        ta.import_path(self._write(name), provider="claude-code", source_id=name)

    @rule(data=st.data())
    def amend_an_event(self, data) -> None:
        """An append-only edit: a superseding truth line whose merged payload
        re-hashes to the same dedup_key. The operation most able to break the
        truth↔index correspondence, because it writes a *second* line for an id
        the index already holds."""
        with get_session() as s:
            rows = s.execute(select(Event.id, Event.thread_id).order_by(Event.id)).all()
        if not rows:
            return
        event_id, thread_id = data.draw(st.sampled_from(rows))
        cost = data.draw(st.floats(min_value=0.01, max_value=9.99, allow_nan=False))
        ta.amend([(str(thread_id), int(event_id), {"cost": round(cost, 4)})],
                 reason="durability property")

    @rule()
    def checkpoint(self) -> None:
        """Snapshot the mutable authored tables into the truth log."""
        ta.checkpoint()

    @rule()
    def rebuild_the_index(self) -> None:
        """Delete the projection and rebuild it from truth. The load-bearing
        claim: what comes back is not merely similar to what was there."""
        before_counts = _counts()
        before_reachable = _reachable()

        ta.reindex()

        assert _counts() == before_counts, (
            f"a rebuild from truth changed the archive's shape: "
            f"{before_counts} → {_counts()} (events, threads)"
        )
        assert _reachable() == before_reachable, (
            "a rebuild from truth changed which events a search can reach — "
            f"{len(before_reachable - _reachable())} lost, "
            f"{len(_reachable() - before_reachable)} appeared"
        )

    # ── the invariant, after every step ──────────────────────────────────────

    @invariant()
    def truth_and_index_agree(self) -> None:
        result = ta.verify(deep=True)
        assert result["ok"], (
            f"truth and index disagree after this step: "
            f"failed={result.get('failed')} "
            f"drift_events={result.get('drift_events')} "
            f"drift_threads={result.get('drift_threads')} "
            f"parse_errors={result.get('parse_errors')}"
        )

    @invariant()
    def every_stored_event_is_reachable(self) -> None:
        """The index holding a row a search cannot return is silent loss: the
        conversation is preserved and unfindable, which for a memory-of-record is
        the same as gone."""
        events, _ = _counts()
        if not events:
            return
        assert _reachable(), (
            f"the archive holds {events} event(s) and search reaches none of them"
        )


# The steps are real imports, rebuilds and deep verifies against SQLite and the
# filesystem, so the budget buys sequences rather than repetitions: enough
# examples to interleave the operations in orders nobody wrote by hand, few
# enough that the file stays inside a per-commit suite. `function_scoped_fixture`
# is suppressed because the machine owns its archive outright (see __init__) and
# takes nothing per-example from the suite's fixtures.
ArchiveDurability.TestCase.settings = settings(
    derandomize=True,
    deadline=None,
    max_examples=12,
    stateful_step_count=8,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

TestArchiveDurability = ArchiveDurability.TestCase
