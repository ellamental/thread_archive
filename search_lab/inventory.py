#!/usr/bin/env python3
"""What the bench has on hand — the instruments and the corpora.

A bench instrument, read deliberately::

    .venv/bin/python search_lab/inventory.py
    .venv/bin/python search_lab/inventory.py --json

Every other module here *measures*; this one only answers "what is on this box?"
— which benchmark rows can run right now and which are waiting on a corpus, and
what each dataset costs in disk and holds. That question is answered today by
reading four READMEs against a ``du`` and a ``ls``, and the answer goes stale the
moment a corpus is built or deleted. Reading it off the same registries the
harnesses run from means the page cannot describe a bench that is not there.

:func:`runs` is the second question and reads nothing from disk but the ledger —
not "what does the bench say now" but "what has it ever said here", every
recorded run rather than the newest per row. It is served on its own route for
that reason: the inventory below is a filesystem walk behind a cache, and a
history that has to be current must not be assembled behind one.

**Nothing here is a catalog of its own.** Rows come from
:func:`search_lab.benchmark.manifest`, the published-reference dicts the external
harnesses already carry, and the ledgers — so a benchmark row added or a dataset
downloaded shows up without an edit here. What is written down in this module is
one line per corpus *family*, describing what that family of harness measures;
the survey of benchmarks that could exist but do not yet lives in
``docs/benchmarks.md``, which is prose and stays prose.

Devweb renders this at ``/lab``, the same way ``retrieval_report`` reaches
``/retrieval``: a dev surface that lives outside the wheel. That is the right
side of the boundary — an install carries no measurement surface, so it has no
bench to inventory.

Two costs are bounded on purpose, because this runs inside the always-on
watcher process:

- **Disk sizes are a capped walk.** A built home runs to gigabytes across
  hundreds of thousands of files, and the per-question haystack roots are
  hundreds of small homes. :func:`dir_bytes` stops at a file budget and says so
  (``truncated``), so a page load can never turn into a filesystem sweep.
- **Corpus counts come from the snapshot manifest**, not from the index — a
  built home records its own event/thread/vector counts when it is stamped, and
  opening a 12 GB SQLite index to re-derive them would be the expensive way to
  learn what a 400-byte JSON file already says.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

#: The one-pass size index (see :func:`size_index`), or None to walk on demand.
Sizes = Optional[dict]

# The repo root, so the siblings reach here as ``search_lab.X`` however this file
# was loaded — as a script, or as ``search_lab.inventory`` from the dev bridge.
# Package-qualified throughout and not by bare sibling import, because some of
# what this reads reaches its own siblings relatively and only resolves inside
# the package. Importing the package puts the lab dir and ``src`` on the path in
# turn (see ``__init__``).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: One line per corpus family: what a harness in it measures, and what a number
#: from it is worth. Keyed by the ``family`` on every dataset row.
FAMILIES: dict[str, str] = {
    "beir": "public IR benchmarks — nDCG@10 beside the published Anserini BM25 "
            "and a strong zero-shot dense retriever",
    "cdr": "NVIDIA ChatRAG conversational retrieval — nDCG@10 beside the best of "
           "the 16 embedding models the leaderboard evaluates",
    "haystack": "conversational memory, per question: each question carries its "
                "own history and the task is to pull the evidence out of it",
    "mtrag": "multi-turn RAG retrieval over four document corpora — the same "
             "information need as a terse last turn and as a standalone rewrite, "
             "nDCG@10 beside the published BM25 / BGE / Elser table for both",
    "memory-units": "personal long-term memory retrieval, scored by memory-unit "
                    "id — labels exist by construction (each question was "
                    "authored from the unit it names), no published baseline",
}

#: How many files a size walk will stat before it gives up and reports a partial
#: number. Generous enough that every corpus on the bench completes; small enough
#: that a page load stays under a second on a cold cache.
WALK_BUDGET = 60_000


# ── filesystem ────────────────────────────────────────────────────────────────

def dir_bytes(path: Path, *, budget: int = WALK_BUDGET) -> dict[str, Any]:
    """Bytes under ``path``, walked with a file budget.

    Returns ``{"bytes", "files", "truncated"}``. ``truncated`` is the honest
    signal that the number is a floor rather than the size: a partial sum
    reported as a total would understate a large corpus by whatever the walk
    missed, and there is no way to tell from the number itself."""
    total = 0
    seen = 0
    stack = [str(path)]
    while stack:
        if seen >= budget:
            return {"bytes": total, "files": seen, "truncated": True}
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            # Counted per entry, not per directory: a corpus that keeps a million
            # files in one flat directory would otherwise consume the whole budget
            # in a single uninterruptible pass, which is the case the budget is for.
            if seen >= budget:
                return {"bytes": total, "files": seen, "truncated": True}
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                    seen += 1
            except OSError:
                continue
    return {"bytes": total, "files": seen, "truncated": False}


def _sum_sizes(parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Roll several walked subtrees into one. ``truncated`` is sticky: a total
    built from any partial part is itself a floor."""
    parts = list(parts)
    return {
        "bytes": sum(p.get("bytes", 0) for p in parts),
        "files": sum(p.get("files", 0) for p in parts),
        "truncated": any(p.get("truncated") for p in parts),
    }


