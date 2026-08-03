#!/usr/bin/env python3
"""What each benchmark scores, pinned to the bytes it was scored on.

Every corpus on the bench is somebody else's file fetched from somewhere, and
none of the upstreams offer an immutable handle this lab can rely on: BEIR is a
plain zip URL, three of the datasets are a ``git clone`` of a default branch, and
the two Hugging Face files are ``resolve/main`` — a branch tip, not a revision. So
"the same benchmark" is not a fact about the source. It is a fact about the bytes,
and this module is where those bytes are named and checked.

    python -m search_lab pins            # what is on disk vs what is accepted
    python -m search_lab pins --update   # accept what is on disk as the pin

**A content hash is a stronger pin than a revision.** A revision only binds a
re-fetch; the hash binds the file however it got there — a re-download, a repo
that force-pushed its default branch, a half-extracted zip, a local edit. The
accepted hashes live in ``dataset-pins.json`` beside this file, checked in for the
same reason ``quality-baseline.json`` is: a number is only auditable next to a
statement of what it was measured on, and that statement has to travel with the
code rather than sit in a per-box cache.

**Two jobs, one hash.**

*Prevention.* Each harness verifies its dataset before it builds or scores, so a
corpus that moved fails the run where it moved, naming the file. This is the half
a revision pin would also buy.

*Detection.* :meth:`benchmark.Row.corpus_id` reports the corpus a row's numbers
describe, and the gate refuses to read a delta across a change in it. Rows that
build an archive home get that identity from the home's snapshot manifest — but
the per-question haystacks (``locomo``, ``longmemeval``, ``beam``) build no single
home, so they had no corpus identity at all and were guarded only by the scored
query count. A dataset that changed *content* at a constant count was invisible to
the gate on exactly those three rows. Their fingerprint comes from here instead.

**Unpinned is not an error.** A dataset with no accepted hash verifies as a no-op:
which corpora a box has is a fact about the box, and a lab that refused to run an
unpinned one would be unrunnable everywhere but here. Accepting a pin is a verb
somebody types, like accepting a baseline — and until they do, the dataset is
listed as unpinned rather than silently treated as fine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_runs  # noqa: E402
import eval_home  # noqa: E402

#: The accepted hashes. Beside the code, not in the cache, because a release is
#: cut against the code and "which corpus" is part of what the numbers mean.
PINS_PATH = Path(__file__).resolve().parent / "dataset-pins.json"

#: Where the per-file digests are memoized, keyed by ``(size, mtime_ns)``. The
#: whole point is that verifying is cheap enough to sit in front of every harness
#: run: LongMemEval alone is 277 MB, and re-reading it on each invocation would
#: make the check something a caller looks for a way around.
CACHE_FILE = "dataset-hashes.json"

#: How far a file's modification time must predate the read that hashed it before
#: that pairing is trusted again. The nanosecond field is not nanosecond-precise:
#: granularity belongs to the filesystem, and whole-second stamping is common
#: enough that the container the install proof builds in does exactly that. Under
#: a clock that coarse a write landing in the same tick as the read leaves
#: ``mtime_ns`` untouched, and at an unchanged length leaves the whole memo key
#: untouched. Two seconds clears the coarsest stamping in practical use and costs
#: a corpus nothing: a dataset is fetched once and read for months, so its mtime
#: is never inside the window.
SETTLE_NS = 2_000_000_000


@dataclass(frozen=True)
class Source:
    """One dataset's identity: the files it is, and where it came from."""

    #: Paths relative to the eval cache root, in a fixed order — the fingerprint
    #: hashes them in this sequence, so reordering the tuple changes the answer.
    #: A directory contributes every file under it, sorted.
    paths: tuple[str, ...]

    #: Where to re-fetch it, written so somebody staring at a drifted hash can act
    #: on it.
    upstream: str

    #: The upstream revision that produces exactly these bytes — what makes a fresh
    #: box able to *obtain* the pinned corpus rather than merely discover it has a
    #: different one. The hash stays the authority; this is the handle that leads to
    #: it, and the two are checked against each other before it is written down.
    #:
    #: **Only ever recorded once fetching it has been confirmed to reproduce the
    #: pinned hash.** An unconfirmed revision is worse than none: it reads as
    #: provenance while sending the next person to bytes nobody compared. The
    #: confirmation differs by host — a clean clone at that commit, a Hugging Face
    #: LFS ``oid``, a GitHub blob sha — and none of them require downloading the
    #: corpus again.
    #:
    #: None where no revision is recorded; ``upstream`` says whether that is because
    #: the host offers no such handle or because nobody has established which one.
    revision: Optional[str] = None


