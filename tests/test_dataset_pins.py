"""Dataset pins: the corpus a number was measured on, checked against the bytes.

The bench compares numbers across time, and every one of those comparisons
assumes the corpus held still. Nothing upstream guarantees that — BEIR is a plain
zip URL, three datasets are a clone of a default branch, two are Hugging Face
``resolve/main`` — so the guarantee has to be local, and it is a content hash.

What must hold, and what these cover:

- a change to the data changes the fingerprint, including a change that preserves
  the file count and the total size
- the fingerprint is *absent*, never a stable placeholder, when a corpus is not
  on the box — two boxes without a dataset must not agree that it matches
- ``verify`` fails a drifted corpus and stays out of the way of an unpinned or
  absent one
- ``--update`` cannot un-pin a dataset the running box merely does not have

Run against real files in a real tree: every case builds the dataset on disk and
points the resolver at it, so what is exercised is the hashing and the decision
table, not a description of them.
"""

from __future__ import annotations

import json

import pytest

from search_lab import benchmark, dataset_pins


def _tree(root, dataset="locomo", body=b"one\ntwo\n"):
    """Write a dataset's declared files under ``root`` and return it."""
    for rel in dataset_pins.SOURCES[dataset].paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return root


def _pins(tmp_path, **datasets):
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({
        "accepted_at": "2026-07-30T00:00:00+00:00",
        "datasets": {k: {"sha256": v} for k, v in datasets.items()},
    }))
    return path


# ── fingerprinting ───────────────────────────────────────────────────────────


def test_a_corpus_that_is_not_here_has_no_fingerprint(tmp_path) -> None:
    """Absent must read as unknown, not as a value. A stable placeholder would
    make every box lacking the dataset agree with every other, and the gate reads
    agreement as 'same corpus'."""
    assert dataset_pins.fingerprint("locomo", root=tmp_path) is None


def test_changing_the_data_changes_the_fingerprint(tmp_path) -> None:
    before = dataset_pins.fingerprint("locomo", root=_tree(tmp_path))
    (tmp_path / dataset_pins.SOURCES["locomo"].paths[0]).write_bytes(b"one\nTWO\n")
    after = dataset_pins.fingerprint("locomo", root=tmp_path)

    assert before and after and before != after


def test_a_swap_that_preserves_count_and_size_still_changes_it(tmp_path) -> None:
    """The exact drift the gate was blind to before this existed: a corpus whose
    question count and bulk are unchanged while its content is not. Guarding on
    ``n`` alone reads that as a ranking movement."""
    root = _tree(tmp_path, "cdr", body=b"aaaa")
    before = dataset_pins.fingerprint("cdr", root=root)
    (root / dataset_pins.SOURCES["cdr"].paths[1]).write_bytes(b"bbbb")
    after = dataset_pins.fingerprint("cdr", root=root)

    assert before != after


def test_the_fingerprint_is_stable_across_calls(tmp_path) -> None:
    # The memo is keyed on (size, mtime_ns); an unchanged tree must not produce a
    # new identity on the second read, or every row would read corpus-changed.
    root = _tree(tmp_path)
    assert dataset_pins.fingerprint("locomo", root=root) == \
        dataset_pins.fingerprint("locomo", root=root)


def test_moving_a_file_within_the_dataset_changes_it(tmp_path) -> None:
    """Names are hashed with the bytes, so the same content under a different
    layout is a different corpus — which it is, to a harness that reads by path."""
    root = _tree(tmp_path, "perltqa", body=b"same")
    before = dataset_pins.fingerprint("perltqa", root=root)
    first, second = (root / p for p in dataset_pins.SOURCES["perltqa"].paths)
    first.write_bytes(b"other")
    after = dataset_pins.fingerprint("perltqa", root=root)
    assert before != after
    # ...and swapping which file holds which bytes is visible too.
    first.write_bytes(b"same")
    second.write_bytes(b"other")
    assert dataset_pins.fingerprint("perltqa", root=root) != after


def test_a_partial_download_does_not_fingerprint_as_the_whole(tmp_path) -> None:
    root = _tree(tmp_path, "cdr")
    whole = dataset_pins.fingerprint("cdr", root=root)
    (root / dataset_pins.SOURCES["cdr"].paths[-1]).unlink()

    assert dataset_pins.fingerprint("cdr", root=root) != whole


# ── verify ───────────────────────────────────────────────────────────────────


def test_verify_passes_the_corpus_it_was_pinned_on(tmp_path) -> None:
    root = _tree(tmp_path / "cache")
    pins = _pins(tmp_path, locomo=dataset_pins.fingerprint("locomo", root=root))

    dataset_pins.verify("locomo", pins, root=root)  # no raise


def test_verify_fails_a_drifted_corpus_and_names_it(tmp_path) -> None:
    """The whole point: scoring against data that moved silently compares numbers
    across different corpora, and the number looks like a ranking result."""
    root = _tree(tmp_path / "cache")
    pins = _pins(tmp_path, locomo="0000000000000000")

    with pytest.raises(SystemExit) as raised:
        dataset_pins.verify("locomo", pins, root=root)

    message = str(raised.value)
    assert "locomo" in message and "0000000000000000" in message
    assert "pins --update" in message, "the message has to name the way out"