def size_index(cache_root: Path) -> dict[str, dict[str, Any]]:
    """Sizes for the eval cache root and each dataset / home under it, in one pass.

    Every row on the page wants a size, and the roots overlap — the cache total
    contains ``homes/``, which contains every built corpus. Walked independently,
    a big subtree gets walked twice and the page load doubles for a number nobody
    reads twice. So the walk happens once per subtree here and the totals are
    summed rather than re-derived, which also guarantees the parts add up to the
    whole shown above them."""
    index: dict[str, dict[str, Any]] = {}
    children: list[dict[str, Any]] = []
    loose = {"bytes": 0, "files": 0, "truncated": False}
    try:
        top = list(os.scandir(cache_root))
    except OSError:
        return {str(cache_root): loose}
    for entry in top:
        if not entry.is_dir(follow_symlinks=False):
            try:
                loose["bytes"] += entry.stat().st_size
                loose["files"] += 1
            except OSError:
                pass
            continue
        if entry.name == "homes":
            homes: list[dict[str, Any]] = []
            try:
                for home in os.scandir(entry.path):
                    if home.is_dir(follow_symlinks=False):
                        index[home.path] = dir_bytes(Path(home.path))
                        homes.append(index[home.path])
            except OSError:
                pass
            index[entry.path] = _sum_sizes(homes)
        else:
            index[entry.path] = dir_bytes(Path(entry.path))
        children.append(index[entry.path])
    index[str(cache_root)] = _sum_sizes([*children, loose])
    return index


