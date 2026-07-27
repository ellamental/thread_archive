# Measuring search quality

How thread-archive measures its own retrieval, what the numbers say, and what
they can and cannot certify. The instruments live in `search_lab/` (`search_lab/README.md`
is the working manual — the how; this is the numbers), and they stay there: an
install ships no scoring surface. A metric with no baseline beside it is not
information, and every protocol below carries limits that have to be read with it
— so quality is measured deliberately, against a snapshot-bound baseline, by
someone holding the protocol. What an install *does* report is whether search is
degraded — the capability matrix on `thread_archive status` and the viewer's health
page — which is a state you can act on.

A caution before any number: most metrics below score **single searches in
isolation**, and where the first right answer lands. That is not how agents use the
tool. An agent fires several searches carrying terms that surround what it wants,
dedupes the results by hand, and reads around whatever looks worth opening. What
that workflow needs is not an answer ranked first but a window full of relevant
material, so the measure that matches it is **how much of what is relevant comes
back** — "What the window holds" below, which scores the fan-out end to end rather
than assuming it rescues the single-shot numbers. MRR and success@1 stay in the
report because they are sharp tuning signals, not because they describe the
experience.

## The measurement of record: the mined gold files

The baseline is a set of **agent-mined gold case files** — graded, corpus-grounded
relevance pools scored over a *frozen corpus snapshot*, so the number moves only
when the ranking code moves. `scripts/retrieval_gold_gate.py` scores one corpus
per run — a `(snapshot, gold dir)` pair — with the production ranker at the
canonical `limit=20`, prints per-file metrics, and appends to a timeseries ledger
in the gold dir it scored (`gold-runs.jsonl`, beside the cases it describes).