#: Every corpus any harness in this directory scores, on or off the bench manifest.
#: ``trec-covid`` and ``mtrag`` are held off the manifest on cost but stay runnable
#: by hand, and a hand-run number is worth no less care about what produced it.
SOURCES: dict[str, Source] = {
    # The three BEIR sets carry no revision because BEIR offers none: the corpus is
    # a zip at a fixed URL on a university web server, with no version in the path
    # and no digest published beside it. Re-serving different bytes there is
    # undetectable upstream and unobtainable afterwards, so these are pinned for
    # detection only — a drift here means the corpus is gone, not that it can be
    # re-fetched.
    "scifact": Source(
        paths=("scifact/corpus.jsonl", "scifact/queries.jsonl",
               "scifact/qrels/test.tsv"),
        upstream="public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip"
                 " — versionless: the URL is the only handle BEIR publishes",
    ),
    "nfcorpus": Source(
        paths=("nfcorpus/corpus.jsonl", "nfcorpus/queries.jsonl",
               "nfcorpus/qrels/test.tsv"),
        upstream="public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip"
                 " — versionless: the URL is the only handle BEIR publishes",
    ),
    "trec-covid": Source(
        paths=("trec-covid/corpus.jsonl", "trec-covid/queries.jsonl",
               "trec-covid/qrels/test.tsv"),
        upstream="public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/trec-covid.zip"
                 " — versionless: the URL is the only handle BEIR publishes",
    ),
    "locomo": Source(
        paths=("locomo/repo/data/locomo10.json",),
        upstream="github.com/snap-research/locomo (clone into locomo/repo)",
        revision="3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376",
    ),
    "longmemeval": Source(
        paths=("longmemeval/data/longmemeval_s_cleaned.json",),
        upstream="huggingface.co/datasets/xiaowu0162/longmemeval-cleaned "
                 "(longmemeval_s_cleaned.json)",
        revision="98d7416c24c778c2fee6e6f3006e7a073259d48f",
    ),
    "beam": Source(
        paths=("beam/100K.parquet",),
        upstream="huggingface.co/datasets/Mohammadta/BEAM "
                 "(data/100K-00000-of-00001.parquet)",
        revision="3205395e897e7318c7b094ef4e6047b9b82dbb03",
    ),
    "perltqa": Source(
        paths=("perltqa/perltmem_en.json", "perltqa/perltqa_en.json"),
        upstream="github.com/Elvin-Yiming-Du/PerLTQA (Dataset/en)",
        revision="8d9e19868e239740ef701e603ec205cd581f221b",
    ),
    "cdr": Source(
        paths=("CDR-Benchmark/cdr_benchmark_data/test_dataset/data/test/corpus.json",
               "CDR-Benchmark/cdr_benchmark_data/test_dataset/data/test/queries.json",
               "CDR-Benchmark/cdr_benchmark_data/test_dataset/data/test/relevant_docs.json"),
        upstream="github.com/l-yohai/CDR-Benchmark",
        revision="b9954dddf0c94c0aae784cc422a5424b92e4c689",
    ),
    "mtrag": Source(
        paths=("mtrag/corpora", "mtrag/retrieval_tasks"),
        upstream="github.com/IBM/mt-rag-benchmark (passage-level corpora + "
                 "retrieval_tasks) — revision not established: the fetched layout "
                 "does not correspond to the repository's own paths",
    ),
}


# ── hashing ──────────────────────────────────────────────────────────────────


def source_paths(dataset: str, root: Optional[Path] = None) -> tuple[Path, ...]:
    """The absolute files a dataset is, directories expanded, in fingerprint order.

    A declared path that does not exist is simply absent from the result — "some
    of it is here" is the state :func:`fingerprint` refuses to hash, and it says so
    there rather than inventing an identity for a partial download.

    ``root`` overrides the eval cache the paths resolve under, which is what lets
    this be exercised against a real tree of real files instead of a stubbed
    filesystem."""
    source = SOURCES.get(dataset)
    if source is None:
        return ()
    base = root if root is not None else eval_home.CACHE_ROOT
    out: list[Path] = []
    for entry in source.paths:
        path = base / entry
        if path.is_dir():
            out.extend(sorted(p for p in path.rglob("*") if p.is_file()))
        elif path.is_file():
            out.append(path)
    return tuple(out)