def _sized(path: Path, index: Optional[dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """A path's size, off the one-pass index when it is in it. A path outside the
    cache root — a download kept beside the operator's checkout — is walked on its
    own."""
    if index is not None and str(path) in index:
        return index[str(path)]
    return dir_bytes(path)


def _manifest(home: Path) -> dict[str, Any]:
    """A built home's ``snapshot.json``, or empty when it is not one."""
    try:
        blob = json.loads((home / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return blob if isinstance(blob, dict) else {}


#: The build markers a harness drops in the home it built, and where each keeps
#: its document count and whether it embedded. A stamped home answers both from
#: its snapshot manifest; the haystack corpora are built but never stamped, so
#: without this a real corpus reads as an empty one. Owned by the harnesses that
#: write them (``beir_eval``, ``haystack_corpus``) — read here, never written.
BUILD_MARKERS = (
    ("beir_build.json", "corpus_docs", "embedded"),
    ("haystack_corpus.json", "docs", "vectors"),
    ("mtrag_build.json", "corpus_docs", "embedded"),
    ("perltqa_build.json", "corpus_docs", "embedded"),
)


def _build_marker(home: Path) -> dict[str, Any]:
    """``{"docs", "embedded"}`` off whichever build marker the home carries, or
    empty. ``embedded`` normalizes a count and a flag to the same yes/no — what a
    reader wants from it is whether the semantic arm has anything to read."""
    for name, docs_key, embed_key in BUILD_MARKERS:
        try:
            blob = json.loads((home / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(blob, dict):
            return {"docs": blob.get(docs_key), "embedded": bool(blob.get(embed_key))}
    return {}


def _home_row(path: Path, *, label: str, sizes: Sizes = None) -> dict[str, Any]:
    """One built corpus home, as the page reads it. A home that is not there is
    still a row — "absent" is the answer to what is installed, and dropping it
    would make an unbuilt corpus indistinguishable from one nobody defined."""
    manifest = _manifest(path)
    row: dict[str, Any] = {
        "label": label,
        "path": str(path),
        "built": path.is_dir(),
        "snapshot_id": manifest.get("snapshot_id"),
        "counts": manifest.get("counts") or {},
        "embedding_space": manifest.get("embedding_space"),
        "created_at": manifest.get("created_at"),
    }
    if row["built"]:
        row.update(_sized(path, sizes))
        row["build"] = _build_marker(path)
    return row


def _workspace_row(path: Path, *, label: str, sizes: Sizes = None) -> dict[str, Any]:
    """A root holding *many* small homes (the per-question haystacks build one
    per question). There is no single snapshot to report, so the count of homes
    is what describes it."""
    row: dict[str, Any] = {"label": label, "path": str(path), "built": path.is_dir()}
    if row["built"]:
        try:
            row["homes"] = sum(1 for p in os.scandir(path) if p.is_dir())
        except OSError:
            row["homes"] = None
        row.update(_sized(path, sizes))
    return row


# ── benchmarks ────────────────────────────────────────────────────────────────

def _state(*, fresh: bool, corpus_built: bool, last: Optional[dict]) -> str:
    """A row's one-word state, in the order a reader needs it.

    ``missing`` outranks everything: a row whose corpus is not built cannot run
    at all, and reporting it as "stale" would suggest a re-run is what it needs.
    ``fresh`` means the ledger's numbers still describe what a run right now
    would measure — the same test :mod:`search_lab.benchmark` skips on."""
    if not corpus_built:
        return "missing"
    if fresh:
        return "fresh"
    return "stale" if last else "never-run"


def benchmarks() -> list[dict[str, Any]]:
    """Every row ``python -m search_lab benchmark`` knows, with its corpus state
    and its last recorded numbers.

    The manifest is the source: a row added there appears here. What this adds is
    the three facts the manifest cannot carry — whether the corpus is on this box,
    whether the ledger's numbers still describe the code as it sits in the working
    tree, and what those numbers were."""
    from search_lab import bench_runs, benchmark

    rows: list[dict[str, Any]] = []
    for row in benchmark.manifest():
        corpus = row.home
        corpus_id = row.corpus_id()
        last = bench_runs.latest_ok(row=row.name)
        fresh = bench_runs.is_fresh(last, corpus_id=corpus_id, code=row.code_id())
        # A haystack row has no single home (hundreds of per-question ones), so
        # "is the corpus built" has no path to test — the run itself is what says
        # so. Treat it as runnable rather than inventing a missing corpus.
        built = corpus is None or corpus.is_dir()
        rows.append({
            "name": row.name,
            "argv": [str(a) for a in row.argv],
            "corpus_home": str(corpus) if corpus is not None else None,
            "corpus_id": corpus_id,
            "corpus_built": built,
            "build_hint": row.build_hint,
            "cost_min": row.cost_min,
            "est_min": round(benchmark.estimate(row, last), 1),
            "fresh": fresh,
            "state": _state(fresh=fresh, corpus_built=built, last=last),
            "measure_keys": list(row.measure_keys),
            "code_id": row.code_id(),
            "last": _last_run(last),
        })
    return rows


def _last_run(record: Optional[dict]) -> Optional[dict[str, Any]]:
    """The reportable part of a ledger row: when, how long, and what it measured.
    The full record carries argv and a commit that the page has no use for."""
    if not record:
        return None
    return {
        "at": record.get("at"),
        "elapsed_s": record.get("elapsed_s"),
        "commit": record.get("commit"),
        "code_id": record.get("code_id"),
        "measures": record.get("measures") or {},
    }


#: How many ledger records one read returns. The ledger grows a row per benchmark
#: per tuning pass and is never pruned, so the read has to be bounded; the count
#: it was cut from travels with it (``total``) so a truncated history says so
#: rather than reading as the whole one.
RUNS_LIMIT = 500


def runs(*, row: Optional[str] = None, limit: int = RUNS_LIMIT,
         home: Optional[Path] = None) -> dict[str, Any]:
    """Every recorded benchmark run, newest first — the ledger itself, rather
    than the one-row-per-benchmark summary :func:`benchmarks` reports.

    That summary answers "what does the bench say *now*", and to answer it keeps
    only the newest successful run of each row still in the manifest. Three
    things it therefore cannot show, each of which is the whole record of
    something: the runs before the newest one (the history a delta is read
    against — a tuning pass's numbers are meaningless without the pass before
    it), the failures (a row that stopped being runnable at some configuration,
    which a summary of successes renders as a silent gap), and the rows that have
    since left the manifest, whose numbers were still measured on this box and
    have nowhere else to be read.

    Records come back as they were written. What is added is the three facts that
    take the present to establish: a stable id to address one by, whether its row
    is still on the bench, and whether the code it was measured under is the code
    in the working tree now.

    ``home`` overrides where the ledger is read from, as the recording side's
    does — the harnesses point it at a throwaway corpus, so the two ends have to
    agree on which home holds the history."""
    from search_lab import bench_runs, benchmark

    on_bench = {r.name: r for r in benchmark.manifest()}
    # Hashed once per row rather than once per record: every row of one harness
    # shares a code id, and the hash re-reads the whole ranking source.
    current_code = {name: r.code_id() for name, r in on_bench.items()}

    # Listed once rather than stat-ed per run: the answer is a directory read,
    # and asking it five hundred times is five hundred syscalls for one fact.
    try:
        kept = {p.stem for p in bench_runs.queries_dir(home).glob("*.json")}
    except OSError:
        kept = set()

    records = bench_runs.read_runs(home, row=row)
    reported: set[str] = set()
    out: list[dict[str, Any]] = []
    for record in records[:limit]:
        name = record.get("row") or ""
        bench_row = on_bench.get(name)
        measures = record.get("measures") or {}
        ok = record.get("status") == "ok"
        rid = bench_runs.run_id(record)
        out.append({
            **record,
            "id": rid,
            # Whether this run's per-query detail is still on disk. Bounded, so
            # an old run keeps its numbers and loses its detail — and the page
            # has to be able to say which, rather than offering a link to
            # nothing.
            "has_queries": rid in kept,
            "on_bench": bench_row is not None,
            # The newest successful run of its row: the one the summary above
            # reports, and the one a re-run would skip on if it is also fresh.
            "current": ok and name not in reported,
            # None, not False, for a row that has left the bench — there is no
            # current code id for a harness the manifest no longer names, and
            # saying "measured under different code" would invent one.
            "code_current": (record.get("code_id") == current_code[name]
                             if bench_row is not None else None),
            # The row's declared headline metrics, so a run reads with the same
            # numbers the summary shows it with. A row off the bench declares
            # nothing, so what it recorded is what there is to lead with.
            "measure_keys": list(bench_row.measure_keys) if bench_row is not None
            else [k for k in measures if k != "n"],
        })
        if ok:
            reported.add(name)
    return {
        "code_id": bench_runs.code_id(),
        "total": len(records),
        "returned": len(out),
        "runs": out,
    }


#: How many per-query rows one read returns. A row scores up to a couple of
#: thousand queries and nobody reads two thousand rows — what gets read is the
#: bad end, which is why the order below matters more than the cap.
QUERIES_LIMIT = 200


def _miss_key(row: dict[str, Any]) -> tuple:
    """Sort key putting the worst-served queries first, without knowing which
    metric a row is scored on.

    Ordered by what actually went wrong rather than by a score: a query whose
    gold document never came back at all is worse than one that ranked it 40th,
    which is worse than one that ranked it 3rd — and a score of 0.0 reports the
    first two identically. Fraction of gold found breaks ties, so a query that
    retrieved one of four answers sorts above one that retrieved all four."""
    rank = row.get("rank")
    n_gold, found = row.get("n_gold") or 0, row.get("found")
    fraction = (found / n_gold) if n_gold and isinstance(found, (int, float)) else 0.0
    # Not retrieved at all sorts first; then deepest rank first.
    return (0 if rank is None else 1, fraction, -(rank or 0))


def _lead_measure(rows: list[dict[str, Any]]) -> Optional[str]:
    """The metric a row is read on: the first its harness listed. Harnesses put
    their headline first, and picking by name here would mean maintaining a catalog
    of every metric any harness reports."""
    for row in rows:
        for key, value in (row.get("measures") or {}).items():
            if isinstance(value, (int, float)):
                return key
    return None


def queries(run_id: str, *, vs: Optional[str] = None, limit: int = QUERIES_LIMIT,
            home: Optional[Path] = None) -> dict[str, Any]:
    """One run's per-query detail, worst first — or, given ``vs``, the queries
    that moved between two runs, biggest regression first.

    The comparison is the reason this is kept at all. Two configurations whose
    aggregate differs by 0.004 have usually not moved a little on every query;
    they have moved a lot on a few, and *which* few is the entire content of the
    change. An aggregate cannot say it, and a re-run cannot recover it, because
    the earlier configuration is gone.

    Joined on query id, and only over queries both runs scored — a query one run
    did not see has not regressed, and reporting it as a fall from nothing would
    put the loudest rows in the table on the queries that moved least."""
    from search_lab import bench_runs

    rows = bench_runs.read_queries(run_id, home=home)
    lead = _lead_measure(rows)
    misses = sum(1 for r in rows if r.get("rank") is None)
    order = "worst"
    compared: Optional[str] = None

    if vs:
        before = {r["qid"]: r for r in bench_runs.read_queries(vs, home=home)}
        if before and lead:
            joined = []
            for row in rows:
                was = before.get(row.get("qid", ""))
                if was is None:
                    continue
                now_v = (row.get("measures") or {}).get(lead)
                was_v = (was.get("measures") or {}).get(lead)
                if not isinstance(now_v, (int, float)) or not isinstance(
                        was_v, (int, float)):
                    continue
                joined.append({**row, "before": {
                    "measures": was.get("measures") or {},
                    "rank": was.get("rank"),
                    "latency_ms": was.get("latency_ms"),
                }, "moved": round(now_v - was_v, 4)})
            # Only the queries that actually moved: a table led by hundreds of
            # unchanged rows buries the handful that carry the change.
            rows = sorted((r for r in joined if r["moved"]),
                          key=lambda r: r["moved"])
            order, compared = "moved", vs

    if order == "worst":
        rows = sorted(rows, key=_miss_key)
    return {
        "run_id": run_id,
        "compared_to": compared,
        "order": order,
        "lead": lead,
        "total": len(rows),
        "misses": misses,
        "returned": min(len(rows), limit),
        "rows": rows[:limit],
    }


# ── datasets ──────────────────────────────────────────────────────────────────

def _beir_datasets(cache_root: Path, on_bench: dict[str, list[str]],
                   sizes: Sizes = None) -> list[dict]:
    """Every BEIR set ``beir_eval`` carries a published reference for — the ones
    it can run with a ``--dataset`` value and nothing else. Downloaded or not:
    the un-downloaded ones are exactly the "available" half of the question, and
    their published references are what says whether a row is worth the embed."""
    from search_lab import beir_eval

    rows = []
    for name, reference in sorted(beir_eval.REFERENCE.items()):
        download = cache_root / name
        rows.append({
            "name": name,
            "family": "beir",
            "harness": f"search_lab/beir_eval.py --dataset {name}",
            "download": _download_row(
                download, present=(download / "corpus.jsonl").is_file(), sizes=sizes),
            "homes": [_home_row(cache_root / "homes" / name, label="corpus",
                                sizes=sizes)],
            "reference": {"metric": "nDCG@10", **reference},
            "on_bench": on_bench.get(name, []),
        })
    return rows


def _download_row(path: Path, *, present: bool, sizes: Sizes = None) -> dict[str, Any]:
    """A raw dataset download. ``present`` is the harness's own readiness test
    (BEIR wants its corpus/queries pair; a cloned repo wants its data subdir),
    passed in rather than guessed at from the directory existing — a half-failed
    unzip leaves the directory behind."""
    row: dict[str, Any] = {"path": str(path), "present": present}
    if present:
        row.update(_sized(path, sizes))
    return row


def datasets(sizes: Sizes = None) -> list[dict[str, Any]]:
    """Every corpus the lab knows how to build, downloaded or not.

    Four families, and the shape of a row differs by what the family builds: BEIR
    and CDR each build one home, the haystacks build a registered corpus *and* a
    root of per-question micro-homes. Which benchmark rows a dataset feeds is
    filled in from the manifest, so the two lists on the page agree by
    construction."""
    from search_lab import cdr_eval, eval_home, haystack_eval

    cache_root = eval_home.CACHE_ROOT
    homes = cache_root / "homes"
    on_bench = _bench_index()
    if sizes is None:
        sizes = size_index(cache_root)

    rows: list[dict[str, Any]] = _beir_datasets(cache_root, on_bench, sizes)

    cdr_repo = cdr_eval.DEFAULT_REPO
    rows.append({
        "name": "cdr",
        "family": "cdr",
        "harness": "search_lab/cdr_eval.py",
        "download": _download_row(
            cdr_repo, present=(cdr_repo / "cdr_benchmark_data").is_dir(), sizes=sizes),
        "homes": [_home_row(homes / "cdr", label="corpus", sizes=sizes)],
        "reference": {"metric": "nDCG@10", "best_model": cdr_eval.BEST_MODEL_NDCG10},
        "on_bench": on_bench.get("cdr", []),
    })

    for name, reference in haystack_eval.REFERENCE.items():
        download = cache_root / name
        rows.append({
            "name": name,
            "family": "haystack",
            "harness": f"search_lab/haystack_eval.py --dataset {name}",
            "download": _download_row(download, present=download.is_dir(),
                                      sizes=sizes),
            "homes": [
                _home_row(homes / f"hay-{name}", label="corpus", sizes=sizes),
                _workspace_row(homes / name, label="per-question homes",
                               sizes=sizes),
            ],
            "reference": reference,
            "on_bench": on_bench.get(name, []),
        })

    rows.append(_mtrag_dataset(cache_root, homes, on_bench, sizes))
    rows.append(_perltqa_dataset(cache_root, homes, on_bench, sizes))
    return rows


def _mtrag_dataset(cache_root: Path, homes: Path, on_bench: dict[str, list[str]],
                   sizes: Sizes = None) -> dict[str, Any]:
    """The only multi-corpus row on the page.

    MTRAG retrieves from four separate corpora and reports the macro-average over
    them, so it builds four homes and is only comparable to its published table
    when all four are present — which makes "which of them are built" the fact a
    reader needs, rather than one aggregate yes/no."""
    from search_lab import mtrag_eval

    download = cache_root / "mtrag"
    return {
        "name": "mtrag",
        "family": "mtrag",
        "harness": "search_lab/mtrag_eval.py",
        "source": "https://github.com/IBM/mt-rag-benchmark",
        "download": _download_row(
            download, present=(download / "corpora").is_dir(), sizes=sizes),
        "homes": [_home_row(homes / f"mtrag-{domain}", label=domain, sizes=sizes)
                  for domain in mtrag_eval.DOMAINS],
        "reference": {"metric": "nDCG@10 (4-domain macro)",
                      **{f"{form}_{system}": value
                         for form, systems in mtrag_eval.REFERENCE.items()
                         for system, value in systems.items()}},
        "on_bench": on_bench.get("mtrag", []),
    }


def _perltqa_dataset(cache_root: Path, homes: Path, on_bench: dict[str, list[str]],
                     sizes: Sizes = None) -> dict[str, Any]:
    """Personal long-term memory, retrieved by memory unit.

    Its ``reference`` is deliberately empty: the paper reports a memory-retrieval
    subtask but not in a form this run reproduces, and a borrowed number would
    read as a comparison it is not."""
    download = cache_root / "perltqa"
    return {
        "name": "perltqa",
        "family": "memory-units",
        "harness": "search_lab/perltqa_eval.py",
        "source": "https://github.com/Elvin-Yiming-Du/PerLTQA",
        "download": _download_row(
            download, present=(download / "perltmem_en.json").is_file(), sizes=sizes),
        "homes": [_home_row(homes / "perltqa", label="corpus", sizes=sizes)],
        "reference": {},
        "on_bench": on_bench.get("perltqa", []),
    }


def _bench_index() -> dict[str, list[str]]:
    """Dataset name → the benchmark rows that run on it, from each row's own
    :meth:`~search_lab.benchmark.Row.dataset_name` — read off the row name
    (``beir:scifact[vectors]`` → ``scifact``) unless the row declares it. So a row
    added to the manifest joins its dataset without an edit here."""
    from search_lab import benchmark

    index: dict[str, list[str]] = {}
    for row in benchmark.manifest():
        index.setdefault(row.dataset_name(), []).append(row.name)
    return index


# ── the whole thing ───────────────────────────────────────────────────────────

def inventory() -> dict[str, Any]:
    """The bench's whole inventory, as the ``/lab`` page reads it."""
    from search_lab import bench_runs, eval_home

    cache_root = eval_home.CACHE_ROOT
    # One walk, shared: the cache total and every dataset row read the same index,
    # so the sizes on the page add up and the 25 GB corpus is visited once.
    sizes = size_index(cache_root)
    return {
        "cache_root": str(cache_root),
        "cache": sizes.get(str(cache_root), {"bytes": 0, "files": 0, "truncated": False}),
        "code_id": bench_runs.code_id(),
        "families": FAMILIES,
        "benchmarks": benchmarks(),
        "datasets": datasets(sizes),
    }


# ── the terminal read ─────────────────────────────────────────────────────────

def _gb(n: Optional[int]) -> str:
    if not n:
        return "—"
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def _lines(inv: dict[str, Any]) -> Iterable[str]:
    yield f"bench inventory   code {inv['code_id']}   {inv['cache_root']} " \
          f"({_gb(inv['cache'].get('bytes'))})"

    yield "\nbenchmarks"
    for row in inv["benchmarks"]:
        measures = (row.get("last") or {}).get("measures") or {}
        shown = "  ".join(f"{k} {measures[k]:.3f}" for k in row["measure_keys"]
                          if isinstance(measures.get(k), (int, float)))
        yield f"  {row['name']:<28}{row['state']:<11}" \
              f"~{row['est_min']:>4.0f} min   {shown}"

    yield "\ndatasets"
    for row in inv["datasets"]:
        built = [h for h in row["homes"] if h.get("built")]
        size = sum(h.get("bytes", 0) for h in built) or row["download"].get("bytes", 0)
        counts = next((h.get("counts") for h in built if h.get("counts")), {})
        marker = next((h.get("build") for h in built if h.get("build")), {})
        where = ("built" if built else
                 "downloaded" if row["download"].get("present") else "available")
        if counts:
            detail = (f"{counts.get('threads', 0):,} threads  "
                      f"{counts.get('vectors', 0):,} vectors")
        elif marker:
            detail = (f"{marker.get('docs') or 0:,} docs"
                      f"{'  embedded' if marker.get('embedded') else ''}")
        else:
            detail = ""
        yield f"  {row['name']:<20}{row['family']:<16}{where:<12}" \
              f"{_gb(size):>9}   {detail}"


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print the payload as JSON")
    args = ap.parse_args(argv)

    inv = inventory()
    if args.json:
        print(json.dumps(inv, indent=1, default=str))
        return 0
    for line in _lines(inv):
        print(line)
    return 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    raise SystemExit(main(sys.argv[1:]))