There are two such corpora, and a full read is both invocations: the archive's own
(the section below) and the SWE-chat hold-out (`--snap`/`--gold-dir`, see "The
hold-out corpus"). Golds bound to another snapshot skip rather than score, which
is what keeps the two from being averaged into a number that describes neither.

The gold dir holds 317 cases across 25 files: 64 `querygen`, 21 `query`, 19
`rerank`, and 213 `topic` cases spanning 22 topics. Pooled, the `topic` files read
MRR 0.781 / success@10 0.958 / nDCG@10 0.575, and their recall@10 of 0.383 is
ceiling-bound rather than low — see "What the window holds". Seven files carry
calibrated floors and are the ones the gate can fail on; the rest are scored and
reported but ungated until a floor is added for them. At the shipped configuration
(`fusion_weight=400`, `bm25_weight=100`, cross-encoder off), the floored files
read:

| miner | files | n | MRR | success@10 | recall@10 | nDCG@10 |
|---|---|---|---|---|---|---|
| `querygen` | 1 | 64 | 0.726 | 0.953 | 0.953 | 0.780 |
| `query` | 1 | 21 | 0.480 | 0.952 | 0.905 | 0.574 |
| `rerank` | 1 | 19 | 0.658 | 0.895 | 0.496 | 0.648 |
| `topic` | 4 | 34 | 0.775 | 0.941 | 0.656 | 0.631 |

The `topic` row pools four files that individually spread wide — MRR 0.552–0.917,
recall@10 0.510–0.879 — because a topic's difficulty is a property of the subject,
and subjects differ. That spread is the interesting number, not any one file's.

Rows are per *miner*, not per file, and deliberately: a gold file is named for the
topic it was mined from, and those subjects belong to a private archive. Nothing
here — or anywhere in this repo — names one. The gate discovers the corpus in the
operator's gold dir and reads each file's floor from the sidecar beside it, so
per-file numbers live in the run ledger on the machine that has the cases, and a
newly mined topic changes no document.

Pooled over all 25 files (317 cases) that reads recall@10 0.543 and nDCG@10 0.626.
The two arm-magnitude terms below are what separate it from the rank-only ranking
that scored 0.533 / 0.612; the gain concentrates in the query shapes the lexical
arm cannot match (findability's vague stratum recall@10 0.850 → 0.900, paraphrase
0.909 → 0.955) and two of the topic files pay a case of it back.

Read the metrics apart: **success@k** asks whether any grade-2 answer ranks by k,
**recall@k** measures the fraction of a case's *whole* grade-2 set that ranks,
**window fill** is that fraction against what could fit in k rather than against
the whole set, and **nDCG@k** scores the order of the entire graded 2/1/0 pool
(partial answers and hard negatives included). Which one is sharp depends on the
miner, and the four cover complementary failure modes:

- **`querygen` → findability** — a random thread, difficulty-laddered queries
  (verbatim / paraphrase / vague) that must re-find it; the single gold is that
  thread, so success and recall coincide and read as raw findability. The ladder
  separates: verbatim S@10 1.00, paraphrase 0.91, vague 0.85. Corpus-representative
  recall.
- **`query` → judged** — one `claude` agent per real trail query reads the
  originating session for intent and sweeps the snapshot deep with its own
  reformulations, crediting threads the incumbent buries. Multiple grade-2 answers,
  so **recall@10** (0.905) is the signal — the recall-capable rung.
- **`rerank` → rerank-cases** — a deep production pool graded 2/1/0 in one judge
  pass; it scores ordering *within what search retrieved*, so **nDCG** is sharp and
  recall@10 is bounded well under 1 by construction (the graded pool is far larger
  than 10). Precision.
- **`topic` → 22 files, one per topic** — a topic dense with confounds, one query
  per angle, a comprehensive graded pool (2=intended, 1=partial, 0=confound).
  Confound ranking, and the only protocol with enough answers per case to measure
  *completeness*: median grade-2 sets run from 3 on a narrow topic to 23 on a broad
  one, so **window fill** and **nDCG** are the reads and raw recall@k is bounded by
  pool size. Topics that sit in a family of near-synonyms — a project's name
  beside that project's testing, its UI, and its architecture; a subsystem beside
  its process and its queue — carry the hardest negatives, because the confounds
  are real conversations rather than synthesized ones.

Scoring is deterministic — same code, same snapshot, same digits — so a movement is
never noise. That holds because the scorer builds the corpus graph before its first
case (`_eval.warm_for_scoring`): the coherence re-rank otherwise no-ops until a
background build lands, which would split a run in two and move a file by roughly
0.02 window fill. Resolution is otherwise `1/n` per file: one case going from rank 1 to
unfound moves any metric by at most `1/n`, so anything smaller is a rank shuffle
within cases that already worked. On a 10-case topic file that unit is 0.100 and on
the smallest 7-case ones 0.143; on the 64-case findability file it is 0.016. Pooling
a protocol's files is what buys resolution below that. This is the one instrument
that can credit
an *improvement*: its grade-2 labels were mined to be complete, so a change that
surfaces a better answer scores as a gain — not, as click labels do, as a loss.

The gold files are the promotion bar. To claim "search improved," score the
challenger and the shipped configuration on **every file, over both corpora, on
both sides of the change**, and keep a hold-out: tune against one file while
another stays untouched until the confirming run. `python -m search_lab.mine`
mints these files, `--calibrate` gives a freshly mined one its floor, and
`search_lab/README.md` → "Taking a baseline" is the full protocol.

## The hold-out corpus

Every file above was mined from one archive, by one author, over one set of
projects, and hold-out discipline *within* a corpus cannot see overfitting *to*
it. The second corpus closes that: `search_lab/swechat_corpus.py` builds a home
from [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat) — public
Claude Code sessions, other people's code, ODC-BY — and the same miners mint golds
against it. It is domain-matched (agent session logs, not a third-party IR corpus)
and nothing is ever tuned against it, so it is the one corpus where a ranking
change's direction is not an artifact of the tuning loop.

It carries the same furniture as the archive's own — case files, a floor sidecar
per file, a run ledger — under `gold/` beside the download, since none of it
quotes the operator's conversations. On the 726-session corpus (snapshot
`ed739bdf…`), 310 cases across 22 files: `commit` 75, `querygen` 75, `topic` 160
over 20 repository and cross-cutting subjects.

| miner | files | n | MRR | success@10 | recall@10 | nDCG@10 |
|---|---|---|---|---|---|---|
| `commit` | 1 | 75 | 0.569 | 0.747 | 0.747 | 0.550 |
| `querygen` | 1 | 75 | 0.819 | 0.933 | 0.933 | 0.845 |
| `topic` | 20 | 160 | 0.915 | 0.994 | 0.666 | 0.715 |

