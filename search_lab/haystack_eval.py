"""Haystack retrieval eval — per-question conversational retrieval over LoCoMo and
LongMemEval, scored against their published recall baselines.

BEIR (``beir_eval.py``) and CDR (``cdr_eval.py``) both retrieve from one *shared*
corpus. These two memory benchmarks are a different shape: **per-question haystack
retrieval** — each question carries its own small conversation history, and the
task is to pull the evidence turn(s)/session(s) out of *that* history. So this
harness builds a small archive per corpus, ingests just that corpus, runs the
question(s) through the real ``api.search``, and scores the ranking. The archive
engine is closed and reopened per corpus in one process; the embedding / rerank
models are process-level singletons, so they load once and persist across corpora.

Each built corpus home is **cached** (keyed by content) and reused across runs, so
a re-run — or the ``--rerank`` pass over an already-embedded ``--vectors`` corpus —
skips ingest and embedding entirely (rerank is query-time, so it needs no rebuild).
``--rebuild`` forces a fresh build; ``--fresh`` uses throwaway homes with no reuse.
These homes are workspace rather than archives — hundreds per run — so they stay out
of the registry. ``haystack_corpus.py`` builds the complementary shape: the whole
dataset as one registered ``benchmark`` home, to operate on rather than to score.

Two datasets, one loop (``--dataset``):

- **locomo** — 10 multi-session conversations (Maharana et al., arXiv 2402.17753).
  Corpus = one conversation's ~588 turns (doc id = ``dia_id`` ``D<sess>:<turn>``);
  each ``qa`` question's gold is its ``evidence`` turn-id list. Turn-level
  retrieval. Reference: DRAGON dialog Recall@{5,10,25,50} = {56.7, 66.2, 76.7,
  82.7} overall (paper Table 3).
- **longmemeval** — 500 questions, each with its own ~48-session haystack
  (Wu et al., arXiv 2410.10813, the -S variant). Corpus = the question's sessions
  (doc id = session id, text = that session's **user-side** turns, matching the
  paper's reference retriever); gold is ``answer_session_ids``. Session-level
  retrieval; ``_abs`` (abstention) questions are skipped, as the official eval
  does. Reference (on the -M split, so not directly comparable to -S): vanilla
  session Recall@10 = 0.710 BM25 / 0.823 Contriever.

Metrics, overall and per question category: mean per-question **recall@k**
(fraction of gold retrieved), **recall_all@k** (all gold in top-k — LongMemEval's
strict metric), and **nDCG@k** (binary relevance). Read recall@k against the
references above.

Not the real archive, and not Ella's domain: these are third-party conversation
corpora that look nothing like an agent's own session log — a strong number
certifies the stack retrieves conversational evidence competitively in general,
the same complementary-tier caveat BEIR and CDR carry. Refuses the real archive
home. The datasets are fetched by the recon step, not this script:

    # locomo ships in its repo; longmemeval-S is a 272MB HF file:
    #   git clone https://github.com/snap-research/locomo ~/.cache/thread-evals/locomo/repo
    #   curl -L -o ~/.cache/thread-evals/longmemeval/data/longmemeval_s_cleaned.json \\
    #     https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json

    .venv/bin/python search_lab/haystack_eval.py --dataset locomo
    .venv/bin/python search_lab/haystack_eval.py --dataset longmemeval
    .venv/bin/python search_lab/haystack_eval.py --dataset locomo --vectors
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))

_SPEC = importlib.util.spec_from_file_location("beir_eval", _HERE / "beir_eval.py")
beir_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("beir_eval", beir_eval)
_SPEC.loader.exec_module(beir_eval)

_session_lines = beir_eval._session_lines
_log = beir_eval._log

_CACHE = Path.home() / ".cache" / "thread-evals"

# A built+embedded corpus home is cached under ``<data-dir>/homes/<dataset>/<fp>``
# and reused across runs. The fingerprint pins the dataset, the group, the corpus
# *content*, and whether it carries vectors — but not rerank (a query-time step),
# so a ``--vectors`` home serves ``--vectors --rerank`` without re-embedding. Bump
# CACHE_VERSION to invalidate every cached home after a schema/harness change.
CACHE_VERSION = "1"
_READY = ".ready.json"


def _fingerprint(dataset: str, gid: str, corpus: dict[str, str], vectors: bool) -> str:
    h = hashlib.sha256()
    for part in (CACHE_VERSION, dataset, str(gid), "vec" if vectors else "lex"):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    for docid in sorted(corpus):
        h.update(docid.encode("utf-8"))
        h.update(b"\x00")
        h.update(corpus[docid].encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]

# Published reference recall baselines (overall), for scale. locomo: DRAGON
# dialog (turn-level), paper Table 3. longmemeval: vanilla session recall on the
# -M split (Table 9) — the -S split scored here has no published per-retriever
# table, so treat these as ballpark, not a matched comparison.
REFERENCE = {
    "locomo": {"metric": "DRAGON dialog Recall@k",
               "recall": {5: 0.567, 10: 0.662, 25: 0.767, 50: 0.827}},
    "longmemeval": {"metric": "session Recall@k (BM25 / Contriever, -M split)",
                    "recall": {5: "0.634 / 0.723", 10: "0.710 / 0.823"}},
}

# locomo category codes (paper) and longmemeval's string question_type both land
# in one per-category breakdown.
LOCOMO_CATEGORY = {1: "multi-hop", 2: "temporal", 3: "open-domain",
                   4: "single-hop", 5: "adversarial"}

_EVIDENCE_ID = re.compile(r"D\d+:\d+")


# ── loaders: each yields (group_id, corpus{docid:text}, queries[...]) ──────────

def locomo_groups(repo: Path):
    """One group per conversation: corpus = every turn (``dia_id`` -> ``speaker:
    text``), queries = the ``qa`` items with their ``evidence`` turn ids as gold.
    Evidence strings are parsed defensively (a handful cram several ids into one
    string or carry typos) and clipped to ids that resolve to a real turn."""
    data = json.loads((repo / "data" / "locomo10.json").read_text(encoding="utf-8"))
    for conv in data:
        c = conv["conversation"]
        corpus: dict[str, str] = {}
        for key in c:
            m = re.fullmatch(r"session_(\d+)", key)
            if not m:
                continue
            for turn in c[key]:
                did = turn.get("dia_id")
                if did and turn.get("text"):
                    corpus[did] = f"{turn.get('speaker', '')}: {turn['text']}".strip()
        queries = []
        for i, qa in enumerate(conv.get("qa", [])):
            if not qa.get("question"):
                continue
            gold = {g for tok in _EVIDENCE_ID.findall(" ".join(
                str(x) for x in (qa.get("evidence") or [])))
                for g in [tok] if tok in corpus}
            if not gold:
                continue
            queries.append({"qid": f"{conv['sample_id']}_{i}", "text": qa["question"],
                            "gold": gold,
                            "category": LOCOMO_CATEGORY.get(qa.get("category"), "?")})
        if corpus and queries:
            yield conv["sample_id"], corpus, queries


def longmemeval_groups(path: Path):
    """One group per (non-abstention) question: corpus = the question's haystack
    sessions (session id -> that session's user-side turns, joined), a single
    query with ``answer_session_ids`` as gold. Matches the reference retriever's
    user-only, session-granularity indexing."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for inst in data:
        qid = inst["question_id"]
        if qid.endswith("_abs"):
            continue
        corpus: dict[str, str] = {}
        for sid, sess in zip(inst["haystack_session_ids"], inst["haystack_sessions"]):
            users = [t["content"] for t in sess
                     if t.get("role") == "user" and t.get("content")]
            if users:
                corpus[sid] = " ".join(users)
        gold = {s for s in inst.get("answer_session_ids", []) if s in corpus}
        if not corpus or not gold:
            continue
        yield qid, corpus, [{"qid": qid, "text": inst["question"], "gold": gold,
                             "category": inst.get("question_type", "?")}]


# ── scoring ────────────────────────────────────────────────────────────────

def _dcg(gains: list[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def score_query(ranked: list[str], gold: set[str], ks: tuple[int, ...]) -> dict:
    """recall@k (fraction of gold in top-k), recall_all@k (all gold in top-k),
    and binary nDCG@k for one ranking."""
    out = {"recall": {}, "recall_all": {}, "ndcg": {}}
    for k in ks:
        topk = ranked[:k]
        inter = sum(1 for d in topk if d in gold)
        out["recall"][k] = inter / len(gold) if gold else 0.0
        out["recall_all"][k] = 1.0 if inter >= len(gold) else 0.0
        gains = [1 if d in gold else 0 for d in topk]
        idcg = _dcg([1] * min(len(gold), k))
        out["ndcg"][k] = (_dcg(gains) / idcg) if idcg else 0.0
    return out


# ── per-corpus rebuild + search ──────────────────────────────────────────────

def _build_group(api, home: Path, corpus: dict[str, str], *, vectors: bool) -> dict[str, str]:
    """Fresh-build one corpus home: wipe, ingest every doc, embed if asked. Returns
    the ``thread_id -> docid`` map the scorer needs to translate hits back to gold
    ids — persisted beside the home so a later cache hit can skip straight to search."""
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    os.environ["THREAD_ARCHIVE_HOME"] = str(home)
    api.open_archive(str(home))

    work = home / "_ingest"
    work.mkdir(parents=True, exist_ok=True)
    # Tracked like any archive load (progress lands in this home's
    # load-state.json/ledger — the home is unregistered, but a stalled or slow
    # build is still inspectable via `thread_archive loads --home <home>`).
    from thread_archive._ops.load_runs import load_run

    doc_of_thread: dict[str, str] = {}
    with load_run("import", home=home, note="haystack corpus build") as run:
        with run.phase("import", total=len(corpus)) as ph:
            for i, (docid, text) in enumerate(corpus.items()):
                f = work / f"d{i}.jsonl"
                f.write_text("\n".join(json.dumps(x) for x in _session_lines(docid, text)) + "\n",
                             encoding="utf-8")
                res = api.import_path(f, source_id=f"hay-{docid}")
                f.unlink()
                doc_of_thread[str(res.thread_id)] = docid
                ph.advance()
    if vectors:
        api.embed()
    shutil.rmtree(work, ignore_errors=True)
    return doc_of_thread


def eval_group(api, home: Path, corpus: dict[str, str], queries: list[dict],
               *, vectors: bool, rerank, ks: tuple[int, ...],
               cache: bool, rebuild: bool) -> tuple[list[dict], bool]:
    """Score every query over ``corpus``. Reuses a cached home when one is present
    (``cache`` on, no ``rebuild``, ready marker written), else builds fresh and —
    when caching — drops the marker so the next run reuses it. Returns
    ``(rows, was_cache_hit)``. The engine is closed by the caller between groups."""
    ready = home / _READY
    if cache and not rebuild and ready.exists():
        os.environ["THREAD_ARCHIVE_HOME"] = str(home)
        api.open_archive(str(home))
        doc_of_thread = json.loads(ready.read_text(encoding="utf-8"))["doc_of_thread"]
        hit = True
    else:
        doc_of_thread = _build_group(api, home, corpus, vectors=vectors)
        if cache:
            ready.write_text(json.dumps({"doc_of_thread": doc_of_thread,
                                         "vectors": vectors}), encoding="utf-8")
        hit = False

    limit = max(ks) * 2
    results = []
    for q in queries:
        hits = api.search(q["text"], limit=limit, content_types=["user"],
                          group="none", rerank=rerank)
        ranked, seen = [], set()
        for h in hits:
            d = doc_of_thread.get(str(h["thread_id"]))
            if d is not None and d not in seen:
                seen.add(d)
                ranked.append(d)
        m = score_query(ranked, q["gold"], ks)
        m["category"] = q["category"]
        results.append(m)
    return results, hit


def _aggregate(rows: list[dict], ks: tuple[int, ...]) -> dict:
    """Mean of each metric at each k over ``rows`` (empty -> zeros)."""
    n = len(rows) or 1
    agg = {m: {k: sum(r[m][k] for r in rows) / n for k in ks}
           for m in ("recall", "recall_all", "ndcg")}
    agg["n"] = len(rows)
    return agg


def run(args) -> int:
    ks = tuple(int(k) for k in args.ks.split(","))
    if args.dataset == "locomo":
        repo = Path(args.locomo_repo).expanduser()
        src = repo / "data" / "locomo10.json"
        if not src.exists():
            raise SystemExit(f"locomo data not found at {src}; clone snap-research/locomo there")
        groups = list(locomo_groups(repo))
    else:
        path = Path(args.longmemeval_file).expanduser()
        if not path.exists():
            raise SystemExit(f"longmemeval file not found at {path}; download longmemeval_s_cleaned.json there")
        groups = list(longmemeval_groups(path))
    if args.max_groups:
        groups = groups[: args.max_groups]

    # Per-corpus homes live under a root: a throwaway tmpdir when --fresh (no reuse),
    # else a persistent cache dir. Guard the root against overlapping the real
    # archive home either way — rmtree only ever touches paths beneath it.
    cache = not args.fresh
    cache_root = Path(tempfile.mkdtemp(prefix=f"hay-{args.dataset}-")) if args.fresh \
        else (Path(args.data_dir).expanduser() / "homes")
    default_home = (Path.home() / ".thread" / "archive").resolve()
    cr = cache_root.resolve()
    if cr == default_home or default_home in cr.parents or cr in default_home.parents:
        raise SystemExit(f"refusing: eval home root {cache_root} overlaps the real archive")

    os.environ["THREAD_ARCHIVE_NO_THROTTLE"] = "1"
    os.environ["THREAD_ARCHIVE_EMBED"] = "on" if args.vectors else "off"
    os.environ["THREAD_ARCHIVE_RERANK"] = "off" if args.rerank == "off" else "on"
    # The community-coherence re-rank reads event_vectors; with embeddings off that
    # table never exists, so it throws (fail-soft) on every conceptual query. A
    # core lexical install has no coherence arm at all — pin it off to match, and
    # to keep the lexical measurement free of a swallowed per-query exception.
    if not args.vectors:
        os.environ["THREAD_ARCHIVE_COHERENCE"] = "off"
    rerank = {"on": True, "off": False, "auto": None}[args.rerank]

    from thread_archive import _api as api

    n_q = sum(len(q) for _, _, q in groups)
    n_docs = sum(len(c) for _, c, _ in groups)
    _log(f"{args.dataset}: {len(groups)} corpora, {n_docs} docs, {n_q} queries "
         f"(ks={ks}, vectors={args.vectors}, rerank={args.rerank})")

    all_rows: list[dict] = []
    n_hits = 0
    t0 = time.monotonic()
    for gi, (gid, corpus, queries) in enumerate(groups):
        home = cache_root / args.dataset / _fingerprint(args.dataset, gid, corpus, args.vectors)
        rows, hit = eval_group(api, home, corpus, queries,
                               vectors=args.vectors, rerank=rerank, ks=ks,
                               cache=cache, rebuild=args.rebuild)
        all_rows += rows
        n_hits += hit
        api.close()
        if (gi + 1) % max(1, len(groups) // 10) == 0:
            _log(f"  {gi + 1}/{len(groups)} corpora  ({time.monotonic() - t0:.0f}s, "
                 f"{n_hits} from cache)")
    if cache:
        _log(f"cache: {n_hits}/{len(groups)} corpora reused, {len(groups) - n_hits} built"
             + (" (--rebuild)" if args.rebuild else ""))

    overall = _aggregate(all_rows, ks)
    cats = sorted({r["category"] for r in all_rows})
    per_cat = {c: _aggregate([r for r in all_rows if r["category"] == c], ks) for c in cats}

    arms = ["lexical"] + (["vectors"] if args.vectors else []) \
        + (["rerank:on"] if rerank is True else ["rerank:auto"] if rerank is None else [])
    ref = REFERENCE.get(args.dataset, {})
    print()
    print(f"=== {args.dataset} — archive stack [{' + '.join(arms)}] ===")
    print(f"corpora: {len(groups)}   queries: {overall['n']}   "
          f"elapsed: {time.monotonic() - t0:.0f}s")
    print(f"  reference: {ref.get('metric', '-')}")
    print()
    hdr = "  metric        " + "".join(f"@{k:<8}" for k in ks)
    print(hdr)
    for m, label in (("recall", "recall"), ("recall_all", "recall_all"), ("ndcg", "nDCG")):
        print(f"  {label:<12}" + "".join(f"{overall[m][k]:<9.3f}" for k in ks))
    if ref.get("recall"):
        print(f"  {'ref recall':<12}" + "".join(
            f"{str(ref['recall'].get(k, '-')):<9}" for k in ks))
    print()
    print("  recall@{} by category:".format(ks[1] if len(ks) > 1 else ks[0]))
    kk = ks[1] if len(ks) > 1 else ks[0]
    for c in cats:
        pc = per_cat[c]
        print(f"    {str(c):<14} n={pc['n']:<5} recall@{kk}={pc['recall'][kk]:.3f}  "
              f"nDCG@{kk}={pc['ndcg'][kk]:.3f}")
    print()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "dataset": args.dataset, "arms": arms, "n_queries": overall["n"],
            "ks": list(ks), "overall": overall,
            "per_category": per_cat, "reference": ref,
        }, indent=2, default=str) + "\n", encoding="utf-8")
        _log(f"wrote {args.json_out}")

    if args.fresh:
        shutil.rmtree(cache_root, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", choices=["locomo", "longmemeval"], required=True)
    ap.add_argument("--locomo-repo", default=str(_CACHE / "locomo" / "repo"))
    ap.add_argument("--longmemeval-file",
                    default=str(_CACHE / "longmemeval" / "data" / "longmemeval_s_cleaned.json"))
    ap.add_argument("--data-dir", default=str(_CACHE))
    ap.add_argument("--ks", default=None,
                    help="comma-separated cutoffs (default: locomo 5,10,25,50; longmemeval 5,10)")
    ap.add_argument("--vectors", action="store_true")
    ap.add_argument("--rerank", choices=["on", "off", "auto"], default="off")
    ap.add_argument("--max-groups", type=int, default=None, help="cap corpora (smoke runs)")
    ap.add_argument("--rebuild", action="store_true",
                    help="force fresh ingest+embed even when a cached home exists (refreshes it)")
    ap.add_argument("--fresh", action="store_true",
                    help="throwaway homes under a tmpdir, deleted on exit (no cache reuse)")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    if args.ks is None:
        args.ks = "5,10,25,50" if args.dataset == "locomo" else "5,10"
    # Per-question haystacks mean hundreds of tiny fingerprint-named corpus homes
    # per run — workspace, not archives. Registering them would bury the real
    # entries in ~/.thread/archives.json.
    from thread_archive._ops.archives import suppress_registration

    with suppress_registration():
        return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
