"""PerLTQA calibration harness — personal long-term memory retrieval, scored by
memory-unit id match.

The other conversational yardsticks on this bench retrieve a *turn* (LoCoMo,
BEAM) or a *session* (LongMemEval). PerLTQA retrieves a **memory unit**: a
curated record — a profile, a relationship, an event, a dialogue — sitting in a
structured memory bank. That is a third granularity, and it is the one an
explicit memory store is organised around, so it measures whether the stack can
find the right record rather than the right conversation.

Its labels exist by construction and cost nothing to trust. Each question was
authored *from* one memory unit and carries that unit's id in ``Reference
Memory``; on the shipped English data all 8,236 non-profile questions resolve
exactly, and the reference is always the block's own id. No annotator judged a
ranking, no model graded a pool, and no retriever chose what counts as correct.

Shape and scale (``Dataset/en`` of Elvin-Yiming-Du/PerLTQA):

- corpus — 141 memory banks × four memory types, 7,521 units in all: 141
  profiles, 1,339 relationships, 3,037 events, 3,004 dialogues. Unit ids are
  globally unique across banks, so this is one **shared corpus**, not a
  per-question haystack.
- queries — 8,593 questions (357 profile, 897 relationship, 4,501 event, 2,838
  dialogue), each **single-gold**.

Two things to hold while reading a number off this. It is single-gold, so it
scores findability and says nothing about completeness — BEAM is the row for
that. And the corpus is synthetic persona material translated from Chinese, so
its surface language is not English-native; a lexical arm meets phrasings no
English corpus would produce.

The profile questions need one construction the others don't. Their ``Reference
Memory`` names a *field* ("Gender"), not a unit id, because a bank's profile is a
flat record rather than a keyed collection. The gold is that person's profile
unit, resolved by matching the question block's protagonist name against
``profile.Protagonist`` — unambiguous on the shipped data, where all 141
protagonists are distinct and every one of the 32 questioned people matches one.

No published retrieval baseline is printed. The PerLTQA paper reports a memory
retrieval subtask, but not in a form this run reproduces, and a borrowed number
reads as a comparison it is not. ``bm25_baseline.py`` is what would give this row
a local reference.

Never touches the real archive: ``search_lab.eval_home`` refuses any home that
overlaps one. The build is cached under
``~/.cache/thread-evals/homes/perltqa``, so the ingest and the embed pass are
paid once.

    .venv/bin/python search_lab/perltqa_eval.py
    .venv/bin/python search_lab/perltqa_eval.py --vectors
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
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

import eval_core  # noqa: E402
import eval_home  # noqa: E402

from thread_archive._retrieval import _probe  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("beir_eval", _HERE / "beir_eval.py")
beir_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("beir_eval", beir_eval)
_SPEC.loader.exec_module(beir_eval)

_log = beir_eval._log
_read_json = beir_eval._read_json
score_run = beir_eval.score_run

#: The four memory types, in the order the dataset nests them.
MEMORY_TYPES = ("profile", "social_relationship", "events", "dialogues")


def profile_id(index: int) -> str:
    """The synthesized id for bank ``index``'s profile unit.

    A bank's profile is a flat field record with no key of its own, where every
    other memory type is a dict already keyed by a globally unique id. Namespaced
    under ``profile:`` so it can never collide with a real unit id."""
    return f"profile:{index}"


def profile_text(bank: dict) -> str:
    """A bank's profile as one document: its field/value pairs, then the prose
    description. Both, because the questions ask across the two — "what is X's
    gender" is answered by a field and "what did X study" by the description."""
    fields = bank.get("profile") or {}
    lines = [f"{k}: {v}" for k, v in fields.items() if v not in (None, "")]
    description = (bank.get("profile_description") or "").strip()
    if description:
        lines.append(description)
    return "\n".join(lines)


