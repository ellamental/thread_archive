#!/usr/bin/env python3
"""Build a haystack memory benchmark as one consolidated corpus home.

``haystack_eval.py`` *scores* LoCoMo and LongMemEval per question: each question
carries its own small conversation history and the task is to pull the evidence
out of *that* history, so the harness builds a throwaway home per question and
retrieves inside it. That shape is what the metric needs and it is useless as an
archive — the corpus lands as hundreds of fingerprint-named micro homes, none of
which is a thing you can open, search, or point a second stack at.

This builds the other thing: the whole dataset as **one ordinary archive home**,
a sibling of ``homes/cdr`` and ``homes/swe-chat``. What that buys is a benchmark
corpus you can *operate* — open it in the viewer, run ``thread_search`` against
it, watch its load in the health page, compare its index against the real
archive's. The per-question homes stay unregistered workspace; this one registers
as a ``benchmark`` archive.

Doc granularity matches what ``haystack_eval`` retrieves, so a number measured
here is about the same units the scored harness ranks:

- **locomo** — one thread per turn, id ``<sample_id>:<dia_id>``. The ``dia_id``
  restarts at ``D1:1`` in every conversation, so the conversation id has to
  namespace it or ten conversations collapse onto each other.
- **longmemeval** — one thread per haystack session, id = the session id, text =
  its user-side turns. The 500 questions draw from a shared session pool, so the
  same session id recurs across questions; it is ingested once (the pool is
  consistent — a repeated id always carries the same text).

Usage::

    python search_lab/haystack_corpus.py --dataset locomo --vectors
    python search_lab/haystack_corpus.py --dataset longmemeval --vectors
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
# The lab dir too, so bare sibling imports (eval_home, eval_core, …) resolve
# however this file was loaded: as a script, by path, or as search_lab.X.
sys.path.insert(0, str(_HERE))

import eval_home  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("haystack_eval", _HERE / "haystack_eval.py")
haystack_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("haystack_eval", haystack_eval)
_SPEC.loader.exec_module(haystack_eval)

_session_lines = haystack_eval._session_lines
_log = haystack_eval._log
_CACHE = haystack_eval._CACHE

#: Written into the built home so the corpus is self-describing: which dataset it
#: is, how big, when it was built, and the ``thread_id -> doc_id`` map anything
#: scoring against it needs to translate a hit back to a benchmark id.
MARKER = "haystack_corpus.json"


def collect(dataset: str, args) -> dict[str, str]:
    """The whole dataset flattened to ``{doc_id: text}``.

    Reuses ``haystack_eval``'s loaders, so the corpus here is the same text the
    scored harness retrieves over — one flattened union of it rather than one
    corpus per question."""
    if dataset == "locomo":
        repo = Path(args.locomo_repo).expanduser()
        if not (repo / "data" / "locomo10.json").exists():
            raise SystemExit(f"locomo data not found under {repo}; "
                             "clone snap-research/locomo there")
        return {f"{gid}:{did}": text
                for gid, corpus, _ in haystack_eval.locomo_groups(repo)
                for did, text in corpus.items()}

    path = Path(args.longmemeval_file).expanduser()
    if not path.exists():
        raise SystemExit(f"longmemeval file not found at {path}; "
                         "download longmemeval_s_cleaned.json there")
    out: dict[str, str] = {}
    for _gid, corpus, _ in haystack_eval.longmemeval_groups(path):
        for did, text in corpus.items():
            out.setdefault(did, text)  # shared session pool: first wins, they agree
    return out


def build_home(dataset: str, corpus: dict[str, str], home: Path, *,
               vectors: bool, fresh: bool) -> dict[str, str]:
    """Ingest every doc into ``home`` as its own thread; embed when asked. Returns
    the ``thread_id -> doc_id`` map, also persisted in the marker."""
    import logging

    from thread_archive import _api as api
    from thread_archive._ops.load_runs import load_run

    # Every doc is a first-time import under its own source id, so the importer's
    # per-source cursor bookkeeping has nothing useful to say.
    logging.getLogger("thread_archive._importers._cursor").setLevel(logging.ERROR)

    if fresh:
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    os.environ["THREAD_ARCHIVE_HOME"] = str(home)
    api.open_archive(str(home))

    work = home / "_ingest"
    work.mkdir(parents=True, exist_ok=True)

    doc_of_thread: dict[str, str] = {}
    n = 0
    t0 = time.monotonic()
    with load_run("import", home=home, note=f"{dataset} corpus build") as run:
        with run.phase("import", total=len(corpus)) as ph:
            for doc_id, text in corpus.items():
                f = work / f"doc-{n}.jsonl"
                f.write_text(
                    "\n".join(json.dumps(x) for x in _session_lines(doc_id, text)) + "\n",
                    encoding="utf-8",
                )
                res = api.import_path(f, source_id=f"hay-{doc_id}")
                f.unlink()
                doc_of_thread[str(res.thread_id)] = doc_id
                n += 1
                ph.advance()
                if n % 1000 == 0:
                    _log(f"  ingested {n}/{len(corpus)} "
                         f"({n / (time.monotonic() - t0):.0f}/s)")
    shutil.rmtree(work, ignore_errors=True)
    _log(f"ingested {n} docs in {time.monotonic() - t0:.0f}s")

    if vectors:
        _log("embedding (slow)...")
        api.embed()
    return doc_of_thread


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", choices=["locomo", "longmemeval"], required=True)
    ap.add_argument("--locomo-repo", default=str(_CACHE / "locomo" / "repo"))
    ap.add_argument("--longmemeval-file",
                    default=str(_CACHE / "longmemeval" / "data" / "longmemeval_s_cleaned.json"))
    ap.add_argument("--home", type=Path, default=None,
                    help="corpus home (default <data-dir>/homes/hay-<dataset>)")
    ap.add_argument("--data-dir", default=str(_CACHE))
    ap.add_argument("--vectors", action="store_true",
                    help="embed after ingest (needed for the semantic arms)")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the home and rebuild from scratch")
    args = ap.parse_args(argv)

    home = eval_home.guard_home(
        args.home or Path(args.data_dir).expanduser() / "homes" / f"hay-{args.dataset}",
        what=f"{args.dataset} corpus home")

    # No rerank arm here: this builds a corpus, it does not score one.
    eval_home.pin_arms(vectors=args.vectors, rerank="off")

    corpus = collect(args.dataset, args)
    _log(f"{args.dataset}: {len(corpus)} docs -> {home}")
    doc_of_thread = build_home(args.dataset, corpus, home,
                               vectors=args.vectors, fresh=args.fresh)

    (home / MARKER).write_text(json.dumps({
        "benchmark": args.dataset,
        "docs": len(doc_of_thread),
        "vectors": args.vectors,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "doc_of_thread": doc_of_thread,
    }, indent=2), encoding="utf-8")

    _log(f"built {home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
