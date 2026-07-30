"""Haystack retrieval eval — per-question conversational retrieval over LoCoMo,
LongMemEval and BEAM, scored against their published recall baselines.

BEIR (``beir_eval.py``) and CDR (``cdr_eval.py``) both retrieve from one *shared*
corpus. These two memory benchmarks are a different shape: **per-question haystack
retrieval** — each question carries its own small conversation history, and the
task is to pull the evidence turn(s)/session(s) out of *that* history. So this
harness builds a small archive per corpus, ingests just that corpus, runs the
question(s) through the real ``api.search``, and scores the ranking. The archive
engine is closed and reopened per corpus in one process; the embedding
models are process-level singletons, so they load once and persist across corpora.

Each built corpus home is **cached** (keyed by content) and reused across runs, so
a re-run over an already-embedded ``--vectors`` corpus skips ingest and embedding
entirely.
``--rebuild`` forces a fresh build; ``--fresh`` uses throwaway homes with no reuse.
These homes are workspace rather than archives — hundreds per run — so they stay out
of the registry. ``haystack_corpus.py`` builds the complementary shape: the whole
dataset as one registered ``benchmark`` home, to operate on rather than to score.

Three datasets, one loop (``--dataset``):

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
- **beam** — 20 conversations at the 100K token tier, ~280 questions across the
  seven memory-ability categories that are retrieval questions (see
  :data:`BEAM_UNSCORED` for the three that are not). Corpus = one conversation's
  messages, both roles (doc id = the message ``id``); gold is the question's
  ``source_chat_ids``. Message-level retrieval, and the **multi-answer** row on
  this bench: median 2–3 gold messages and up to 16, where LoCoMo and LongMemEval
  sit near one. So recall@k here reads *completeness* — does a window hold
  everything bearing on the question — which nothing else measures. No published
  retrieval baseline: the paper scores end-to-end QA under a memory framework, not
  the retrieval step. BEAM also publishes 500K and 1M tiers, which are not carried:
  they are the same conversations extended, so the length ladder they buy costs
  11 hours of embed for questions this one already asks.

Metrics, overall and per question category: mean per-question **recall@k**
(fraction of gold retrieved), **recall_all@k** (all gold in top-k — LongMemEval's
strict metric), and **nDCG@k** (binary relevance). Read recall@k against the
references above.

Not the real archive, and not its domain: these are third-party conversation
corpora that look nothing like an agent's own session log — a strong number
certifies the stack retrieves conversational evidence competitively in general,
the same complementary-tier caveat BEIR and CDR carry. Refuses the real archive
home. The datasets are fetched by the recon step, not this script:

    # locomo ships in its repo; longmemeval-S is a 272MB HF file:
    #   git clone https://github.com/snap-research/locomo ~/.cache/thread-evals/locomo/repo
    #   curl -L -o ~/.cache/thread-evals/longmemeval/data/longmemeval_s_cleaned.json \\
    #     https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
    # beam ships as one parquet per tier:
    #   curl -L -o ~/.cache/thread-evals/beam/100K.parquet \\
    #     https://huggingface.co/datasets/Mohammadta/BEAM/resolve/main/data/100K-00000-of-00001.parquet

    .venv/bin/python search_lab/haystack_eval.py --dataset locomo
    .venv/bin/python search_lab/haystack_eval.py --dataset longmemeval
    .venv/bin/python search_lab/haystack_eval.py --dataset locomo --vectors
    .venv/bin/python search_lab/haystack_eval.py --dataset beam --beam-tier 100K
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
# The lab dir too, so bare sibling imports (eval_home, eval_core, …) resolve
# however this file was loaded: as a script, by path, or as search_lab.X.
sys.path.insert(0, str(_HERE))

import dataset_pins  # noqa: E402
import eval_core  # noqa: E402
import eval_home  # noqa: E402

from thread_archive._retrieval import _probe  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("beir_eval", _HERE / "beir_eval.py")
beir_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("beir_eval", beir_eval)
_SPEC.loader.exec_module(beir_eval)

_session_lines = beir_eval._session_lines
_log = beir_eval._log

_CACHE = eval_home.CACHE_ROOT

# A built+embedded corpus home is cached under ``<data-dir>/homes/<dataset>/<fp>``
# and reused across runs. The fingerprint pins the dataset, the group, the corpus
# *content*, and whether it carries vectors. Bump
# CACHE_VERSION to invalidate every cached home after a schema/harness change.
CACHE_VERSION = "1"
_READY = ".ready.json"


def corpus_id(fingerprints: list[str]) -> str:
    """One identity for a whole per-question run: a digest over its corpora's
    content fingerprints. The shared-corpus benchmarks stamp their single home;
    this is the same binding for a shape that has hundreds of them."""
    h = hashlib.sha256()
    for fp in sorted(fingerprints):
        h.update(fp.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


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
    # BEAM's paper reports end-to-end QA accuracy under a memory framework, not
    # retrieval recall, so there is no baseline to print beside this row. Left
    # empty rather than borrowed: a number from a different task is worse than no
    # number, because it reads as a comparison. `bm25_baseline.py` is what would
    # give this row a local reference if one is wanted.
    "beam": {"metric": "no published retrieval baseline"},
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


#: BEAM categories whose questions are not retrieval tasks, and so are not scored
#: as one. Each is skipped for a reason that holds however good the ranker is:
#:
#: - ``abstention`` — deliberately unanswerable from the conversation, and ships
#:   with no ``source_chat_ids``. Nothing to retrieve.
#: - ``summarization`` — "a comprehensive summary of how my project has
#:   progressed" matches the whole corpus by construction, and its gold runs to 16
#:   messages, so a *perfect* retriever caps at recall@10 = 0.625. The task is a
#:   whole-conversation read, not a top-k window.
#: - ``event_ordering`` — "list the order in which I brought up different aspects
#:   of my project development, mention ONLY and ONLY five items". The query names
#:   a broad topic and an output format; the operation asked for is sequencing,
#:   and there is no distinguishing content in it to match on.
#:
#: ``instruction_following`` is deliberately **not** here. Its questions are
#: generic ("Which libraries are used in this project?") and its gold is thin, so
#: it scores low — but finding the message that answers a broad question is
#: retrieval doing its actual job, and dropping a row for being hard is how a
#: bench stops measuring anything.
BEAM_UNSCORED = frozenset({"abstention", "summarization", "event_ordering"})


def beam_groups(path: Path, tier: str):
    """One group per conversation: corpus = every chat message (``id`` ->
    ``role: content``), queries = the probing questions with their
    ``source_chat_ids`` as gold.

    The **multi-answer** shape on this bench. LoCoMo and LongMemEval are close to
    single-gold; BEAM's median question needs 2–3 messages and its worst needs 96,
    so recall@k here reads completeness — whether a window holds everything that
    bears on the question — rather than findability. Roughly three quarters of the
    questions carry more than one gold id.

    Both roles are indexed. A quarter of the gold ids are assistant messages (the
    answer to "what did I decide about X" often lives in the reply), so a
    user-only corpus would put them out of reach and score the miss as a ranking
    failure.

    ``source_chat_ids`` arrives in three shapes: a flat id list, a dict of named
    id lists (``temporal_reasoning`` splits ``first_event`` / ``second_event``),
    and occasionally absent, which drops the question rather than scoring it
    against an empty gold set. Ids are clipped to those that resolve to a real
    message; on the shipped data none are lost.

    The categories in :data:`BEAM_UNSCORED` are skipped — see there for why each
    one is not a retrieval question."""
    import ast

    import pyarrow.parquet as pq

    for row in pq.read_table(path).to_pylist():
        flat = [m for session in row["chat"] for m in session]
        corpus = {str(m["id"]): f"{m.get('role', '')}: {m.get('content', '')}".strip()
                  for m in flat if m.get("content")}
        try:
            probing = ast.literal_eval(row["probing_questions"])
        except (ValueError, SyntaxError):
            continue
        queries = []
        for category, items in sorted(probing.items()):
            if category in BEAM_UNSCORED:
                continue
            for i, item in enumerate(items):
                raw = item.get("source_chat_ids")
                ids: list = []
                if isinstance(raw, dict):
                    for value in raw.values():
                        ids += value if isinstance(value, list) else [value]
                elif isinstance(raw, list):
                    ids = raw
                elif raw is not None:
                    ids = [raw]
                gold = {str(i) for i in ids if str(i) in corpus}
                if not gold or not item.get("question"):
                    continue
                queries.append({"qid": f"{row['conversation_id']}_{category}_{i}",
                                "text": item["question"], "gold": gold,
                                "category": category})
        if corpus and queries:
            yield f"{tier}:{row['conversation_id']}", corpus, queries


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
    # build is still inspectable via `thread-archive loads --home <home>`).
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
        eval_home.embed_corpus(home)
    shutil.rmtree(work, ignore_errors=True)
    return doc_of_thread


def eval_group(api, home: Path, corpus: dict[str, str], queries: list[dict],
               *, vectors: bool, ks: tuple[int, ...],
               cache: bool, rebuild: bool) -> tuple[list[dict], bool]:
    """Score every query over ``corpus``. Reuses a cached home when one is present
    (``cache`` on, no ``rebuild``, ready marker written), else builds fresh and —
    when caching — drops the marker so the next run reuses it. Returns
    ``(rows, was_cache_hit, (latencies, stage_samples, per_query))``. The engine is
    closed by the caller between groups."""
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

    # This home is the one just opened, so the graph cache is warmed (and the
    # previous corpus's dropped) here rather than once for the run.
    eval_home.warm(swapped_home=True)

    limit = max(ks) * 2
    results = []
    # Timed per question, and per stage. This shape queries hundreds of tiny
    # corpora rather than one large one, so its cost profile is the only place
    # the fixed per-search overhead shows up separated from the index scan it
    # usually hides behind — which is exactly what a per-question memory
    # benchmark is measuring the retrieval side of.
    latencies: list[float] = []
    stage_samples: list[dict] = []
    # Every question's own result. This shape is one question per corpus, so a
    # failure here names a specific haystack the retrieval could not find the
    # needle in — the most directly readable failure the bench produces.
    per_query: list[dict] = []
    for q in queries:
        with _probe.install() as probe:
            s0 = time.monotonic()
            hits = api.search(q["text"], limit=limit, content_types=["user"])
            elapsed = time.monotonic() - s0
        latencies.append(elapsed)
        if probe.ran:
            sample = probe.as_record()
            sample["total_ms"] = elapsed * 1000.0
            stage_samples.append(sample)
        ranked, seen = [], set()
        for h in hits:
            d = doc_of_thread.get(str(h["thread_id"]))
            if d is not None and d not in seen:
                seen.add(d)
                ranked.append(d)
        m = score_query(ranked, q["gold"], ks)
        m["category"] = q["category"]
        gold = set(q["gold"])
        top = ks[-1]
        per_query.append(eval_core.query_row(
            qid=q.get("qid", ""), query=q["text"], latency_s=elapsed,
            n_gold=len(gold),
            rank=next((i + 1 for i, d in enumerate(ranked) if d in gold), None),
            found=sum(1 for d in ranked[:top] if d in gold),
            group=q["category"],
            measures={f"recall{top}": m["recall"][top],
                      f"ndcg{top}": m["ndcg"][top],
                      f"recall_all{top}": m["recall_all"][top]}))
        results.append(m)
    return results, hit, (latencies, stage_samples, per_query)


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
        dataset_pins.verify("locomo")
        groups = list(locomo_groups(repo))
    elif args.dataset == "beam":
        path = Path(args.beam_file or (_CACHE / "beam" / f"{args.beam_tier}.parquet")).expanduser()
        if not path.exists():
            raise SystemExit(
                f"beam tier not found at {path}; fetch it from "
                f"huggingface.co/datasets/Mohammadta/BEAM "
                f"(data/{args.beam_tier}-00000-of-00001.parquet)")
        dataset_pins.verify("beam")
        groups = list(beam_groups(path, args.beam_tier))
    else:
        path = Path(args.longmemeval_file).expanduser()
        if not path.exists():
            raise SystemExit(f"longmemeval file not found at {path}; download longmemeval_s_cleaned.json there")
        dataset_pins.verify("longmemeval")
        groups = list(longmemeval_groups(path))
    # Sampling *corpora* rather than questions: this shape builds or opens one
    # home per group, so cutting groups cuts the fixed per-corpus cost too, where
    # cutting questions inside every group would leave it whole.
    groups = eval_core.sample_queries(groups, args.sample, key=lambda g: g[0])

    # Per-corpus homes live under a root: a throwaway tmpdir when --fresh (no reuse),
    # else a persistent cache dir. Guard the root against overlapping the real
    # archive home either way — rmtree only ever touches paths beneath it.
    cache = not args.fresh
    cache_root = Path(tempfile.mkdtemp(prefix=f"hay-{args.dataset}-")) if args.fresh \
        else (Path(args.data_dir).expanduser() / "homes")
    cache_root = eval_home.guard_home(cache_root, what="eval home root")

    eval_home.pin_arms(vectors=args.vectors)

    from thread_archive import _api as api

    n_q = sum(len(q) for _, _, q in groups)
    n_docs = sum(len(c) for _, c, _ in groups)
    _log(f"{args.dataset}: {len(groups)} corpora, {n_docs} docs, {n_q} queries "
         f"(ks={ks}, vectors={args.vectors})")

    all_rows: list[dict] = []
    latencies: list[float] = []
    stage_samples: list[dict] = []
    per_query: list[dict] = []
    n_hits = 0
    fingerprints: list[str] = []
    t0 = time.monotonic()
    for gi, (gid, corpus, queries) in enumerate(groups):
        fingerprints.append(_fingerprint(args.dataset, gid, corpus, args.vectors))
        home = cache_root / args.dataset / fingerprints[-1]
        rows, hit, (lat, stages, per_q) = eval_group(
            api, home, corpus, queries, vectors=args.vectors, ks=ks,
            cache=cache, rebuild=args.rebuild)
        all_rows += rows
        latencies += lat
        stage_samples += stages
        per_query += per_q
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

    arms = eval_home.arm_labels(vectors=args.vectors)
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
        if not eval_core.comparable_to_published(sample=args.sample):
            print(eval_core.NOT_COMPARABLE)
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
            # This shape has no single corpus to stamp — it is hundreds of
            # per-question homes — so its identity is the digest of their content
            # fingerprints, which is the same thing one level up.
            "corpus_id": corpus_id(fingerprints),
            "per_category": per_cat, "reference": ref,
            "per_query": per_query,
            # Wall-clock here spans corpus builds as well as searches — hundreds
            # of homes get built or reused inside the loop — so `scoring_s` is
            # the loop's whole elapsed time and `qps` is throughput over it, not
            # a search rate. The latency percentiles below are search-only.
            "performance": eval_core.performance(
                latencies, stage_samples, scoring_s=time.monotonic() - t0,
                corpus_docs=n_docs, arms=arms),
        }, indent=2, default=str) + "\n", encoding="utf-8")
        _log(f"wrote {args.json_out}")

    if args.fresh:
        shutil.rmtree(cache_root, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", choices=["locomo", "longmemeval", "beam"], required=True)
    ap.add_argument("--locomo-repo", default=str(_CACHE / "locomo" / "repo"))
    ap.add_argument("--longmemeval-file",
                    default=str(_CACHE / "longmemeval" / "data" / "longmemeval_s_cleaned.json"))
    ap.add_argument("--beam-tier", default="100K", choices=["100K"],
                    help="BEAM conversation-length tier. Only the 100K tier is "
                         "carried; --beam-file still points the loader anywhere")
    ap.add_argument("--beam-file", default=None,
                    help="explicit BEAM parquet path (default: <data-dir>/beam/<tier>.parquet)")
    ap.add_argument("--data-dir", default=str(_CACHE))
    ap.add_argument("--ks", default=None,
                    help="comma-separated cutoffs (default: locomo/beam 5,10,25,50; "
                         "longmemeval 5,10)")
    ap.add_argument("--vectors", action="store_true")
    ap.add_argument("--sample", type=int, default=None,
                    help="score a deterministic sample of this many corpora instead of all (the quick tier; see search_lab.eval_core.sample_queries)")
    ap.add_argument("--rebuild", action="store_true",
                    help="force fresh ingest+embed even when a cached home exists (refreshes it)")
    ap.add_argument("--fresh", action="store_true",
                    help="throwaway homes under a tmpdir, deleted on exit (no cache reuse)")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    if args.ks is None:
        args.ks = "5,10" if args.dataset == "longmemeval" else "5,10,25,50"
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
