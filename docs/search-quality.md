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
succeeds far more often than any one query's success@10 suggests. The
single-shot numbers are the *tunable* signal, not the product experience.

## Measured against real usage, not a synthetic benchmark

Every `thread_search` an agent runs is itself archived, along with the
`thread_read` that followed — so the archive holds a click-labeled query log
of its own use. The eval harness (`evals/retrieval_eval.py --from-log`) mines
those search→read pairs: each query is one an agent actually ran, and the
thread the agent opened next is the answer that must rank. On a 17k-thread /
3.7M-event archive, 561 mined cases:

| search stack | MRR | success@10 | p50 latency |
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
fusion is the layer that pays — +10 points of success@10 over the lexical core
at no latency cost. The cross-encoder costs 5× the latency, which is why the
pipeline auto-gates it to conceptual queries instead of running it everywhere.
Both model arms have an off switch — `THREAD_ARCHIVE_EMBED=off` and
`THREAD_ARCHIVE_RERANK=off` pin a process to
the lexical core without uninstalling the extra, for a box that wants search
cheap and free of the cold-start model load (`retrieval_eval.py
--lexical-only` measures that configuration). One more signal made the cut: a
**community-coherence re-rank** from the corpus-native embedding graph
(thread centroids → cosine kNN → Leiden — no topic-graph input, every embedded
conversation a node). Within a ranked pool, threads whose community carries
more of the pool's top mass get a small boost; on this protocol it lifts
success at every depth past 1 (S@5 0.327→0.341, S@10 0.414→0.433, S@20
0.492→0.508) with MRR flat, and `evals/graph_eval.py` re-measures it. It
orders the head only when the cross-encoder stands down: the two are
alternative head orderers, and stacking coherence under the rerank measures
as a loss end-to-end (it reshuffles which candidates reach the rerank
window). On by default; `THREAD_ARCHIVE_COHERENCE=off` disables, a float
retunes gamma. Two graph signals were measured, rejected on the same
protocol, and are not in the stack: PageRank authority from the topic
graph degrades ranking monotonically with weight, because
query-independent authority floats hub threads over the specific thread a
query names. Stored thread summaries move these numbers by less than a
point — whatever their value for browsing, ranked search does
not measurably ride on them. (An earlier title-as-query eval said otherwise
on every count; its queries were LLM distillations of the threads they named,
and it flattered every layer that searched other distillations. It survives
in the harness as a quick local probe; CI runs a lean arm-liveness gate (no
metric) alongside a gold-file regression floor. The click-label numbers stay off
the per-commit path — censored by the incumbent ranker, they wear the shape of a
quality score without being one — but the snapshot-bound gold files, grounded and
graded, ride CI as a **one-way floor** (`retrieval-gold-gate`) that fails only on
a drop below a calibrated baseline, never displaying a per-commit quality
number.)

## Beyond the click labels

The same trail powers more instruments, each aimed at a limit of the
click labels. Any harness run can append its numbers to a trend ledger
(`--trend-out` → `~/.thread/archive/retrieval-trend.jsonl`), so deliberate
measurements accumulate into a time series, and `--mined-after` holds out
only the cases mined after a ranking change shipped. `--behavior` reports zero-label usage
signals — for every search the trail shows whether the agent opened a
result, searched again, or walked away — rates that move only when something
real moves. And the gold miners produce the labels clicks can't: the
`thread_archive mine` command (package `thread_archive._mine`) runs headless
`claude` agents against a frozen corpus snapshot to mint graded gold cases —
`mine query` reads a real query's session for intent and sweeps the snapshot
deep for a corpus-grounded case; `mine topic` mints graded cases from a topic
dense with confounds; the cheaper `mine rerank` grades a retrieved pool in one
judge pass (ordering, not recall); and `mine querygen` generates
difficulty-laddered queries for a random thread to test findability. All write
snapshot-bound eval `--cases` files, so the one-time mining spend buys
coverage-capable, deterministic labels every later eval run scores against for
free. The scorer keeps first-hit success separate from true recall: success@k
asks whether any grade-2 answer ranks by k, while recall@k measures the fraction
of every case's known grade-2 set recovered. nDCG@k measures the order of the
whole graded pool, including partial answers and hard negatives.

## External calibration

The click labels and gold files both score the archive on its own corpus. The
complementary question — are the retrieval *components* competitive against
published baselines — is what the lab's three external benchmarks answer, each
running the real pipeline over a third-party corpus (`evals/beir_eval.py`,
`evals/cdr_eval.py`, `evals/haystack_eval.py`). None of these corpora resemble an
agent's own session log, so a strong number certifies the machinery, never
archive-domain quality — read each against that mismatch.

