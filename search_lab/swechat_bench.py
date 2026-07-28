#!/usr/bin/env python3
"""Turn mined gold into a benchmark other people can run.

A case file is keyed by **our** thread ids — ULIDs this archive minted at
ingest, which name nothing in anyone else's copy of SWE-chat. That is the single
thing standing between the gold under ``swe-chat-data/gold/`` and a benchmark: the
queries are good, the judgments are good, the identifiers are local. This rewrites
them onto the dataset's own ``session_id`` and emits the four files a retrieval
benchmark is made of, none of which mention this archive:

- ``queries.jsonl`` — ``{query_id, text, ...}``. The query text and nothing that
  hints at the answer.
- ``qrels.txt`` — TREC qrels, ``query_id 0 session_id grade``, grades 0/1/2. The
  whole graded pool, not only the relevant rows: nDCG's ideal ranking comes from
  the pool, so dropping the 0s would silently change the metric.
- ``corpus.jsonl`` — every session in scope, ``{session_id, repo}``. A system may
  return these ids and no others. Text is not copied: the dataset is 12 GB and
  ODC-BY, so the corpus is *pinned by id* against a dataset revision instead.
- ``manifest.json`` — the revision, the selection rule, the harness bound, the
  metric contract, and the counts. What makes a rebuild reproducible.

Ids are content-addressed (``sha256`` over protocol, scope and query text), so
re-exporting the same case yields the same ``query_id`` and runs stay comparable
across regenerations. A *different* query takes a different id, which
is the honest behaviour — it is a different query.

``run`` is the other half: it drives a ranker over ``queries.jsonl`` and writes a
TREC run file, which is what a scorer consumes. Two rankers ship as published
baselines (``stack``, the archive's own fused pipeline; ``bm25``, FTS5's ``bm25()``
alone), plus ``lexical`` for the ablation rung between them. Anyone else's system
produces the same file shape from ``queries.jsonl`` without touching this code —
that is the point of exporting a format rather than an ``evaluate()`` call.

The scoring contract is deliberately *not* here. It lives with the published
files (``bench/score.py``), dependency-free, so running the benchmark needs no
part of this archive.

Read-only against the corpus home. ``export`` needs the home only for the
thread -> session id map::

    THREAD_ARCHIVE_HOME=<corpus> python search_lab/swechat_bench.py export \\
        --gold ~/dev/swe-chat-data/gold --out ~/dev/swe-chat-data/bench
    THREAD_ARCHIVE_HOME=<corpus> python search_lab/swechat_bench.py run \\
        --bench ~/dev/swe-chat-data/bench --ranker bm25
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Every metric this benchmark reports, named as the ir_measures expression that
# defines it. MRR, success and recall count only grade 2 (the mined gold) as
# relevant; grade 1 is a near-miss the judge kept in the pool, and it earns
# partial credit in nDCG alone. The first three reproduce exactly under
# `ir_measures` or `trec_eval`, which threshold at rel=2 the same way.
#
# nDCG does not, and the manifest says so: the reference scorer uses exponential
# gain (`2**rel - 1`), where trec_eval's `ndcg` — and so pytrec_eval and
# ir_measures on top of it — uses linear gain. On a pool holding grade-1 rows the
# two disagree, so the benchmark ships its own scorer (`bench/score.py`) and names
# the gain function rather than deferring to whichever library a consumer has
# installed. Exponential gain is the archive's own convention
# (`search_lab.eval_core.ndcg_at_k`), which is what makes an exported number and
# an in-lab number the same measurement.
MEASURES = ["RR(rel=2)", "Success(rel=2)@10", "R(rel=2)@10", "nDCG@10 (exponential gain)"]
# Depth every run is truncated to. The metrics do not look past 20, but a run
# carrying rank 35 would still score under RR where ours scores zero, so the
# depth is part of the contract rather than an implementation detail.
RUN_DEPTH = 20
# Cutoffs the reference scorer reports at, matching `eval_core.RECALL_KS`.
KS = (1, 5, 10, 20)
# A TREC run's score column is higher-is-better, and a conforming scorer sorts by
# it rather than trusting the rank column. FTS5's `bm25()` is the other way round
# (more negative = better match), so that ranker's scores are negated on the way
# out — without it the baseline's run reads as its own exact reverse.
_SCORE_SIGN = {"bm25": -1.0}

# Short, readable id prefixes per protocol. An unmapped protocol falls back to its
# own first two characters, so an export never fails on a case shape this table
# has not been taught — it just gets a less readable prefix.
_PREFIX = {"commit-linked": "cm"}


def query_id(case: dict) -> str:
    """Stable id for a case: a prefix naming its protocol plus a digest over
    protocol, scope and query text.

    Scope (the repository or topic the case was mined within) is in the digest
    because the same question can be asked of two repositories and mean two
    different things — without it those collide into one id and one of the two
    judgment sets is silently lost."""
    protocol = case.get("protocol") or "unknown"
    scope = case.get("topic") or case.get("repo") or ""
    digest = hashlib.sha256(
        f"{protocol}\n{scope}\n{case['query']}".encode()).hexdigest()[:10]
    return f"{_PREFIX.get(protocol, protocol[:2])}-{digest}"


def load_gold(gold_dir: Path) -> list[dict]:
    """Every mined case under ``gold_dir``, newest-sorted by filename.

    Globbed rather than listed: gold accumulates a file at a time (one per topic
    mined), and an export that needed editing to see a new file would fall behind
    the corpus it describes. Which files are cases — and which are audit
    sidecars beside them — is ``search_lab.gold_files``' call."""
    from gold_files import discover
    cases = []
    for path in discover(gold_dir):
        for line in path.read_text().splitlines():
            if line.strip():
                case = json.loads(line)
                case["_source_file"] = path.name
                cases.append(case)
    return cases