Read these against the archive's numbers only as *directions*, never as levels:
the corpora differ in selectivity (see "What the window holds"), and the same
stack scores differently on each by construction. `commit` is the hardest rung
anywhere on the bench and the most trustworthy — its labels come from commit
provenance rather than from searching with the engine under test — and its
difficulty ladder separates sharply: literal queries S@10 1.000, functional 0.680,
intent 0.560. Naming what was *done* finds the session; naming *why* often does
not, and that gap is not visible on any circular protocol.

`search_lab/swechat_bench.py` exports the same golds onto the dataset's own
session ids as a standalone benchmark (queries / qrels / corpus / manifest, plus a
stdlib-only scorer), so the hold-out is reproducible by someone with no access to
this archive.

## What the window holds

The measure that matches the workflow is **window fill**: of the relevant threads
that *could* fit the window an agent reads, what share actually do. It is
ceiling-normalized (`hits@k / min(k, |gold|)`) because raw recall@k on a
multi-answer case scores the size of the gold set as much as the ranking — a case
with 23 relevant threads cannot exceed 0.43 recall@10 however well it ranks. Over
the 213 `topic` cases, against `search_lab/bm25_baseline.py` on the same snapshot at
k=10:

| measure | stack | BM25 |
|---|---|---|
| window fill, answers (grade 2) | 0.517 | 0.389 |
| window fill, answers + partials (grade ≥1) | 0.608 | 0.432 |
| union coverage across a topic's queries | 0.353 | 0.265 |

**Union coverage** scores the fan-out end to end: fire every query a topic carries,
union the windows, dedupe, and measure the share of the topic's whole relevant set
assembled. The stack leads it on all 22 topics, and per-query fill on 21 of 22, so
the margin does not wash out when an agent reformulates — reformulating lifts both
rankers by about the same factor rather than closing the gap.

Read the absolute number, not only the margin. **Union coverage of 0.353 means
that firing every angle a topic has and deduping still assembles roughly a third of
the relevant material.** That is the standing headroom, and ordering metrics cannot
see it: a file can score 0.9 MRR while surfacing a quarter of what is relevant, and
several do. No amount of reordering reaches it — only recall does.

A fill number means nothing on its own, which is what the BM25 reference is for; it
is also not comparable across corpora. The same stack *loses* to plain BM25 on the
homogeneous corpus of other people's coding sessions ("The hold-out corpus" above):
its single-gold files reduce fill to success@10, and against
`search_lab/bm25_baseline.py` on the same snapshot the stack reads 0.747 to BM25's
0.813 on `commit` cases, and ties at 0.933 on `querygen` while ranking worse inside
that reach (MRR 0.819 to 0.876, nDCG@10 0.845 to 0.890). **Selectivity** is the
corpus property that predicts the
direction — the share of the corpus a query's terms match at all. The archive sits
near 50%, so lexical matching still filters and the arms above it have something to
arbitrate; that corpus sits near 95%, where matching filters nothing, IDF-weighted
ranking is the only signal left, and the extra machinery has nothing to add.

**The circularity that bounds all of it.** `topic`, `query`, and `rerank` gold is
labeled by agents that search with the production stack, so the relevant set is
approximately what this ranker can reach across many reformulations. A completeness
metric is more exposed to that than an ordering one: a thread the stack
systematically cannot surface never enters the gold, and so can never be counted as
missing. Read 0.517 as an upper bound and 0.353 as an *overestimate* of true
coverage. Only gold whose membership is fixed outside retrieval closes it —
`commit`'s provenance labels, or the tool-use trail's record of which sessions
edited a given file, which yields multi-answer sets no ranker had a hand in
choosing.

## The stack, and what each layer buys

Production `search` federates two arms — FTS5 **lexical** and an in-process
**vector** (semantic, `nomic-embed-text`) — fuses them by reciprocal-rank fusion,
scores the merged pool with the weighted lexical **ranker** (density / phrase /
recency / content-type / bm25), then re-orders the head with the **community-coherence**
signal. A **cross-encoder** re-rank exists but sits out by default. Every tunable
is one field of `thread_archive._retrieval.SearchParams`; the shipped defaults ARE
production, and each candidate is another instance scored against them.

