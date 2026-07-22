# Measuring search quality

How thread-archive measures its own retrieval, what the numbers say, and what
they can and cannot certify. The instruments themselves live in `evals/`
(`evals/README.md` is the working manual); the shipped operator command is
`thread_archive eval` — a read-only self-checkup over your own archive.

A caution before any number: the headline metrics below score **single
searches in isolation**, because that is what the click protocol can label. It
is not how agents use the tool. In practice an agent fires several searches —
often in parallel, reformulating, browsing, then reading around a hit — and
the session-level question ("did the agent get to the right conversation?")
succeeds far more often than any one query's recall@10 suggests. The
single-shot numbers are the *tunable* signal, not the product experience.

## Measured against real usage, not a synthetic benchmark

Every `thread_search` an agent runs is itself archived, along with the
`thread_read` that followed — so the archive holds a click-labeled query log
of its own use. The eval harness (`evals/retrieval_eval.py --from-log`) mines
those search→read pairs: each query is one an agent actually ran, and the
thread the agent opened next is the answer that must rank. On a 17k-thread /
3.7M-event archive, 561 mined cases:

| search stack | MRR | recall@10 | p50 latency |
|---|---|---|---|
| core install (FTS5 lexical) | 0.19 | 0.33 | 0.6 s |
| + local semantic fusion | 0.25 | 0.43 | 0.6 s |
| + cross-encoder rerank | 0.25 | 0.44 | 3.1 s |

Rows are cumulative, and the labels carry a click's limits: the opened thread
was the agent's pick from what search surfaced that day — not a verdict that
nothing better existed — so relevant siblings score as misses, and a stack
that surfaces what past search never could gets no credit for it. Good
numbers here mean the stack reliably re-finds what real searches actually
delivered; they cannot certify there was nothing better to find. Semantic
fusion is the layer that pays — +10 points of recall@10 over the lexical core
at no latency cost. The cross-encoder adds about two more for 5× the latency,
which is why the pipeline auto-gates it to conceptual queries instead of
running it everywhere. Both model arms have an off switch —
`THREAD_ARCHIVE_EMBED=off` and `THREAD_ARCHIVE_RERANK=off` pin a process to
the lexical core without uninstalling the extra, for a box that wants search
cheap and free of the cold-start model load (`retrieval_eval.py
--lexical-only` measures that configuration). One more signal made the cut: a
**community-coherence re-rank** from the corpus-native embedding graph
(thread centroids → cosine kNN → Leiden — zero curation input, every embedded
conversation a node). Within a ranked pool, threads whose community carries
more of the pool's top mass get a small boost; on this protocol it lifts
recall at every depth past 1 (R@5 0.327→0.341, R@10 0.414→0.433, R@20
0.492→0.508) with MRR flat, and `evals/graph_eval.py` re-measures it. It
orders the head only when the cross-encoder stands down: the two are
alternative head orderers, and stacking coherence under the rerank measures
as a loss end-to-end (it reshuffles which candidates reach the rerank
window). On by default; `THREAD_ARCHIVE_COHERENCE=off` disables, a float
retunes gamma. Two graph signals were measured, rejected on the same
protocol, and are not in the stack: PageRank authority from the *curated*
topic graph degrades ranking monotonically with weight, because
query-independent authority floats hub threads over the specific thread a
query names. Curated thread summaries move these numbers by less than a
point — whatever their value for browsing and curation, ranked search does
not measurably ride on them. (An earlier title-as-query eval said otherwise
on every count; its queries were LLM distillations of the threads they named,
and it flattered every layer that searched other distillations. It survives
in the harness as a quick local probe; CI runs a single lean gate — semantic
arm verified alive directly, plus a small seeded sample of the log-mined
cases as a collapse alarm.)

## Beyond the click labels