def session_map() -> dict[str, str]:
    """``{thread_id: session_id}`` for the corpus home — the whole translation.

    ``swechat_corpus.build_home`` imports each transcript with ``source_id`` set
    to its stem, and the stem is the dataset's ``session_id`` verbatim, so this is
    a lookup and not a transformation. (The dataset uses two id shapes, a bare
    uuid and a date-prefixed one; both are carried through unchanged.)"""
    from sqlalchemy import text as sa_text

    from thread_archive._store import use_session

    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT id, source_id FROM threads "
            "WHERE thread_type = 'conversation' AND source_id IS NOT NULL")).all()
    return {str(tid): str(sid) for tid, sid in rows}


def dataset_revision(data: Path) -> str | None:
    """The Hugging Face commit the download came from, read out of the hub's
    per-file cache metadata (first line). ``None`` if the download is gone or was
    not fetched by ``hf download`` — the manifest records that honestly rather
    than inventing a revision."""
    cache = data / ".cache" / "huggingface" / "download"
    for meta in sorted(cache.glob("*.metadata")):
        head = meta.read_text().splitlines()
        if head:
            return head[0].strip()
    return None


def export(gold_dir: Path, out: Path, *, data: Path) -> dict:
    """Rewrite every mined case onto session ids and write the benchmark files.

    Cases whose gold does not resolve in this home are dropped with a report
    rather than exported half-translated: a qrels row naming a document outside
    the corpus scores as an unfindable gold for every system equally, which looks
    like a hard query instead of a broken export."""
    from snapshot import read_snapshot_id

    to_session = session_map()
    cases = load_gold(gold_dir)
    if not cases:
        raise SystemExit(f"no case files under {gold_dir}")

    snapshots = {c.get("snapshot_id") for c in cases}
    current = read_snapshot_id()
    if snapshots != {current}:
        raise SystemExit(
            f"gold under {gold_dir} was mined against snapshot(s) "
            f"{sorted(s for s in snapshots if s)}, but this home is {current} — "
            "export from the snapshot the cases were mined against.")

    queries: list[dict] = []
    qrels: list[tuple[str, str, int]] = []
    seen: dict[str, str] = {}
    dropped: list[str] = []

    for case in cases:
        if case.get("sessions"):
            # The scoring loop skips these ids before ranking; TREC qrels has no
            # way to say "ignore this document for this query", so exporting one
            # would quietly change what is being measured.
            raise SystemExit(
                f"case {case['query']!r} carries a session skip-list, which the "
                "exported format cannot express")
        grades = {t: int(g) for t, g in (case.get("grades") or {}).items()}
        for t in case["gold"]:
            grades.setdefault(t, 2)
        unknown = [t for t in grades if t not in to_session]
        if unknown:
            dropped.append(f"{case['_source_file']}: {case['query'][:60]!r} "
                           f"({len(unknown)} unresolved thread id(s))")
            continue

        qid = query_id(case)
        if qid in seen and seen[qid] != case["query"]:
            raise SystemExit(f"query id collision on {qid}: "
                             f"{seen[qid]!r} vs {case['query']!r}")
        seen[qid] = case["query"]
        queries.append({
            "query_id": qid,
            "text": case["query"],
            "protocol": case.get("protocol"),
            "difficulty": case.get("difficulty"),
            # Kept apart rather than folded into one "scope": a topic is not
            # always a repository. Repo-derived topics exist, but so do
            # cross-cutting ones ("Styling and CSS"), and calling those a repo
            # would put a subject name in a field consumers group by.
            "repo": case.get("repo"),
            "topic": case.get("topic"),
            # Filled in below for topic cases: a repository topic's confounds are
            # separable by file and module vocabulary, a cross-cutting subject's
            # are not, so they are different difficulties wearing one protocol
            # name and a consumer has to be able to split them.
            "topic_kind": None,
            # What the case meant by the query, where it wrote one down. This is
            # the relevance criterion, published so a judgment can be argued with
            # — it is not part of the query, and a system that reads it is
            # searching with information the benchmark does not grant.
            "relevance_note": case.get("intent"),
            "source_file": case["_source_file"],
        })
        for thread_id, grade in sorted(grades.items()):
            qrels.append((qid, to_session[thread_id], grade))

    corpus = corpus_rows(gold_dir, to_session)
    repos = repo_names(gold_dir)
    for q in queries:
        if q["topic"] is not None:
            q["topic_kind"] = "repository" if q["topic"] in repos else "subject"
    scoped = {r["session_id"] for r in corpus}
    stray = {s for _, s, _ in qrels} - scoped
    if stray:
        raise SystemExit(f"{len(stray)} judged session(s) fall outside the pinned "
                         f"corpus, e.g. {sorted(stray)[:3]}")

    out.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out / "queries.jsonl", queries)
    (out / "qrels.txt").write_text(
        "".join(f"{q} 0 {d} {g}\n" for q, d, g in qrels))
    _write_jsonl(out / "corpus.jsonl", corpus)

    by_protocol: dict[str, int] = {}
    for q in queries:
        by_protocol[q["protocol"] or "unknown"] = by_protocol.get(
            q["protocol"] or "unknown", 0) + 1
    manifest = {
        "kind": "thread-retrieval-benchmark",
        "format_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_id": current,
        "dataset": {
            "name": "SALT-NLP/SWE-chat",
            "url": "https://huggingface.co/datasets/SALT-NLP/SWE-chat",
            "revision": dataset_revision(data),
            "license": "ODC-BY-1.0",
            "paper": "arXiv:2604.20779",
        },
        "corpus": {
            "unit": "session",
            "documents": len(corpus),
            "repositories": len({r["repo"] for r in corpus}),
            "selection": "every SWE-chat transcript the line-stream importer can "
                         "read; no sampling, no per-repository cap",
            "harness_bound": "Claude Code sessions, and the sessions other "
                             "harnesses wrote in Claude Code's line-delimited "
                             "shape. Excluded: OpenCode and most Gemini CLI "
                             "(pretty-printed JSON), Cursor, and Codex "
                             "(line-delimited but a different envelope). Archive's "
                             "OpenCode and Cursor importers are DB scanners with "
                             "no JSON-export path, so those have no route in",
        },
        "queries": {"total": len(queries), "by_protocol": by_protocol},
        "scoring": {
            "measures": MEASURES,
            "run_depth": RUN_DEPTH,
            "relevant_grade": 2,
            "ndcg_gain": "exponential (2**rel - 1); trec_eval's ndcg uses linear "
                         "gain and disagrees on pools holding grade 1",
            "reference_scorer": "bench/score.py (stdlib only, ships with these files)",
            "note": "grade 1 is a near-miss: partial credit under nDCG, not "
                    "counted by RR / Success / R",
        },
        "dropped_cases": dropped,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")

    for d in dropped:
        print(f"  ! dropped {d}")
    print(f"exported {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}, "
          f"{len(qrels)} judgment(s) over {len(corpus)} session(s) -> {out}")
    for protocol, n in sorted(by_protocol.items()):
        print(f"      {protocol:<14s} {n:>4d}")
    return manifest


def repo_names(gold_dir: Path) -> set[str]:
    """The repository names the corpus grouping knows. A mined topic whose title is
    one of these is a repository; anything else is a cross-cutting subject."""
    groups = gold_dir / "repo-groups.json"
    return set(json.loads(groups.read_text())) if groups.exists() else set()


def corpus_rows(gold_dir: Path, to_session: dict[str, str]) -> list[dict]:
    """The pinned corpus: every session in the home, tagged with its repository.

    Built from the home rather than from ``repo-groups.json`` so it names what was
    actually indexed; the groups file supplies the repository labels, and a
    session missing from it is still in the corpus, just unlabelled."""
    groups_path = gold_dir / "repo-groups.json"
    repo_of: dict[str, str] = {}
    if groups_path.exists():
        for repo, members in json.loads(groups_path.read_text()).items():
            for m in members:
                repo_of[str(m)] = repo
    return sorted(({"session_id": s, "repo": repo_of.get(s)}
                   for s in to_session.values()),
                  key=lambda r: r["session_id"])


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def emit_run(bench: Path, out: Path, *, ranker: str, tag: str, depth: int) -> int:
    """Rank every query with ``ranker`` and write a TREC run file.

    ``qid Q0 session_id rank score tag``, ranks 1-based and dense. A query with no
    hits contributes no rows, which is how TREC spells "returned nothing" — the
    scorer counts it as a miss rather than skipping it.

    A ranker that publishes a score gets its own, oriented so higher is better. The
    fused stack does not: it exposes the per-arm signals it combined (``_lex``,
    ``_semantic``, ``_rrf``) but its answer is an *ordered list*, not a number per
    hit. Those rows carry a rank-derived score, which is what the run format is
    entitled to claim — inventing a fused score would read as a comparable
    magnitude and be one only by accident."""
    import eval_home
    from snapshot import read_snapshot_id

    from thread_archive import _api as api

    manifest = json.loads((bench / "manifest.json").read_text())
    if manifest.get("snapshot_id") != read_snapshot_id():
        raise SystemExit(
            f"{bench} was exported from snapshot {manifest.get('snapshot_id')}, "
            f"but this home is {read_snapshot_id()}")

    if ranker == "lexical":
        # The product's own switches, set before the archive opens: both model arms
        # report unavailable and the coherence re-rank stands down, so what is
        # measured is the lexical-only deployment a core install runs — the same
        # arm pinning every other harness's `lexical` row means.
        eval_home.pin_arms(vectors=False)
    api.open_archive()

    if ranker == "bm25":
        from bm25_baseline import bm25_search as search
    else:
        search = api.search
        # Hold the ranker still before the first query: the coherence re-rank
        # otherwise lands partway through and splits a run in two.
        eval_home.warm()

    to_session = session_map()
    queries = [json.loads(line) for line in (bench / "queries.jsonl").read_text().splitlines()
               if line.strip()]

    def ranked(text: str) -> list[dict]:
        return search(text, limit=depth, content_types=None,
                      exclude_content_types=None)

    lines, missing = run_lines(queries, ranked, to_session,
                               tag=tag, depth=depth,
                               sign=_SCORE_SIGN.get(ranker, 1.0))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(lines))
    print(f"{tag}: {len(lines)} row(s) over {len(queries)} queries -> {out}"
          + (f"   ({missing} hit(s) outside the corpus, dropped)" if missing else ""))
    return 0