- **Cross-arm fusion is the dominant lever** (`fusion_weight=400`). Term density is
  unbounded, so a short doc carrying a few of a long question's common words
  outscores the fusion term's ceiling several times over and sinks the
  vocab-mismatch answers the vector arm ranked first. Weighting cross-arm
  *agreement* up to density's working scale keeps those answers reachable — the
  paraphrase and vague shapes, where the lexical arm has no purchase, are the ones
  that move. 400 is where every gold file reaches its best nDCG@10 and the head is
  at its most confident — success@1 rises with the weight, so what it buys is
  ordering, not just flatter recall. Past ~500 the vector arm starts overriding
  lexical evidence it should defer to and the keyword-shaped files give back
  recall. This is the single biggest ranking knob, and it is what lets the
  cross-encoder ship off.
- **The two arm magnitudes** (`bm25_score_weight=100`, `semantic_weight=200`) are
  what each arm *scored* a hit, beside what it *ranked* it. Both are pool-normalized
  to [0,1]: `_bm25` is FTS5's own bm25 (surfaced by selecting the hidden `rank`
  column — free, and the query plan is unchanged), `_semantic` the vector arm's
  cosine spread min-max across the pool. The rank-based terms below cannot express
  what these do, by construction: at `rrf_k` 60 a reciprocal rank spans 1.00 down to
  0.23 over a 200-deep pool, and RRF cannot tell a 0.72 cosine from a 0.55 one. The
  cosine must be *spread* rather than used raw — raw it is mostly a constant offset,
  and since the content-type multiplier scales the whole sum, a flat semantic term
  amplifies content-type preference instead of relevance; spread is worth roughly
  three times as much. Together +0.020 recall@10 / +0.025 nDCG@10 on the three
  protocol files, which were held out of the weights' tuning.
- **The bm25 term** (`bm25_weight=100`) carries the lexical arm's own placement of a
  hit (`_lex`, its peak-normalized reciprocal rank). It is the counterweight to
  density's blind spot: density is IDF-blind and length-normalized, so it weighs a
  corpus-common term exactly like the rare one that discriminates and then divides
  by length — a short doc holding a few common query words outranks the long doc
  holding the discriminating ones. 100 is a deliberate trade, not a free win: the
  query-shaped files gain (findability +.019 MRR / +.015 nDCG@10 with all three
  difficulty strata up, rerank-cases +.052 success@10) and the confound-dense topic
  files pay in recall (two topic files, −.048 and −.033 recall@10). Past
  ~400 bm25's order overrides the density evidence those files lean on and they
  break their floors. The term matters most where fusion cannot reach: `_rrf` is
  computed only when the vector arm returns, so a lexical-only search — a
  `tool_name` or `types` scope, a structural query, an archive with no embeddings —
  would otherwise rank on density alone.
- **Community-coherence re-rank** — a corpus-native embedding graph (thread
  centroids → cosine kNN → Leiden, no topic-graph input, every embedded conversation
  a node) partitions into communities; within a ranked pool, threads whose community
  carries more of the pool's top mass get a small boost
  (`score = 1/(60+rank) + γ·community_mass`, shipped γ=0.005). On by default, a
  light precision head-orderer: on `search_lab/graph_eval.py`'s log-mined regression
  protocol it lifts success at depth with MRR flat (baseline → coherence: S@5
  0.40 → 0.45, S@10 0.52 → 0.53, recall@10 0.44 → 0.46), not a headline mover.
  That harness is its regression check and the gate any new graph lever must pass.
- **Cross-encoder re-rank — off by default** (`rerank_auto=False`). It is the
  pipeline's dominant latency (2–4s on a long conceptual query, wide variance) and
  buys ~no gold-file MRR over the fused lexical+semantic+coherence stack, so the
  shipped search stays inside its latency budget without it. That verdict is
  domain-bound, not general: on turn-level dialog retrieval the same arm is worth
  +0.134 recall@10 over the same fused stack (External calibration), so it is
  dormant here, not dead. `rerank=True` still forces it (evals, and a
  quality-rebuild that must re-earn it within budget — a smaller model, a tighter
  pool); its `rerank_pool` (12) and `rerank_doc_chars` (768) knobs stay for that
  seam.

Both model arms have an off switch — `THREAD_ARCHIVE_EMBED=off` and
`THREAD_ARCHIVE_RERANK=off` pin a process to the lexical core without uninstalling
the extra, for a box that wants search cheap and free of the cold-start model load;
`THREAD_ARCHIVE_COHERENCE=off` stands the coherence re-rank down (a float retunes γ).