def _cache_path() -> Path:
    return bench_runs.ledger_home() / CACHE_FILE


def _load_cache() -> dict[str, Any]:
    try:
        blob = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _store_cache(cache: dict[str, Any]) -> None:
    """Best-effort: a memo that cannot be written is a slow verify, never a failed
    one. The hash it would have held is recomputed next time."""
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    except OSError:
        pass


def file_digest(path: Path, cache: Optional[dict[str, Any]] = None) -> str:
    """One file's sha256, memoized on ``(size, mtime_ns)`` once that key has settled.

    The memo key is metadata and the value is content, so it may only be believed
    where a change to the bytes is guaranteed to move the key. Timestamps are too
    coarse for that to hold on a freshly written file: a rewrite landing in the
    same tick as the read that hashed it leaves ``mtime_ns`` identical, and at an
    unchanged length nothing in the key moves at all. So an entry also records
    *when* it was read, and is trusted only where the file's mtime predates that
    read by :data:`SETTLE_NS`. Anything more recent is re-read — which is the
    cheap case, since a settled corpus is the one that is gigabytes."""
    stat = path.stat()
    key = str(path)
    entry = (cache or {}).get(key)
    if (isinstance(entry, dict) and entry.get("size") == stat.st_size
            and entry.get("mtime_ns") == stat.st_mtime_ns
            and isinstance(entry.get("sha256"), str)
            and isinstance(entry.get("read_at"), int)
            and stat.st_mtime_ns + SETTLE_NS < entry["read_at"]):
        return entry["sha256"]
    read_at = time.time_ns()
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if cache is not None:
        cache[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                      "sha256": digest, "read_at": read_at}
    return digest


def fingerprint(dataset: str, root: Optional[Path] = None) -> Optional[str]:
    """The dataset's content identity, or None when it is not on this box.

    Hashes each file's *digest* under its cache-relative name rather than the
    bytes themselves, so the per-file memo does the expensive part and a rename
    still counts as a change. None means unknown, never "empty": an absent corpus
    must not fingerprint to a stable value, or every box without it would agree
    with every other box without it and the gate would read that as a match."""
    base = root if root is not None else eval_home.CACHE_ROOT
    paths = source_paths(dataset, root)
    if not paths:
        return None
    cache = _load_cache()
    before = json.dumps(cache, sort_keys=True)
    h = hashlib.sha256()
    for path in paths:
        try:
            digest = file_digest(path, cache)
        except OSError:
            return None
        h.update(str(path.relative_to(base)).encode())
        h.update(b"\x00")
        h.update(digest.encode())
        h.update(b"\x00")
    if json.dumps(cache, sort_keys=True) != before:
        _store_cache(cache)
    return h.hexdigest()[:16]


# ── the accepted pins ────────────────────────────────────────────────────────


def load_pins(path: Path = PINS_PATH) -> dict[str, Any]:
    """The accepted hashes, or an empty set when none are recorded.

    A missing file is "nothing is pinned yet". An unparseable one is an error, for
    the reason a corrupt baseline is: a pin file that read as empty would verify
    everything as unpinned and report it as fine."""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"datasets": {}}
    if not isinstance(blob, dict) or not isinstance(blob.get("datasets"), dict):
        raise ValueError(f"{path} is not a dataset-pin file")
    return blob


def expected(dataset: str, path: Path = PINS_PATH) -> Optional[str]:
    entry = (load_pins(path).get("datasets") or {}).get(dataset)
    return entry.get("sha256") if isinstance(entry, dict) else None