| benchmark | task | metric | lexical | +vectors | +rerank | published ref |
|---|---|---|---|---|---|---|
| BEIR scifact | scientific-claim IR | nDCG@10 | 0.302 | 0.509 | — | 0.665 BM25 / 0.68 dense |
| CDR (NVIDIA ChatRAG) | conversational retrieval | nDCG@10 | 0.101 | 0.249 | — | 0.504 best-of-16 |
| LoCoMo | multi-session dialog, turn-level | recall@10 | 0.594 | 0.621 | **0.756** | 0.662 DRAGON |
| LongMemEval-S | long-history QA, session-level | recall@10 | 0.892 | — | — | 0.710 BM25 / 0.823 Contriever |

On **LoCoMo** the full stack — lexical + semantic + auto-gated rerank — reaches
recall@10 0.756, above the specialized dense retriever DRAGON (0.662) at every
cutoff (@5 0.702 vs 0.567, @25 0.829 vs 0.767, @50 0.859 vs 0.827); the rerank
carries it, and it helps most on the entity- and precise-term categories
(single-hop, temporal) an agent's queries are made of. On **CDR** the stack
trails, on the implicit-semantic queries least like archive traffic — a
model-bound gap (nomic-embed-text is small beside the reference embedders), not
a pipeline one: recall@100 is 0.52, so the evidence is retrieved but ordered
below the top ten nDCG@10 rewards. **BEIR** is out-of-domain scientific IR
(recall@100 0.93; nDCG@10 held back by ordering on abstracts, not recall).
**LongMemEval-S** is the easy split — its references are measured on the harder
-M split — so read 0.892 as ballpark, not a matched win.

This is calibration, not the product measure: none of it scores the archive on
the agentic coding and design sessions it actually serves.

## The quality ladder

The instruments stack into a **quality ladder**, fastest tier first — change
a ranking weight and climb until the evidence matches the stakes. The scorers
and experiments live together in `evals/` — the search lab — and the agent
miners that feed them are the `thread_archive mine` command; `evals/README.md`
is the working manual:

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` (in every pytest run) | checked-in synthetic corpus (`tests/quality_corpus.py`), lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding + rerank models | minutes | touching the model arms |
| 2 | CI `retrieval-gate` (arm-liveness probes) + `retrieval-gold-gate` (gold-file regression floors) | live archive + the golds' frozen snapshot | ~a minute | every commit, via thread-ci |
| 3 | `retrieval_eval.py` by hand, `graph_eval.py`, `--behavior` | live archive | minutes | evaluating a deliberate ranking change |
| 3½ | `retrieval_eval.py --cases` on agent-mined golds (`thread_archive mine <miner>` to mint them) | frozen snapshot, corpus-grounded labels | seconds to score; agent-minutes per mined case | scoring against grounded labels; mining is an occasional cadence |
| 4 | `pytest -m beir`; `cdr_eval.py`, `haystack_eval.py --dataset …` by hand | external IR / conversational-memory benchmarks | tens of minutes (built homes cache for re-runs) | calibrating against published baselines |

Tier 0 is the laboratory bench: known relevance structure, deterministic,
and `run_cases(search=...)` scores any candidate ranker against the incumbent
on identical cases — the A/B seam the higher tiers then validate on real
usage.

## The search lab

That seam has a front door: **the search lab**. Every tunable of the pipeline
(ranking weights, decay constants, pool sizes) lives in one object,
`thread_archive._retrieval.SearchParams`, accepted by `search(params=...)` —
the shipped defaults ARE the production configuration. Each module in
`evals/experiments/` is one candidate configuration (a `SearchParams` value,
or a full `SEARCH` callable for changes params can't express — the contract
is in `evals/experiments/README.md`), and `evals/search_lab.py` scores the
baseline plus every experiment on identical corpus cases and prints a
leaderboard with deltas: seconds for the lexical stack, `--models` for the
fused pipeline. On the gold bench `--sample FRAC` (paired with `--only
<experiment>`) scores a deterministic subset of each file — the same slice every
run — so a tuning loop takes minutes instead of the full bench's tens; a subset
reads a direction, and the full bench is still the promotion bar. The corpus carries adversarial structure (a TF-spam paste
bm25 loves, a recency pair whose old twin is the lexically stronger match)
precisely so configurations *separate* — stripping the weighted ranker
measurably loses. A winner here is a direction, not a verdict.

The promotion bar is the snapshot-bound gold files: score the challenger and
the shipped configuration with `retrieval_eval.py --cases` on every minted
gold file, each over its own corpus snapshot, on both sides of the change —
tuning against one file and confirming against a held-out one — before
changing the defaults in `_retrieval/params.py`. Where the lab says "this
direction looks good on the synthetic corpus," the gold delta says "on
corpus-grounded labels from real usage, it measures better."