def test_verify_is_a_no_op_for_an_unpinned_corpus(tmp_path) -> None:
    """Which corpora a box has is a fact about the box. A lab that refused to run
    an unpinned one would be unrunnable anywhere but the machine that pinned it."""
    root = _tree(tmp_path / "cache")
    dataset_pins.verify("locomo", _pins(tmp_path), root=root)  # no raise


def test_verify_is_a_no_op_when_the_corpus_is_absent(tmp_path) -> None:
    # The harness raises its own missing-data error, with better instructions than
    # this module has. Failing here first would replace it with a worse one.
    pins = _pins(tmp_path, locomo="0000000000000000")
    dataset_pins.verify("locomo", pins, root=tmp_path / "empty")  # no raise


# ── accepting pins ───────────────────────────────────────────────────────────


def test_update_records_what_is_on_the_box(tmp_path) -> None:
    root = _tree(tmp_path / "cache")
    built = dataset_pins.build_pins(root=root)

    assert built["datasets"]["locomo"]["sha256"] == \
        dataset_pins.fingerprint("locomo", root=root)
    assert built["datasets"]["locomo"]["files"] == 1
    assert built["datasets"]["locomo"]["upstream"]


def test_update_cannot_unpin_a_dataset_this_box_lacks(tmp_path) -> None:
    """Otherwise running ``--update`` on a box without MTRAG would quietly drop
    MTRAG's pin for everyone, and the next run of it would verify against
    nothing."""
    root = _tree(tmp_path / "cache")
    previous = {"datasets": {"mtrag": {"sha256": "abc123", "files": 8}}}

    built = dataset_pins.build_pins(previous=previous, root=root)

    assert built["datasets"]["mtrag"]["sha256"] == "abc123"


def test_a_round_trip_through_the_file_verifies(tmp_path) -> None:
    root = _tree(tmp_path / "cache")
    path = tmp_path / "pins.json"
    dataset_pins.write_pins(dataset_pins.build_pins(root=root), path)

    dataset_pins.verify("locomo", path, root=root)
    assert [s for d, s, _ in dataset_pins.states(path, root=root) if d == "locomo"] \
        == ["pinned"]


def test_states_calls_a_drifted_corpus_drifted(tmp_path) -> None:
    root = _tree(tmp_path / "cache")
    pins = _pins(tmp_path, locomo="0000000000000000")

    states = dict((d, s) for d, s, _ in dataset_pins.states(pins, root=root))

    assert states["locomo"] == "DRIFTED"
    assert states["mtrag"] == "absent", "not on this tree at all"


# ── the checked-in pin file, and what reads it ───────────────────────────────


def test_every_pinned_dataset_is_one_the_lab_knows(tmp_path) -> None:
    """A pin for a dataset no harness reads is checked against nothing — the same
    silent no-op an orphaned baseline row is."""
    orphans = set(dataset_pins.load_pins()["datasets"]) - set(dataset_pins.SOURCES)
    assert not orphans, f"pinned but unknown: {sorted(orphans)}"


def test_every_pin_carries_a_hash_and_where_it_came_from() -> None:
    for name, entry in dataset_pins.load_pins()["datasets"].items():
        assert entry.get("sha256"), name
        assert entry.get("upstream"), name


def test_a_recorded_revision_is_a_full_commit_sha() -> None:
    """A revision is the handle a fresh box fetches by, so an abbreviated or
    hand-typed one is a dead end at exactly the moment somebody needs it."""
    for name, source in dataset_pins.SOURCES.items():
        if source.revision is None:
            continue
        assert len(source.revision) == 40, name
        assert all(c in "0123456789abcdef" for c in source.revision), name


def test_a_source_without_a_revision_says_why_in_its_upstream() -> None:
    """None is two different facts — the host publishes no such handle, or nobody
    has established which one produced these bytes — and they call for different
    work. A bare None reads as an oversight."""
    for name, source in dataset_pins.SOURCES.items():
        if source.revision is not None:
            continue
        assert "versionless" in source.upstream or "not established" in source.upstream, \
            f"{name} records no revision and does not say why"


def test_the_pin_file_carries_the_revision_the_registry_declares() -> None:
    """The checked-in file is what a reader consults; a revision that lived only in
    the registry would leave it describing bytes with no way back to them."""
    pins = dataset_pins.load_pins()["datasets"]
    for name, source in dataset_pins.SOURCES.items():
        if source.revision and name in pins:
            assert pins[name].get("revision") == source.revision, name


def test_every_bench_row_reports_a_corpus_identity() -> None:
    """The gate refuses to read a delta across a corpus change, which it can only
    do for a row that states which corpus it ran on. A row returning None is
    exempt from that check — silently, and forever."""
    for row in benchmark.manifest():
        assert row.home is not None or row.dataset_name() in dataset_pins.SOURCES, \
            f"{row.name} has neither a home nor a pinned source to fingerprint"
