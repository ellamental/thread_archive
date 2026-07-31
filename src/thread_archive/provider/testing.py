"""Test support for provider plugins — an isolated archive, a conformance kit,
and a golden harness.

A provider is a format contract with something you don't control, and the way
that contract breaks is quiet: a renamed key, a block that stops being emitted, a
timestamp that shifts. None of it raises. What catches it is pinning the *whole*
normalized output and reading the diff when it moves.

That is what :func:`assert_golden` does. Import a fixture transcript into a
throwaway archive, and the truth JSONL it produces — the canonical normalized
form, exactly what would be stored for real — is compared field-for-field
against a reviewed file you commit. Any change to what your importer emits shows
up as a reviewable diff instead of passing silently.

A golden locks *your* output. What it cannot check is the half of the contract
archive relies on from the outside — that your watcher stays quiet on a machine
without your harness, that your descriptor's cross-references resolve, that
importing the same transcript twice doesn't double it. Those are
:func:`assert_provider_contract` and :func:`assert_reimport_adds_nothing`, and
they are the same assertions archive holds its own providers to
(``tests/test_provider_contract.py`` runs them over every built-in).

Volatile fields are normalized before comparison so a golden is stable across
runs: per-run uuids (stream/api-call ids) become ordinals, wall-clock fields
(row id, ``recorded_at``) are dropped, and the tmp path is scrubbed to a
placeholder. What a golden locks is *your normalization*, not the provider's
format — drift on their side still needs real captures.

## Using it

This module ships a pytest plugin. Enable it in your ``conftest.py``::

    pytest_plugins = ["thread_archive.provider.testing"]

which gives you an ``archive_home`` fixture — a tmp archive, fully isolated from
the real one. Then::

    def test_myharness_golden(archive_home):
        init_archive()
        path = archive_home / "session.jsonl"
        path.write_text(MY_FIXTURE)
        import_myharness_session(path, "s1")
        assert_golden("myharness", archive_home, GOLDEN_DIR)

Generate the golden the first time (and after any deliberate importer change)
with ``UPDATE_GOLDENS=1 pytest``, then **read the diff before committing it** —
a regenerated golden that nobody looked at locks in whatever regression prompted
the regeneration.

Make the fixture carry your format's interesting shapes, not a happy path: tool
calls with their results, thinking/reasoning, a block type you don't model, an
unknown field, a torn final line. Those are where preservation actually gets
decided.

The conformance calls need no fixture and no store::

    def test_myharness_conforms():
        assert_provider_contract(PROVIDER)

    def test_myharness_reimport_is_a_noop(archive_home):
        init_archive()
        path = archive_home / "session.jsonl"
        path.write_text(MY_FIXTURE)
        assert_reimport_adds_nothing(
            lambda: import_myharness_session(path, "s1"), source="myharness"
        )
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import pytest
except ImportError as e:  # pragma: no cover — the extra isn't installed
    # Test support, so pytest is an extra rather than a runtime dependency of the
    # archive itself: importing thread_archive must never require a test runner.
    raise ImportError(
        "thread_archive.provider.testing requires pytest — install the 'testing' "
        "extra (pip install 'thread-archive[testing]')"
    ) from e

from .._config import ENV_HOME, resolve_paths
from .._store import init_db

#: Thread-record fields a golden pins. Identity and provenance — the things a
#: format change can silently alter — not the mutable bookkeeping around them.
THREAD_KEYS = ("name", "title", "source", "source_id", "source_metadata", "description")


@pytest.fixture
def archive_home(tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> Iterator[Path]:
    """A tmp archive home, pointed at by ``THREAD_ARCHIVE_HOME``.

    Isolated from the real archive: an import in a test can never reach the
    machine's actual conversation store.
    """
    from .._store import _base
    from .._truth import jsonl_log

    home = tmp_path / "archive"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(ENV_HOME, str(home))
    monkeypatch.delenv("THREAD_ARCHIVE_TRUTH_DIR", raising=False)
    monkeypatch.delenv("THREAD_ARCHIVE_INDEX", raising=False)
    _base.close_engine()
    jsonl_log.reset_handles()
    yield home
    jsonl_log.reset_handles()
    _base.close_engine()


def init_archive() -> None:
    """Create the archive schema in the current home. Call before importing."""
    init_db()


def _scrub(value: Any, tmp: str) -> Any:
    if isinstance(value, str):
        return value.replace(tmp, "<fixtures>")
    if isinstance(value, list):
        return [_scrub(v, tmp) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, tmp) for k, v in value.items()}
    return value


def normalized_truth(home: "str | os.PathLike[str]") -> list[dict]:
    """Every truth record in thread-file order, volatile fields normalized.

    Thread files are ULID-named, so plain lexicographic sort is mint order.

    The comparable form of what an import actually wrote. Stream and api-call
    uuids are replaced by ordinals in first-seen order — they are freshly minted
    each run, so comparing them raw would fail every time while still hiding a
    real change in how turns *group*, which the ordinals preserve exactly.
    """
    threads_dir = resolve_paths(None).truth_dir / "threads"
    streams: dict[str, str] = {}
    calls: dict[str, str] = {}
    out: list[dict] = []
    if not threads_dir.exists():
        return out
    for f in sorted(threads_dir.rglob("*.jsonl")):
        for raw in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(raw)
            if rec.get("type") == "thread":
                row: dict = {"type": "thread", **{k: rec.get(k) for k in THREAD_KEYS}}
            elif rec.get("type") == "event":
                sid, cid = rec.get("stream_id"), rec.get("api_call_id")
                row = {
                    "type": "event",
                    "event_type": rec["event_type"],
                    "occurred_at": rec.get("occurred_at"),
                    "stream": streams.setdefault(sid, f"s{len(streams)}") if sid else None,
                    "api_call": calls.setdefault(cid, f"c{len(calls)}") if cid else None,
                    "dedup_key": rec.get("dedup_key"),
                    "payload": rec.get("payload"),
                }
            else:
                row = rec
            out.append(_scrub(row, str(home)))
    return out


def assert_golden(
    name: str,
    home: "str | os.PathLike[str]",
    golden_dir: "str | os.PathLike[str]",
    *,
    update_env: str = "UPDATE_GOLDENS",
) -> None:
    """Compare this import's normalized truth against the reviewed golden ``name``.

    With ``$UPDATE_GOLDENS`` set the golden is rewritten and the test **skips**
    rather than passing — a regeneration is not evidence of anything until
    someone reads the diff, and a pass would read as one.
    """
    got = normalized_truth(home)
    golden_file = Path(golden_dir) / f"{name}.json"
    if os.environ.get(update_env):
        golden_file.parent.mkdir(parents=True, exist_ok=True)
        golden_file.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"golden regenerated: {golden_file.name} — review the diff")
    assert golden_file.exists(), (
        f"no golden for {name}; generate it with {update_env}=1 and review it before committing"
    )
    want = json.loads(golden_file.read_text(encoding="utf-8"))
    assert got == want, (
        f"{name}: normalized output diverged from the reviewed golden. If the importer "
        f"change is deliberate, regenerate with {update_env}=1 and review the diff."
    )


def write_jsonl(
    path: "str | os.PathLike[str]", lines: list, *, torn_tail: Optional[str] = None
) -> None:
    """Write a JSONL fixture, optionally ending mid-line.

    ``torn_tail`` appends unterminated bytes with no trailing newline — a write
    cut off by a crash. Worth having in a fixture: a torn final line is the
    normal state of any transcript being written right now, so an importer that
    can't skip one and keep the complete lines fails constantly in practice.
    """
    text = "\n".join(json.dumps(ln) for ln in lines) + "\n"
    if torn_tail is not None:
        text += torn_tail
    Path(path).write_text(text, encoding="utf-8")


# ── the conformance kit ──────────────────────────────────────────────────────
# What archive relies on from a provider it did not write. Every assertion below
# is a fault archive cannot see for itself: a watcher that claims a store it
# doesn't have joins every poll, every health record and every coverage verdict
# on a machine whose operator never installed that harness; a descriptor whose
# cross-references don't resolve silently disables the thing it names; an import
# that isn't idempotent doubles the archive one watcher pass at a time.

#: Provider names are permanent identity (``Thread.source``, the config opt-out
#: key, a search filter), so they live in URLs, JSON keys and shell arguments.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def assert_provider_contract(provider: Any, *, siblings: "Sequence[str]" = ()) -> None:
    """Hold a :class:`~thread_archive.provider.Provider` to the contract archive
    reads it under. No fixture, no store, no import — runnable against a
    descriptor alone, on a machine that doesn't have the harness installed.

    What it checks, and why each one is a fault nothing else catches:

    - **the name is a stable identifier** — lowercase, hyphenated, no spaces. It
      is stored on every thread, typed as a search filter and a config key, and
      changing it later orphans everything already imported under the old one.
    - **cross-references resolve.** ``follows`` and ``parser_id`` name other
      providers; a name that matches nothing doesn't raise, it just quietly never
      fires — the follow never disables, the parser never registers.
    - **the provider can actually receive something.** A descriptor with neither
      a watcher nor an ``export`` is registered, listed, and permanently empty.
    - **``detect`` declines cleanly.** The export-drop watcher offers every bundle
      to every spec in registry order and the first claim wins, so a ``detect``
      that raises on something that isn't its export takes down the drop zone for
      every other provider.
    - **the watcher is quiet on a machine without the harness.** Constructing it,
      asking ``is_available``, ``discover``, ``store_paths`` and ``store_items``
      must all work with the store absent — that is the state on most machines,
      and it is the poll loop's gate: a watcher that raises or claims an absent
      store puts its errors into the operator's health record forever.
    - **the watcher agrees with itself.** ``discover().name`` and ``source_name``
      are the provider's own name (threads land under it, and coverage judges by
      it), and ``discover().available`` matches ``is_available()``.
    - **``store_items`` pairs with ``store_paths``** — the capture-coverage check
      asks, per file, whether the archive accounts for it, so an item naming a
      file the store doesn't list can never be reconciled.

    ``siblings`` names the other providers this one may reference — the built-ins
    are always in scope, so it is only needed for a plugin that ships as a set.
    """
    from .. import provider as api
    from .._providers.builtins import builtin_providers
    from .._watcher.base import SourceDiscovery, SourceWatcher

    p = api.resolve_provider(provider)

    assert _NAME_RE.match(p.name), (
        f"provider name {p.name!r} is not a stable identifier — it is stored on "
        f"every thread and typed as a filter and a config key, so it must be "
        f"lowercase alphanumeric with single hyphens"
    )
    assert p.label.strip(), f"provider {p.name!r} has no label to present to an operator"

    known = {q.name for q in builtin_providers()} | set(siblings) | {p.name}
    for field_name in ("follows", "parser_id"):
        ref = getattr(p, field_name)
        assert ref is None or ref in known, (
            f"provider {p.name!r}: {field_name}={ref!r} names no known provider, so "
            f"it silently never takes effect (pass siblings= for a plugin set)"
        )

    assert p.watcher is not None or p.export is not None, (
        f"provider {p.name!r} has neither a watcher nor an export spec — nothing "
        f"can ever feed it, so it would register, list, and stay empty"
    )

    if p.export is not None:
        assert p.export.kind.strip(), (
            f"provider {p.name!r}: export.kind is the retention slug imported "
            f"bundles are kept under, and cannot be empty"
        )
        assert p.export.label.strip(), f"provider {p.name!r}: export has no label"
        with tempfile.TemporaryDirectory() as empty:
            try:
                claimed = p.export.detect(Path(empty))
            except Exception as e:  # noqa: BLE001 — that it raised at all is the finding
                raise AssertionError(
                    f"provider {p.name!r}: export.detect raised {type(e).__name__} on a "
                    f"directory that is not an export ({e}). Every bundle in the drop "
                    f"zone is offered to every spec, so a raising detect stops the "
                    f"queue for every other provider"
                ) from e
            assert not claimed, (
                f"provider {p.name!r}: export.detect claims an empty directory, which "
                f"would quarantine another provider's export"
            )

    if p.watcher is None:
        return

    watcher = p.watcher()
    assert isinstance(watcher, SourceWatcher), (
        f"provider {p.name!r}: watcher() returned {type(watcher).__name__}, not a "
        f"SourceWatcher"
    )
    assert watcher.source_name == p.name, (
        f"provider {p.name!r}: its watcher calls itself {watcher.source_name!r}. "
        f"Threads land under the watcher's name and the descriptor is looked up by "
        f"its own, so the two disagreeing splits the source in half"
    )

    available = watcher.is_available()
    assert isinstance(available, bool), (
        f"provider {p.name!r}: is_available() returned {available!r}, not a bool — "
        f"it is the poll loop's gate and is read as one"
    )

    discovery = watcher.discover()
    assert isinstance(discovery, SourceDiscovery), (
        f"provider {p.name!r}: discover() returned {type(discovery).__name__}, not a "
        f"SourceDiscovery"
    )
    assert discovery.name == p.name, (
        f"provider {p.name!r}: discover() reports itself as {discovery.name!r}"
    )
    assert discovery.available == available, (
        f"provider {p.name!r}: discover() says available={discovery.available} while "
        f"is_available() says {available} — setup shows one and the poll loop obeys "
        f"the other"
    )

    paths = list(watcher.store_paths())
    items = list(watcher.store_items())
    if not available:
        assert not paths and not items, (
            f"provider {p.name!r}: an unavailable store still enumerates "
            f"{len(paths)} path(s) and {len(items)} item(s)"
        )
    assert {path for path, _ in items} <= set(paths), (
        f"provider {p.name!r}: store_items() names files store_paths() does not "
        f"list, so the capture-coverage check can never reconcile them"
    )


def assert_reimport_adds_nothing(
    run: "Callable[[], Any]", *, source: Optional[str] = None
) -> tuple[Any, Any]:
    """Run an import twice and require the second to create nothing.

    The invariant the whole ingest loop rests on. A watcher re-offers a
    transcript on every fingerprint change — an in-progress session is re-read
    from the top many times — so an importer that is not idempotent doesn't fail,
    it inflates: the same turns land again, search returns each of them, and the
    archive's own record of what was said stops being one.

    ``run`` is a zero-argument callable that performs the import, so it fits every
    importer shape (``lambda: import_x(path, "s1")``, ``lambda:
    scan_db(db_path)``). The archive must already be open and its schema created
    (:func:`init_archive`).

    ``source``, when given, additionally requires the import to have left a
    watermark for that provider: the row that records how far this store has been
    read. Without one an importer is idempotent only by luck of dedup — it
    re-reads and re-decides the whole transcript every pass, which is a growing
    cost per poll and the thing the watermark exists to bound.

    Returns both runs' results, so a caller can go on to assert what its own
    importer reports (``is_new_thread``, ``processed`` / ``imported`` counts)
    without importing a third time to get at them.
    """
    from sqlalchemy import func, select

    from .._store import Event, ImportState, get_session

    def _events() -> int:
        with get_session() as s:
            return int(s.execute(select(func.count()).select_from(Event)).scalar() or 0)

    before = _events()
    first = run()
    after_first = _events()
    assert after_first > before, (
        "the first import created no events, so this proves nothing about the "
        "second — point it at a fixture the importer actually reads"
    )

    second = run()
    after_second = _events()
    assert after_second == after_first, (
        f"re-importing the same transcript created {after_second - after_first} more "
        f"event(s). A watcher re-reads a session on every change, so this is "
        f"duplication in the archive rather than a wasted pass — check the "
        f"dedup_key is derived from the transcript's own identity and not from "
        f"anything minted per run"
    )

    if source is not None:
        with get_session() as s:
            rows = s.execute(
                select(ImportState).where(ImportState.source == source)
            ).scalars().all()
        assert rows, (
            f"the import left no import_state row for {source!r}, so the next poll "
            f"re-reads the whole store and relies on dedup to stay correct"
        )
        assert any(r.last_import_at is not None for r in rows), (
            f"{source!r}'s watermark carries no last_import_at, so nothing can say "
            f"when the source last fed the archive (status reads it per source)"
        )
    return first, second


__all__ = [
    "archive_home",
    "init_archive",
    "assert_golden",
    "assert_provider_contract",
    "assert_reimport_adds_nothing",
    "normalized_truth",
    "write_jsonl",
    "THREAD_KEYS",
]