def unit_text(memory_type: str, unit: dict) -> str:
    """One non-profile memory unit as retrievable text.

    Each type is shaped differently and flattening them uniformly would drop the
    part the questions ask about: a relationship's meaning is in its
    ``Description``, an event's in its ``content``, and a dialogue's in the
    utterances under its timestamps."""
    if memory_type == "events":
        return (unit.get("content") or "").strip()
    if memory_type == "dialogues":
        turns: list[str] = []
        for when, utterances in (unit.get("contents") or {}).items():
            for utterance in (utterances if isinstance(utterances, list) else [utterances]):
                turns.append(f"{when} {utterance}")
        return "\n".join(turns).strip()
    # social_relationship: a small record whose fields all carry signal.
    return "\n".join(f"{k}: {v}" for k, v in unit.items() if v not in (None, "")).strip()


def load_corpus(mem_path: Path) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    """``(corpus, type_of_unit, bank_of_protagonist)`` from the memory-bank file.

    ``corpus`` maps unit id -> text over all four memory types; ``type_of_unit``
    records which type each id came from, so a score can be broken down by it;
    ``bank_of_protagonist`` is what resolves a profile question to its bank."""
    banks = json.loads(mem_path.read_text(encoding="utf-8"))
    corpus: dict[str, str] = {}
    type_of: dict[str, str] = {}
    bank_of: dict[str, int] = {}
    for index, bank in enumerate(banks):
        name = ((bank.get("profile") or {}).get("Protagonist") or "").strip()
        if name:
            bank_of[name] = index
        text = profile_text(bank)
        if text:
            corpus[profile_id(index)] = text
            type_of[profile_id(index)] = "profile"
        for memory_type in ("social_relationship", "events", "dialogues"):
            for unit_id, unit in (bank.get(memory_type) or {}).items():
                text = unit_text(memory_type, unit)
                if text:
                    corpus[str(unit_id)] = text
                    type_of[str(unit_id)] = memory_type
    return corpus, type_of, bank_of


def _gold_ids(raw) -> list[str]:
    """The unit ids in a ``Reference Memory`` value.

    It ships as a stringified Python list (``"['4_0_0']"``) for the keyed memory
    types and as a bare field name for profiles, so it is parsed permissively and
    whatever comes out is normalized to a list of strings."""
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError, TypeError):
        parsed = raw
    values = parsed if isinstance(parsed, list) else [parsed]
    return [str(v) for v in values if v not in (None, "")]


def load_queries(qa_path: Path, corpus: dict[str, str],
                 bank_of: dict[str, int]) -> list[dict]:
    """Every question with its resolved gold unit id.

    A question whose gold does not resolve is dropped rather than scored against
    an empty set — a case nothing can satisfy is a miss recorded against the
    ranker for the dataset's bookkeeping, not for anything the ranker did. On the
    shipped English data nothing is dropped."""
    people = json.loads(qa_path.read_text(encoding="utf-8"))
    queries: list[dict] = []
    for person in people:
        for name, sections in person.items():
            index = bank_of.get(name.strip())
            for memory_type in MEMORY_TYPES:
                blocks = sections.get(memory_type)
                if not blocks:
                    continue
                # Profiles come as a flat list of questions; every other type is
                # a list of {unit_id: [questions]} blocks.
                if memory_type == "profile":
                    items = [(None, item) for item in blocks]
                else:
                    items = [(unit_id, item) for block in blocks
                             for unit_id, group in block.items() for item in group]
                for unit_id, item in items:
                    question = (item.get("Question") or "").strip()
                    if not question:
                        continue
                    if memory_type == "profile":
                        gold = {profile_id(index)} if index is not None else set()
                    else:
                        gold = {g for g in _gold_ids(item.get("Reference Memory"))
                                if g in corpus}
                        if unit_id and str(unit_id) in corpus:
                            gold.add(str(unit_id))
                    gold = {g for g in gold if g in corpus}
                    if not gold:
                        continue
                    queries.append({
                        "qid": f"{name}:{memory_type}:{unit_id or len(queries)}",
                        "text": question, "gold": gold, "category": memory_type})
    return queries


# ------------------------------------------------------------------------- run