def run_lines(queries: list[dict], ranked, to_session: dict[str, str], *,
              tag: str, depth: int, sign: float) -> tuple[list[str], int]:
    """The TREC run rows for ``queries``, plus the count of hits dropped for
    naming a thread outside the corpus.

    Ranks are assigned **after** the corpus filter, so they stay dense and 1-based
    even when a ranker returns something unexportable — a gap in the rank column
    is a malformed run, and a scorer that trusts ranks would read the gap as a
    document it was never given."""
    lines, missing = [], 0
    for q in queries:
        rank = 0
        for hit in ranked(q["text"])[:depth]:
            session = to_session.get(hit["thread_id"])
            if session is None:
                missing += 1
                continue
            rank += 1
            score = sign * float(hit["score"]) if "score" in hit else float(-rank)
            lines.append(f"{q['query_id']} Q0 {session} {rank} {score:.6f} {tag}\n")
    return lines, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export", help="mined gold -> queries/qrels/corpus/manifest")
    ex.add_argument("--gold", type=Path, required=True, metavar="DIR",
                    help="directory of mined *-cases*.jsonl files")
    ex.add_argument("--out", type=Path, required=True, metavar="DIR",
                    help="directory to write the benchmark files into")
    ex.add_argument("--data", type=Path, metavar="DIR",
                    help="the SWE-chat download, for its revision "
                         "(default: <gold>/../swe-chat)")

    rn = sub.add_parser("run", help="rank the queries and write a TREC run file")
    rn.add_argument("--bench", type=Path, required=True, metavar="DIR",
                    help="the exported benchmark directory")
    rn.add_argument("--ranker", default="stack",
                    choices=("stack", "bm25", "lexical"),
                    help="stack: the archive's fused pipeline (default); "
                         "bm25: FTS5 bm25() alone; lexical: the stack with both "
                         "model arms switched off")
    rn.add_argument("--out", type=Path, metavar="FILE",
                    help="run file (default: <bench>/runs/<ranker>.run)")
    rn.add_argument("--tag", help="run tag recorded in column 6 (default: ranker)")
    rn.add_argument("--depth", type=int, default=RUN_DEPTH,
                    help=f"results per query (default {RUN_DEPTH}, the contract)")
    args = ap.parse_args(argv)

    if args.cmd == "export":
        from thread_archive import _api as api
        api.open_archive()
        export(args.gold, args.out, data=args.data or args.gold.parent / "swe-chat")
        return 0

    tag = args.tag or args.ranker
    out = args.out or args.bench / "runs" / f"{args.ranker}.run"
    return emit_run(args.bench, out, ranker=args.ranker, tag=tag, depth=args.depth)


if __name__ == "__main__":
    raise SystemExit(main())