def verify(dataset: str, path: Path = PINS_PATH,
           root: Optional[Path] = None) -> None:
    """Fail the run if ``dataset`` is not the corpus its pin was accepted on.

    Called by the harnesses before they build or score. A no-op when the dataset
    is unpinned or absent — the first is a decision nobody has made yet, and the
    second is the harness's own error to raise, with better instructions than
    anything this module knows."""
    want = expected(dataset, path)
    if want is None:
        return
    got = fingerprint(dataset, root)
    if got is None or got == want:
        return
    files = "\n  ".join(str(p) for p in source_paths(dataset, root))
    source = SOURCES[dataset]
    where = (f"  re-fetch at revision {source.revision}\n" if source.revision
             else "  no upstream revision to re-fetch: these bytes are not "
                  "recoverable from the source\n")
    raise SystemExit(
        f"{dataset}: corpus does not match its accepted pin.\n"
        f"  expected {want}, found {got}\n"
        f"  upstream: {source.upstream}\n"
        f"{where}"
        f"  files:\n  {files}\n"
        f"Scoring this would compare numbers across different data. Re-fetch the "
        f"pinned corpus, or accept the new one with "
        f"`python -m search_lab pins --update` and re-baseline the rows it feeds "
        f"(`python -m search_lab gate --run --quick --update`)."
    )


def build_pins(previous: Optional[dict[str, Any]] = None,
               root: Optional[Path] = None) -> dict[str, Any]:
    """The pin file the corpora on this box would establish.

    A dataset that is not here contributes nothing, and an already-accepted pin for
    it is carried forward: a box that has never downloaded MTRAG must not be able
    to un-pin it for everyone by running ``--update``."""
    kept = ((previous or {}).get("datasets") or {})
    out: dict[str, Any] = {}
    for dataset in SOURCES:
        got = fingerprint(dataset, root)
        if got is None:
            if dataset in kept:
                out[dataset] = kept[dataset]
            continue
        paths = source_paths(dataset, root)
        entry: dict[str, Any] = {
            "sha256": got,
            "files": len(paths),
            "bytes": sum(p.stat().st_size for p in paths),
            "upstream": SOURCES[dataset].upstream,
        }
        if SOURCES[dataset].revision:
            entry["revision"] = SOURCES[dataset].revision
        out[dataset] = entry
    return {
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "datasets": out,
    }


def write_pins(pins: dict[str, Any], path: Path = PINS_PATH) -> None:
    path.write_text(json.dumps(pins, indent=1, sort_keys=False) + "\n",
                    encoding="utf-8")


# ── cli ──────────────────────────────────────────────────────────────────────


def states(path: Path = PINS_PATH,
           root: Optional[Path] = None) -> list[tuple[str, str, str]]:
    """``(dataset, state, detail)`` for every known corpus, in registry order."""
    pins = (load_pins(path).get("datasets") or {})
    rows: list[tuple[str, str, str]] = []
    for dataset in SOURCES:
        want = (pins.get(dataset) or {}).get("sha256")
        got = fingerprint(dataset, root)
        if got is None:
            rows.append((dataset, "absent", "not downloaded on this box"))
        elif want is None:
            rows.append((dataset, "unpinned", f"{got} — accept with --update"))
        elif want == got:
            revision = SOURCES[dataset].revision
            rows.append((dataset, "pinned",
                         f"{got}  @{revision[:12]}" if revision
                         else f"{got}  (no revision — detection only)"))
        else:
            rows.append((dataset, "DRIFTED", f"expected {want}, found {got}"))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--update", action="store_true",
                    help="accept the corpora on this box as the pin")
    ap.add_argument("--pins", type=Path, default=PINS_PATH, metavar="FILE")
    args = ap.parse_args(argv)

    if args.update:
        fresh = build_pins(previous=load_pins(args.pins))
        write_pins(fresh, args.pins)
        print(f"accepted {len(fresh['datasets'])} dataset(s) into {args.pins}")
        return 0

    rows = states(args.pins)
    print(f"dataset pins against {args.pins.name} "
          f"(accepted {load_pins(args.pins).get('accepted_at', 'never')})\n")
    for dataset, state, detail in rows:
        print(f"  {dataset:<14}{state:<10}{detail}")
    drifted = [d for d, s, _ in rows if s == "DRIFTED"]
    unpinned = [d for d, s, _ in rows if s == "unpinned"]
    pinned = [d for d, s, _ in rows if s == "pinned"]
    print()
    if unpinned:
        print(f"{len(unpinned)} unpinned — verified against nothing: "
              f"{', '.join(unpinned)}")
    if drifted:
        print(f"FAIL: {len(drifted)} drifted — {', '.join(drifted)}")
        return 1
    print(f"OK: {len(pinned)} corpus/corpora match their pin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