def run(args) -> int:
    root = Path(args.data_dir).expanduser() / "perltqa"
    mem_path = Path(args.memory_file or root / "perltmem_en.json").expanduser()
    qa_path = Path(args.qa_file or root / "perltqa_en.json").expanduser()
    for path in (mem_path, qa_path):
        if not path.exists():
            raise SystemExit(
                f"PerLTQA data not found at {path}; fetch Dataset/en/ from "
                f"github.com/Elvin-Yiming-Du/PerLTQA into {root}")

    if args.fresh:
        home = Path(tempfile.mkdtemp(prefix="perltqa-home-"))
    else:
        home = (Path(args.data_dir).expanduser() / "homes"
                / eval_home.home_name("perltqa", max_docs=args.max_docs))
    home = eval_home.guard_home(home, what="PerLTQA corpus home")

    # Pin home + arms BEFORE importing the package, so the store bakes the right
    # DSN and no model cold-loads unless asked.
    os.environ["THREAD_ARCHIVE_HOME"] = str(home)
    eval_home.pin_arms(vectors=args.vectors)

    corpus, type_of, bank_of = load_corpus(mem_path)
    queries = load_queries(qa_path, corpus, bank_of)
    queries = eval_core.sample_queries(queries, args.sample, key=lambda q: q["qid"])
    _log(f"perltqa: {len(corpus)} memory units, {len(queries)} questions")

    marker_path = home / "perltqa_build.json"
    docmap_path = home / "perltqa_docmap.json"
    corpus_key = {"dataset": "perltqa-en", "max_docs": args.max_docs}
    marker = _read_json(marker_path)
    need_ingest = (args.rebuild or eval_home.marker_stale(marker, corpus_key)
                   or not docmap_path.exists())

    if need_ingest:
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(parents=True, exist_ok=True)

    from thread_archive import _api as api

    api.open_archive(str(home))

    if need_ingest:
        work = home / "_ingest"
        work.mkdir(parents=True, exist_ok=True)
        # One temp corpus file in the shape the shared ingest reads, so a
        # PerLTQA build is tracked in the load ledger exactly like a BEIR one.
        staged = home / "_corpus.jsonl"
        with staged.open("w", encoding="utf-8") as fh:
            for unit_id, text in corpus.items():
                fh.write(json.dumps({"_id": unit_id, "title": "", "text": text}) + "\n")
        doc_of_thread = beir_eval.ingest_corpus(staged, work, args.max_docs,
                                                label="perltqa")
        staged.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
        docmap_path.write_text(json.dumps(doc_of_thread), encoding="utf-8")
        marker = {**corpus_key, "corpus_docs": len(doc_of_thread), "embedded": 0}
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
    else:
        doc_of_thread = _read_json(docmap_path)
        _log(f"reusing cached archive at {home} ({len(doc_of_thread)} units)")

    if args.vectors:
        if marker.get("embedded", 0) >= len(doc_of_thread) and not args.rebuild:
            _log(f"reusing cached embeddings ({marker['embedded']} vectors)")
        else:
            _log("embedding corpus (real model) ...")
            t0 = time.monotonic()
            res = eval_home.embed_corpus(home)
            _log(f"embedded {res.get('embedded')} events in {time.monotonic() - t0:.0f}s")
            marker["embedded"] = len(doc_of_thread)
            marker_path.write_text(json.dumps(marker), encoding="utf-8")

    corpus_id = eval_home.stamp_corpus(home, restamp=need_ingest)

    ks = (10, 100)
    rows: list[dict] = []
    latencies: list[float] = []
    stage_samples: list[dict] = []
    per_query: list[dict] = []

    # Hold the ranker still before the first scored query — the coherence re-rank
    # otherwise lands partway through and splits the run in two.
    eval_home.warm()

    t0 = time.monotonic()
    for i, q in enumerate(queries):
        with _probe.install() as probe:
            s0 = time.monotonic()
            hits = api.search(q["text"], limit=max(ks) * 2, content_types=["user"])
            elapsed = time.monotonic() - s0
        latencies.append(elapsed)
        if probe.ran:
            sample = probe.as_record()
            sample["total_ms"] = elapsed * 1000.0
            stage_samples.append(sample)
        ranked: list[str] = []
        seen: set[str] = set()
        for h in hits:
            d = doc_of_thread.get(str(h["thread_id"]))
            if d is not None and d not in seen:
                seen.add(d)
                ranked.append(d)
        rel = {g: 1 for g in q["gold"]}
        m = score_run(ranked, rel, ks)
        m["category"] = q["category"]
        rows.append(m)
        per_query.append(eval_core.query_row(
            qid=q["qid"], query=q["text"], latency_s=elapsed, n_gold=len(rel),
            rank=next((j + 1 for j, d in enumerate(ranked) if d in rel), None),
            found=sum(1 for d in ranked[:10] if d in rel), group=q["category"],
            measures={"ndcg10": m["ndcg10"], "mrr10": m["mrr10"],
                      "recall10": m["recall"][10], "recall100": m["recall"][100]}))
        if (i + 1) % 500 == 0:
            _log(f"  scored {i + 1}/{len(queries)} "
                 f"({(i + 1) / (time.monotonic() - t0):.1f}/s)")

    total_s = time.monotonic() - t0

    def mean(subset: list[dict]) -> dict:
        n = len(subset) or 1
        return {"n": len(subset),
                "ndcg10": sum(r["ndcg10"] for r in subset) / n,
                "mrr10": sum(r["mrr10"] for r in subset) / n,
                "recall": {k: sum(r["recall"][k] for r in subset) / n for k in ks}}

    overall = mean(rows)
    per_type = {t: mean([r for r in rows if r["category"] == t])
                for t in MEMORY_TYPES if any(r["category"] == t for r in rows)}
    p50 = (sorted(latencies)[len(latencies) // 2] * 1000) if latencies else 0.0

    arms = eval_home.arm_labels(vectors=args.vectors)
    print()
    print(f"=== PerLTQA (en) — archive stack [{' + '.join(arms)}] ===")
    print(f"questions scored: {overall['n']}   memory units: {len(doc_of_thread)}   "
          f"query p50: {p50:.0f}ms   scoring: {total_s:.0f}s")
    print()
    print(f"  nDCG@10   {overall['ndcg10']:.3f}")
    print(f"  MRR@10    {overall['mrr10']:.3f}")
    for k in ks:
        print(f"  Recall@{k:<3} {overall['recall'][k]:.3f}")
    print()
    print("  no published retrieval baseline — single-gold, so MRR@10 is the "
          "headline and the corpus is synthetic persona material.")
    print()
    print("  by memory type:")
    for memory_type, stats in per_type.items():
        print(f"    {memory_type:<20} n={stats['n']:<6} MRR@10={stats['mrr10']:.3f}  "
              f"R@10={stats['recall'][10]:.3f}  nDCG@10={stats['ndcg10']:.3f}")
    print()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "dataset": "perltqa", "arms": arms, "n": overall["n"],
            "corpus_docs": len(doc_of_thread), "corpus_id": corpus_id,
            "ndcg10": overall["ndcg10"], "mrr10": overall["mrr10"],
            "recall": {str(k): overall["recall"][k] for k in ks},
            "query_p50_ms": p50, "per_type": per_type, "reference": {},
            "per_query": per_query,
            "performance": eval_core.performance(
                latencies, stage_samples, scoring_s=total_s,
                corpus_docs=len(doc_of_thread), arms=arms),
        }, indent=2, default=str) + "\n", encoding="utf-8")
        _log(f"wrote {args.json_out}")

    if args.fresh:
        api.close()
        shutil.rmtree(home, ignore_errors=True)
    else:
        _log(f"cached archive home at {home} (reused next run; --rebuild to refresh)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(eval_home.CACHE_ROOT),
                    help="cache root holding perltqa/ and the built homes")
    ap.add_argument("--memory-file", default=None,
                    help="explicit perltmem_en.json path")
    ap.add_argument("--qa-file", default=None, help="explicit perltqa_en.json path")
    ap.add_argument("--vectors", action="store_true",
                    help="build + query the semantic arm with the real embedder")
    ap.add_argument("--max-docs", type=int, default=None,
                    help="cap ingested memory units (smoke runs)")
    ap.add_argument("--sample", type=int, default=None,
                    help="score a deterministic sample of this many questions instead of all (the quick tier; see search_lab.eval_core.sample_queries)")
    ap.add_argument("--rebuild", action="store_true",
                    help="discard the cached build and re-ingest")
    ap.add_argument("--fresh", action="store_true",
                    help="use a throwaway home deleted on exit (no caching)")
    ap.add_argument("--json-out", default=None, help="also write the report as JSON")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