The same trail powers three more instruments, each aimed at a limit of the
click labels. Every CI gate run appends its numbers to a trend ledger
(`~/.thread/archive/retrieval-trend.jsonl`), so quality is a time series, not
a launch-day screenshot, and `--mined-after` holds out only the cases mined
after a ranking change shipped. `--behavior` reports zero-label usage
signals — for every search the trail shows whether the agent opened a
result, searched again, or walked away — rates that move only when something
real moves. And `evals/retrieval_judge.py` runs a sample of the mined
queries through the production stack and has a headless `claude` grade every
top-10 thread, yielding graded precision, a calibration of the click labels
themselves, and explicit credit for relevant results the click protocol can
only score as misses. The judge grades only what production returned, from
snippets; `evals/retrieval_mine_gold.py` goes the rest of the way — one
headless `claude` *agent* per sampled query reads the originating session
for intent, sweeps the corpus with its own reformulated searches (bounded to
the corpus as of the original search's date), reads candidates, and writes a
corpus-grounded gold case. The output is an eval `--cases` file whose
per-case date bound the scoring search honors, so the one-time mining spend
buys recall-capable, deterministic labels every later eval run scores
against for free.

## The quality ladder

The instruments stack into a **quality ladder**, fastest tier first — change
a ranking weight and climb until the evidence matches the stakes. Everything
operator-run lives together in `evals/` — the search lab; `evals/README.md`
is the working manual:

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` (in every pytest run) | checked-in synthetic corpus (`tests/quality_corpus.py`), lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding + rerank models | minutes | touching the model arms |
| 2 | CI `retrieval-gate` row (`retrieval_eval.py --from-log`) | live archive, mined click labels | ~minutes | every commit, via thread-ci |
| 3 | `retrieval_eval.py` by hand, `graph_eval.py`, `retrieval_judge.py`, `search_arena.py`, `--behavior` | live archive | minutes–hours | evaluating a deliberate ranking change |
| 3½ | `retrieval_eval.py --cases` on agent-mined golds (`retrieval_mine_gold.py` to mint them) | live archive, corpus-grounded labels | seconds to score; agent-minutes per mined case | scoring against grounded labels; mining is an occasional cadence |
| 4 | `pytest -m beir` | external BEIR benchmark | tens of minutes | calibrating against published baselines |

Tier 0 is the laboratory bench: known relevance structure, deterministic,
and `run_cases(search=...)` scores any candidate ranker against the incumbent
on identical cases — the A/B seam the higher tiers then validate on real
usage.

## The search lab and the arena

That seam has a front door: **the search lab**. Every tunable of the pipeline
(ranking weights, decay constants, pool sizes) lives in one object,
`thread_archive._retrieval.SearchParams`, accepted by `search(params=...)` —
the shipped defaults ARE the production configuration. Each module in
`evals/experiments/` is one candidate configuration (a `SearchParams` value,
or a full `SEARCH` callable for changes params can't express — the contract
is in `evals/experiments/README.md`), and `evals/search_lab.py` scores the
baseline plus every experiment on identical corpus cases and prints a
leaderboard with deltas: seconds for the lexical stack, `--models` for the
fused pipeline. The corpus carries adversarial structure (a TF-spam paste
bm25 loves, a recency pair whose old twin is the lexically stronger match)
precisely so configurations *separate* — stripping the weighted ranker
measurably loses. A winner here is a direction, not a verdict; promote it by
re-measuring on tiers 2–3 before changing the defaults in
`_retrieval/params.py`.

The promotion step has its own instrument: **the arena**
(`evals/search_arena.py`). It duels a challenger from `evals/experiments/`
against the shipped configuration on real mined queries — both rankings for
each query go to a headless `claude` judge, side order randomized, labels
blind — and reports challenger wins/losses/ties with an exact sign test.
Identical rankings tie without spending a judge call, so cost scales with how
much the configurations actually disagree. Where the lab says "this direction
looks good on the synthetic corpus," the arena says "on real usage, a judge
prefers it" — the bar to clear before touching the defaults.