Three signals were measured on this bench and are **not** in the stack. Graph
**expansion** (append community-mates of the pool's top seeds) loses success@20 for
what it rescues (0.625 → 0.533, only 2 of 31 pool-misses recovered); a
topic-graph **PageRank authority** term degraded ranking monotonically with weight,
because query-independent authority floats hub threads over the specific thread a
query names — it is gone from the code. **Thread evidence**
(`thread_evidence_weight`, shipped 0.0) is the third, and it stays in the code
because it is the largest lever measured on subject-shaped queries: +0.022 nDCG@10 /
+0.017 recall@10 over the 22 `topic` files. It fails for the same reason PageRank
did, one step closer in — evidence is query-*dependent*, but it still favours the
thread that keeps returning to a subject over the one that settles it in a single
exchange, so a broad query answered by one specific conversation loses it ("how can
we improve thread_search" falls from rank 1 to past 20 on `judged`, at every weight
down to 25). A query-shape gate is the seam that would earn it.

A negative worth keeping: **pool depth is not the recall lever it looks like.**
29% of the golds' grade-2 answers never enter the 200-event candidate pool at all,
but doubling `pool_floor` to 400 does not recover them — recall@10 moves +0.002 on
the topic files and −0.010 on the protocol files, nDCG@10 down on both. A deeper
pool hands the ranker more confounds along with the extra answers, and it ranks the
confounds too. Stored **summaries** are not a rejected
signal but a deliberate content-type discount (0.6 against a user message's 1.5): a
derived digest's short length already wins the density term, so an at-parity weight
would let generated prose crowd verbatim evidence out of the top ranks. The discount
keeps summaries findable while making them yield to any primary source that matches
comparably.

## Latency

Warm search is FTS-dominated with the cross-encoder off: **p50 ~800 ms, p95 ~1.5 s**.
Three properties hold the tail there — auto-re-rank sits out, code-identifier queries
ride indexed token-MATCH fallbacks rather than a full-table substring scan, and cold
model loads are deferred to warm so they never land inside a request. The
cross-encoder is both the top quality lever and the top latency, so re-enabling it is
a budgeted decision, not a free one.
`retrieval_gold_gate.py --latency` measures warm latency over the same queries it
scores for quality (p50/p95/p99 by stage and query shape) and prints the joint
report, so a `--set` tuning decision reads on both axes at once;
`~/.thread/archive/latency-baseline.json` records the baseline, and the per-search
usage ledger carries a per-stage breakdown that makes any latency change
self-diagnosing.

The breakdown is the three arm totals (`fts_ms` / `semantic_ms` / `rerank_ms`) plus,
when the vector arm ran, its internal split: `embed_ms` (the query embedding),
`scope_ms` (the id-mask query a scoped search runs before the KNN), `matrix_ms`
(serving the KNN matrix, which builds the vector pack inline on a process's first
query — flagged `matrix_built`), `knn_ms` (the matvec and top-k), and `hydrate_ms`
(candidate ids back into hits). The sub-stages nest inside `semantic_ms` rather than
adding to it. This split exists because the arm totals name an arm and nothing else,
which is useless in the tail: a 28-second `semantic_ms` can be a cold model, a mask
query over millions of ids, or a pack read off disk, and those have nothing in
common but the bucket they were charged to.

Two flags separate the cold regime from the warm one. `cold` marks a search that
paid a model load inside the request, attributed to the arm that paid it
(`embed_cold` / `rerank_cold`) — per-arm because a cross-encoder that is installed
and never invoked is permanently "available and not loaded", and a single flag folded
that in and read cold on every search forever. Against them, a `warm` ledger row
records each `warm_models` pass — how long a process takes to become useful, split by
stage — so a slow first search can be told apart from warming that is broken.

Alongside the timings, each record carries what else was competing for the machine:
`inflight` (concurrent retrieval calls in this process), `refreshing` (background
matrix/graph rebuilds in flight), and `wal_age_s` (seconds since anything last wrote
the index, off the SQLite WAL's mtime — the cross-process signal, since reads never
touch the WAL). Timings say where a search spent its time; these say whether it had
the machine to itself while spending it, which a duration alone cannot tell apart.
Fields are omitted when they say nothing, so an idle-machine search records none.

`duration_ms` is retrieval only; `render_ms` beside it is the formatting that turns
hits into the text the agent reads. Their sum is the tool call's wall clock, and
keeping them apart distinguishes a slow *search* from a slow *answer*. Searches and
reads that raise are recorded too, marked `failed`, with the time they burned —
dropping them would bias every percentile toward the calls that happened to succeed.

## Beyond the gold files

The archive's own tool-use trail powers two more instruments, each aimed at a limit
of the gold files:

- **Click labels (`retrieval_eval.py --from-log`)** mine real `thread_search`→`thread_read` pairs from
  the trail: the gold is whatever thread the agent opened, a subset of what search
  surfaced *that day*. The labels are censored by the incumbent ranker — a change
  that surfaces different-better results scores as a loss — so this is an **alarm,
  not a baseline**: run it by hand to ask "did something collapse," never to credit
  a change. Its lasting value to the bench is as a **sampling frame** — real query
  shapes to seed the gold miner with. Nothing runs it on a cadence.
- **Behavioral signals (`retrieval_eval.py --behavior`)** report zero-label usage rates — for every
  search, whether the agent opened a result, searched again, or walked away — rates
  that move only when something real moves.

## External calibration

The gold files score the archive on its own corpus. The complementary question — are
the retrieval *components* competitive against published baselines — is what three
external benchmarks answer, each running the real pipeline over a third-party corpus.
None of these corpora resemble an agent's own session log, so a strong number
certifies the machinery, never archive-domain quality — read each against that
mismatch. Each also reports its own field's metric conventions rather than this
archive's (linear-gain nDCG, where the gold files use exponential), which is the
point of running them: a number is only a yardstick if it means what the
leaderboard beside it means. What they share with the gold bench is the
configuration under test — one arm-pinning path, so `lexical` names the same stack
everywhere — and one cache root, `~/.cache/thread-evals`.

| benchmark | task | metric | lexical | +vectors | +rerank | published ref |
|---|---|---|---|---|---|---|
| BEIR scifact (`beir_eval.py`) | scientific-claim IR | nDCG@10 | 0.445 | 0.658 | — | 0.665 BM25 / 0.68 dense |
| CDR (`cdr_eval.py`) | conversational retrieval | nDCG@10 | 0.230 | 0.492 | — | 0.504 best-of-16 |
| LoCoMo (`haystack_eval.py`) | multi-session dialog, turn-level | recall@10 | 0.595 | 0.653 | **0.787** | 0.662 DRAGON |
| LongMemEval-S (`haystack_eval.py`) | long-history QA, session-level | recall@10 | 0.912 | — | — | 0.710 BM25 / 0.823 Contriever |

On the shipped default (no cross-encoder) the fused stack lands at 97–99% of every
comparable reference, and above the reference on LongMemEval-S. Nothing is tuned
against these corpora, so they are held out in the arithmetic sense — but they are
out-of-domain, so a disagreement between them and the gold files is as easily a
domain gap as an artifact (the `bm25_weight` split below is exactly that). The
**domain-matched** hold-out is the SWE-chat corpus above: same task, same document
shape, other people's code. Score both after a defaults change, and weigh a
disagreement accordingly.

On **LoCoMo**, with the cross-encoder **forced on** (which production does not do),
recall@10 reaches 0.787 — above the specialized dense retriever DRAGON (0.662) at
every cutoff (@5 0.731 vs 0.567, @25 0.842 vs 0.767, @50 0.869 vs 0.827), helping
most on the entity- and precise-term categories (single-hop 0.892, temporal 0.842)
an agent's queries are made of. The arms are additive here: the re-rank is worth
+0.134 recall@10 on top of fusion, where on the gold files it buys ~no MRR over that
same fused stack. The cross-encoder's value is domain-bound, and turn-level dialog is
where it pays — at a price, roughly an hour for this pass against a minute for the
+vectors one over the identical corpus.

On **CDR** the stack reaches 0.492 against the 0.504 best-of-16 reference, at
recall@100 0.706. A weak number here is a ranking-weight symptom, not an
embedder-size one: the same `nomic-embed-text` spans a nearly two-fold range on this
benchmark under different ranking weights, so reach for the ranker before the model.

**BEIR** is out-of-domain scientific IR, and the fused 0.658 sits inside the
harness's own ±0.05 verdict band around BM25 ("in BM25 ballpark", −0.007), with
recall@100 0.958. The **lexical arm is the standing gap**: at 0.445 it still trips
that same harness's `BELOW BM25 — investigate`, and closing it is a knob-turn away —
`bm25_weight` near 2000 reaches the reference — that the gold files refuse, because
past ~400 the topic files break their floors. The weight is set in domain and the
benchmark is left disagreeing, which is the arrangement worth keeping: this suite is
the alarm, not the objective.

The gap between these corpora is itself a finding. The bm25 term's effective
strength scales **inversely with document length**, because density is normalized to
a fixed `density_norm_chars` window but never bounded — a 116-char turn matching
three terms scores `3 × 500/116 ≈ 13`, where a 1500-char abstract matching three
scores `1`. So a weight calibrated on archive-length documents barely registers on
turn-level corpora: LoCoMo (median turn 116 chars, every turn under the 500-char
window) is unmoved by it, while session-level LongMemEval and abstract-level BEIR
both move. Read a flat number on a short-document corpus as the term being out of
scale there, not as the term doing nothing.

**LongMemEval-S** is the easy split, scored over its 470 non-abstention questions —
its references are measured on the harder -M split — so read 0.912 as ballpark, not
a matched win. This is calibration, not the product measure: none of it scores the
archive on the agentic coding and design sessions it actually serves.

## What rides CI, and the quality ladder

Only two things gate every commit, and neither displays a quality number — by
design, because a per-commit metric invites being read as a quality score, which the
click-label protocols are censored against being:

- **Tier 0** — `tests/test_search_quality.py` (metric floors near-saturated on the
  checked-in synthetic corpus: they can only fall, a breakage detector),
  `tests/test_search_recall_shape.py` (the exhaustive and chronological shapes the
  ordering metrics can't score — every thread carrying a term is enumerable, first
  and last mention are answerable — on nonce-term golds that are true by
  construction) and `tests/test_reality_mechanisms.py` (deterministic pipeline
  contracts — content types indexed, MCP default scope, reindex preserving what was
  findable, the cross-encoder's gate/window paths) run in every pytest pass.
- **`retrieval-gate`** (CI, `ci.toml`) — an **arm-liveness probe only**
  (`retrieval_eval.py --probes-only --require-semantic --require-rerank`): it asserts
  the embedding and cross-encoder arms actually load, so a dead model can't silently
  degrade fused search to lexical while every row stays green. No metric run rides it.

The grounded gold-file scoring is a **deliberate run, not a CI row** — its ~140
model-loaded searches run at the edge of the 600 s runner cap, so it timed the sweep
out under load. `retrieval_gold_gate.py` is where it lives now, doubling as the
current-state read and the interactive tuning loop (`--set field=value` to score a
candidate, `--cache` to persist candidate pools across processes for a ~7× re-run
speedup, `--fail-early` to stop once a floor is provably unreachable, `--latency` for
the speed axis).

The instruments stack into a **quality ladder**, fastest tier first — climb until
the evidence matches the stakes:

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` + `tests/test_search_recall_shape.py` + `tests/test_reality_mechanisms.py` (every pytest run) | checked-in synthetic corpus, lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding + rerank models | minutes | touching the model arms |
| 2 | CI `retrieval-gate` (arm-liveness probes) | live archive | ~a minute | every commit, via thread-ci |
| 3 | `retrieval_gold_gate.py` (current-state read + tuning loop), `graph_eval.py` | live archive + the golds' frozen snapshot | seconds to minutes | evaluating a deliberate ranking change |
| 3¼ | `retrieval_gold_gate.py --snap … --gold-dir …` over the SWE-chat corpus | the domain-matched hold-out, its own snapshot | minutes | confirming a change that looked good on the archive's own golds |
| 3½ | `python -m search_lab.mine <miner>` to mint fresh golds, then `--calibrate` to floor them | frozen snapshot, corpus-grounded labels | seconds to score; agent-minutes per mined case | when a file's snapshot goes stale |
| 4 | `pytest -m beir`; `cdr_eval.py`, `haystack_eval.py --dataset …` | external IR / conversational-memory benchmarks | tens of minutes | calibrating against published baselines |

The tunables all live in one object — `SearchParams` (`_retrieval/params.py`) — and a
candidate configuration is another instance of it, passed through `search(params=...)`
and scored against the incumbent on identical cases. The gold gate drives that seam one
knob at a time; `tests/test_search_params.py` keeps it open. A direction that looks good
on the synthetic corpus is only a direction — the gold-file delta says "on
corpus-grounded labels from real usage, it measures better," and only the second can
promote a change.
