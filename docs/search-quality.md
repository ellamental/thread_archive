# Measuring search quality

How thread-archive measures its own retrieval, what the numbers say, and what
they can and cannot certify. The instruments live in `search_lab/`
(`search_lab/README.md` is the working manual — the how; this is the numbers),
and they stay there: an install ships no scoring surface. A metric with no
baseline beside it is not information, and every protocol below carries limits
that have to be read with it. What an install *does* report is whether search is
degraded — the capability matrix on `thread-archive status` and the viewer's
health page — which is a state you can act on.

**The headline finding is a negative one, and it governs the rest of this page:
no protocol that scores this archive's own corpus can certify that search is
good, so nothing gates on one.** Two independent failures put it there — labels
made by searching are circular, and labels made from a record outside search come
with queries nobody asked. The section below is where that was worked out, with
the numbers that showed it. What survives as a quality claim is the external
calibration further down: public corpora, other people's labels, read beside a
published baseline.

A caution before any number: everything below scores **single searches in
isolation**, and where the first right answer lands. That is not how agents use
the tool. An agent fires several searches carrying terms that surround what it
wants, dedupes the results by hand, and reads around whatever looks worth
opening. What that workflow needs is not an answer ranked first but a window full
of relevant material — and **nothing on this bench measures that.** See
"What is not measured" for why, and for what it would take.

## The admission rule

**A gold label may not be established by searching the corpus with the engine
under test — and where that is impossible, the pool it came from must be wider
than one system.** No protocol in this lab clears it, which is why none runs.

An agent that sweeps the corpus with the production ranker to assemble a relevance
pool produces labels that describe *what that ranker already reaches*. A thread
the stack systematically cannot surface never enters the gold, and so can never
be counted as missing. Every number scored against such a pool is an upper bound
on itself, biased optimistic by an unknown amount — and the bias is invisible
from inside, because labeler and ranker share it exactly. A completeness metric
is more exposed to it than an ordering one, but neither escapes.

There is a hard constraint underneath, and it is worth stating plainly because it
is what shapes the whole bench: **you cannot have both a real query and a complete
answer set from the same record.** A real query's answers were never enumerated by
anyone — the only trace is what search returned and what the agent opened, which is
the censored click label. So the two rungs trade against each other:

| rung | labels | queries |
|---|---|---|
| strong | fixed by a record outside the search stack | authored from an artifact |
| weak | judged over a union of independent systems | **observed** — real traffic |

Neither rung clears the bar on its own. The weak rung is not "circular anyway" —
pooling several independent retrievers plus a random draw bounds the bias at *what
no pooled system finds*, which shrinks as systems are added and can be probed by
asking what each system found alone, where a single-system pool's bias can only be
asserted. But it is still bounded rather than clean. And the strong rung escapes
circularity only by authoring the query from an artifact, which buys label
independence at the cost of query realism: **a query written to have a knowable
answer is not shaped like one an agent types.** That is the second failure, it has
no fix inside this constraint, and it is why the strong rung produces material for
an experiment rather than a score.

## What the commit-provenance protocol showed

Retired as a standard, kept as a finding. This was the most defensible local
protocol on the bench — labels fixed outside retrieval, a domain-matched corpus,
nothing ever tuned against it — and its numbers are the clearest evidence for the
conclusion at the top of this page. The case file it produced now lives in
`~/dev/retired-gold/`.

The corpus was frozen to a snapshot so a number moved only when the ranking code
moved, and it was scored with the production ranker at the canonical `limit=20`.

The corpus is [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat) —
public Claude Code sessions, other people's code, ODC-BY, built by
`search_lab/swechat_corpus.py`. It is the only corpus on this bench that ships
session ↔ commit provenance, which is what a label has to be fixed by. It is
domain-matched (agent session logs, not a third-party IR corpus) and nothing is
ever tuned against it.

The protocol read a *linkage file* pairing each session with the commits it
demonstrably authored; one agent read only the commit — message and diff — and
authored queries for it; the linked session was the answer. No search runs during
labeling, and since the agent never reads the target thread there is no
vocabulary leakage either: the query is written from an artifact outside the
corpus, which is also how a person searches ("where did we change the retry
backoff") — from the work they remember, not from the conversation they are
trying to find.

On the 5,124-session corpus (snapshot `c4137bd4…`), 75 cases:

| n | MRR | success@10 | recall@10 | nDCG@10 |
|---|---|---|---|---|
| 75 | 0.514 | 0.627 | 0.627 | 0.465 |

Single-gold cases, so recall@10 and success@10 coincide and read as raw
findability. The difficulty ladder is the sharp part, and it separates hard:

| tier | success@10 |
|---|---|
| literal (names identifiers from the diff) | 0.920 |
| functional (what the change does, no distinctive identifiers) | 0.640 |
| intent (the goal, from memory, weeks later) | 0.320 |

**Naming what was done finds the session; naming why often does not.** That gap
is the most useful thing this protocol said, and it is not visible on any protocol
whose labels came from retrieval — a circular pool cannot contain the answers the
ranker misses, so it cannot show the miss. Read it as a lead about the intent
shape, not as a measured rate: the queries were authored from commits, so how
often *agents* ask that way is not something these cases can say.

**Plain BM25 beats this on the same cases** — 0.563 MRR and 0.773 success@10,
ahead at every tier but `intent`. The fused stack is behind a bag-of-words
baseline on the one corpus here whose labels nothing searched for, which is the
number to argue with before any other on this page. `bm25_baseline.py` is the
reference these are read against and it is currently above them.

Grades 1 and 0 in the pool are structural proxies, not verified labels: sibling
sessions in the same repository grade themselves by file overlap (overlapping 1,
disjoint 0), so the confound pool costs no tokens. Only the 2 is a strong claim.
Read the gold-2 recall numbers as the trustworthy signal and nDCG over the proxy
pool as directional.

**Selectivity** — the share of the corpus a query's terms match at all — is the
corpus property that predicts the BM25 gap above: this corpus sits near 95%,
where lexical matching filters nothing, IDF-weighted ranking is the only signal
left, and the arms above it have nothing to arbitrate. An archive of mixed
conversational material sits far lower, where the extra machinery has something
to do. That is an explanation and not an excuse, and until the `edited` and
`pooled` files below carry cases it stays a hypothesis this bench cannot test —
because the corpus where it would matter is the one nothing has scored yet.

Scoring is deterministic — same code, same snapshot, same digits — so a movement
is never noise. That holds because the scorer builds the corpus graph before its
first case (`search_lab/eval_core.py`'s `warm_for_scoring`): the coherence
re-rank otherwise no-ops until a background build lands, which would split a run
in two. Resolution is `1/n`: one case going from rank 1 to unfound moves any
metric by at most 0.013 on this file, so anything smaller is a rank shuffle
within cases that already worked.

None of that licenses "search improved" — see the top of this page for why, and
`search_lab/README.md` → "Taking a baseline" for what does.
`search_lab/swechat_bench.py` exports these cases onto the dataset's own session
ids as a standalone benchmark (queries / qrels / corpus / manifest, plus a
stdlib-only scorer), so the protocol and its limits are reproducible by someone
with no access to this machine — which is the right way for a retired standard to
survive.

## What is not measured

Naming the holes, because a bench this narrow is easy to over-read. They are
holes, not plans: nothing here is waiting on a protocol that was designed and not
yet run.

- **The operator's own archive.** No file scores it. It carries no session ↔
  commit provenance, so the one protocol whose labels were fixed outside search
  could never have run against it, and the corpus that *was* scored is other
  people's code in other people's repos. Every archive-domain claim rests on the
  synthetic tier-0 corpus (which proves only that nothing broke) and on inference
  from a corpus with very different selectivity.
- **Completeness.** Every scored case has exactly one right answer, so the bench
  reads *findability* and nothing about whether a window holds the several threads
  that bear on a subject — which is what the fan-out workflow actually needs. The
  external `beam` row is the only multi-answer measurement on this bench, and it
  is not archive-domain.
- **Whether the shipped weights are right.** They were arrived at against
  protocols that no longer qualify, and the numbers that justified them are not
  re-derivable. `_retrieval/params.py` states this at the top of its evidence
  list: the mechanism arguments for each term stand, the per-file deltas do not.
  The defaults are what ships and what every candidate is scored against — not a
  configuration this bench has confirmed.
- **Anything a real query looks like.** The gold fixed circularity; it did not
  fix realism, and on surface form it is *further* from observed traffic than
  what it replaced. Against the usage ledger's 298 recorded searches:

  | | gold | observed |
  |---|---|---|
  | query length, median | 20 words / 122 chars | 4 words / 31 chars |
  | shape | grammatical descriptive sentence | bag of terms (`watcher ingest lock`) |
  | carries a scope/shape param | 0% | 43% (`content_type` 37%, `group=browse` 35%, `match` 31%) |
  | paginates | 0% | 49% |
  | uses an operator (`OR`, quotes, `|`) | 0 of 75 | 8 |
  | subject | "find the session that made this code change" | infra debugging, ontology work, personal and emotional material |

  The `intent` tier — the rung meant to model recall-from-memory, and the most
  informative one, since `literal` is close to a smoke test — is 21 of 25 cases
  opening with the same "that session where…" template. That is one phrasing with
  25 fillers, not 25 samples of how anyone searches. The ladder varies the
  *phrasing of one authoring process*; it does not span query shapes.

  Two consequences. The **browse and scoped code paths are unmeasured entirely** —
  a third of real searches, and `group='browse'` is a different retrieval shape,
  not a filter on this one. And a weight swept against 20-word queries may not
  hold for 4-word ones: density normalizes matched terms against a fixed
  `density_norm_chars` window, and the OR-fallback tier in `fts.py` fires when the
  strict all-terms pass comes up short — far likelier on a long query than a
  three-token one. So the two populations exercise different parts of the
  pipeline.

  Closing this would mean labelling the observed queries, whose answer sets exist
  in no record — so the labels would have to be judged over a pool retrieval
  assembled, which is the weak rung. A probe of that shape is worth one finding:
  over four sampled ledger queries, **`bm25` alone contributes up to 8 documents
  the fused stack never returned**. Whatever grades them, a single-system pool
  would have silently missed those.

- **Bench latency over a representative query set.** The warm-latency figures
  below come from the gold queries, which is the same 75-query population above.
  `latency_replay.py` over the usage ledger is the instrument that does not have
  this problem.

## The stack, and what each layer buys

Production `search` federates two arms — FTS5 **lexical** and an in-process
**vector** (semantic, `nomic-embed-text`) — fuses them by reciprocal-rank fusion,
scores the merged pool with the weighted **ranker** (density / phrase / recency /
content-type / fusion / the lexical arm's own bm25 rank and score / the vector
arm's spread cosine), then re-orders the head with the **community-coherence**
signal. Every tunable is one field of
`thread_archive._retrieval.SearchParams`; the shipped defaults ARE production,
and each candidate is another instance scored against them.

What follows is the *mechanism* each term exists for — a property of the scoring
function, and true independently of any measurement — plus the measurements that
survive the admission rule.

- **Cross-arm fusion** (`fusion_weight=400`). Term density is unbounded, so a
  short doc carrying a few of a long question's common words outscores the fusion
  term's ceiling several times over and sinks the vocab-mismatch answers the
  vector arm ranked first. Weighting cross-arm *agreement* up to density's working
  scale keeps those answers reachable — the paraphrase and vague shapes, where the
  lexical arm has no purchase, are the ones that move. Past ~500 the vector arm
  starts overriding lexical evidence it should defer to and keyword-shaped queries
  give back recall.
- **The two arm magnitudes** (`bm25_score_weight=100`, `semantic_weight=200`) are
  what each arm *scored* a hit, beside what it *ranked* it. Both are
  pool-normalized to [0,1]: `_bm25` is FTS5's own bm25 (surfaced by selecting the
  hidden `rank` column — free, and the query plan is unchanged), `_semantic` the
  vector arm's cosine spread min-max across the pool. The rank-based terms cannot
  express what these do, by construction: at `rrf_k` 60 a reciprocal rank spans
  1.00 down to 0.23 over a 200-deep pool, and RRF cannot tell a 0.72 cosine from a
  0.55 one. The cosine must be *spread* rather than used raw — raw it is mostly a
  constant offset, and since the content-type multiplier scales the whole sum, a
  flat semantic term amplifies content-type preference instead of relevance.
- **The bm25 term** (`bm25_weight=100`) carries the lexical arm's own placement of
  a hit (`_lex`, its peak-normalized reciprocal rank). It is the counterweight to
  density's blind spot: density is IDF-blind and length-normalized, so it weighs a
  corpus-common term exactly like the rare one that discriminates and then divides
  by length — a short doc holding a few common query words outranks the long doc
  holding the discriminating ones. The commit golds credit it: +.011 MRR / +.007
  nDCG@10 / +.003 recall@10 against `bm25_weight=0`. Where it has no substitute is
  the search fusion cannot reach: `_rrf` is computed only when the vector arm
  returns, so a lexical-only search — a `tool_name` or `types` scope, a structural
  query, an archive with no embeddings — would otherwise rank on density alone.
- **Community-coherence re-rank** — a corpus-native embedding graph (thread
  centroids → cosine kNN → Leiden, no topic-graph input, every embedded
  conversation a node) partitions into communities; within a ranked pool, threads
  whose community carries more of the pool's top mass get a small boost
  (`score = 1/(60+rank) + γ·community_mass`, shipped γ=0.005). On by default, and
  the smallest lever here. `search_lab/graph_eval.py` scores it over log-mined
  click labels (187 cases): S@5 0.401 → 0.428, S@10 0.513 → 0.519, recall@10 0.417
  → 0.426, S@1 0.203 → 0.193, MRR flat. **Those labels are censored by the
  incumbent** — the gold is whatever thread an agent opened, out of what search
  surfaced that day — so that harness is a regression check and the gate any new
  graph lever must pass, never evidence the re-rank helps.

The model arm has an off switch — `THREAD_ARCHIVE_EMBED=off` pins a process to
the lexical core without uninstalling the extra, for a box that wants search cheap
and free of the cold-start model load; `THREAD_ARCHIVE_COHERENCE=off` stands the
coherence re-rank down (a float retunes γ).

Three signals were measured and are **not** in the stack. Graph **expansion**
(append community-mates of the pool's top seeds) loses success@20 for what it
rescues (0.636 → 0.524, only 2 of 50 pool-misses recovered); a topic-graph
**PageRank authority** term degraded ranking monotonically with weight, because
query-independent authority floats hub threads over the specific thread a query
names — it is gone from the code. **Thread evidence**
(`thread_evidence_weight`, shipped 0.0) is the third, and it stays in the code
because it fails for a reason worth keeping written down, one step closer in than
PageRank: evidence is query-*dependent*, but it still favours the thread that
keeps returning to a subject over the one that settles it in a single exchange, so
a broad query answered by one specific conversation loses it — at every weight
down to 25. A query-shape gate is the seam that would earn it.

A negative worth keeping: **pool depth is not the recall lever it looks like.**
Doubling `pool_floor` from 200 to 400 does not recover the grade-2 answers that
never enter the candidate pool — a deeper pool hands the ranker more confounds
along with the extra answers, and it ranks the confounds too. Stored **summaries**
are not a rejected signal but a deliberate content-type discount (0.6 against a
user message's 1.5): a derived digest's short length already wins the density
term, so an at-parity weight would let generated prose crowd verbatim evidence out
of the top ranks. The discount keeps summaries findable while making them yield to
any primary source that matches comparably.

## Latency

**Two numbers, and the gap between them is the finding.**

*On the bench*, warm search is FTS-dominated: p50 ~620 ms, p95 ~1.7 s, p99 ~2.2 s,
of which the lexical arm is p50 537 ms / p95 1572 ms — the vector arm's whole cost
is ~50 ms at the median. Those came off a curated query set; replaying the
*observed* population instead lands in the same regime (`latency_replay.py`:
p50 ~230 ms, p95 ~1.4 s, p99 ~2.4 s), so query shape is not what separates the
bench from the product.

*As actually served*, it is an order of magnitude worse. Over the searches the
usage ledger recorded warm with a stage breakdown, **p50 ~2.7 s, p95 ~14.5 s,
p99 ~45 s** — and the arms account for about a second of that median (`fts_ms` p50
636 ms, `semantic_ms` p50 345 ms), so most of a served search's wall clock is not
in any stage the probe names. The two differ in conditions as well as code path —
the bench runs one query at a time in a process doing nothing else, the ledger is a
long-lived MCP server under a client that pipelines, against a store ingest is
writing — but nothing here establishes how the gap divides between those, and the
contention fields (`inflight` / `refreshing` / `wal_age_s`) are the seam that
would. **Quote the served numbers when the question is what search costs an agent,
and the bench numbers only when the question is whether a ranking change moved the
clock** — the two are different regimes, and no ranking decision has ever been made
against the served one.

Two properties hold the *bench* tail where it is — code-identifier queries ride
indexed token-MATCH fallbacks rather than a full-table substring scan, and cold
model loads are deferred to warm so they never land inside a request.
`latency_replay.py` measures warm latency over the searches agents actually ran
(p50/p95/p99 by stage and query shape); `--baseline` records the reference the
next run diffs against in
`~/.thread/archive/latency-baseline-observed.json`, and the per-search usage
ledger carries a per-stage breakdown that makes any latency change
self-diagnosing.

The breakdown is the two arm totals (`fts_ms` / `semantic_ms`) plus, when the
vector arm ran, its internal split: `embed_ms` (the query embedding), `scope_ms`
(the id-mask query a scoped search runs before the KNN), `matrix_ms` (serving the
KNN matrix, which builds the vector pack inline on a process's first query —
flagged `matrix_built`), `knn_ms` (the matvec and top-k), and `hydrate_ms`
(candidate ids back into hits). The sub-stages nest inside `semantic_ms` rather
than adding to it. This split exists because the arm totals name an arm and nothing
else, which is useless in the tail: a 28-second `semantic_ms` can be a cold model,
a mask query over millions of ids, or a pack read off disk, and those have nothing
in common but the bucket they were charged to.

Two flags separate the cold regime from the warm one. `cold` marks a search that
paid a model load inside the request, attributed to the arm that paid it
(`embed_cold`). Against them, a `warm` ledger row records each `warm_models` pass —
how long a process takes to become useful, split by stage — so a slow first search
can be told apart from warming that is broken.

Alongside the timings, each record carries what else was competing for the machine:
`inflight` (concurrent retrieval calls in this process), `refreshing` (background
matrix/graph rebuilds in flight), and `wal_age_s` (seconds since anything last
wrote the index, off the SQLite WAL's mtime — the cross-process signal, since reads
never touch the WAL). Timings say where a search spent its time; these say whether
it had the machine to itself while spending it, which a duration alone cannot tell
apart. Fields are omitted when they say nothing, so an idle-machine search records
none.

`duration_ms` is retrieval only; `render_ms` beside it is the formatting that turns
hits into the text the agent reads. Their sum is the tool call's wall clock, and
keeping them apart distinguishes a slow *search* from a slow *answer*. Searches and
reads that raise are recorded too, marked `failed`, with the time they burned —
dropping them would bias every percentile toward the calls that happened to
succeed.

## Zero-label instruments

The archive's own tool-use trail powers two hand-run instruments. **Neither mints
gold** — the admission rule is about labels a benchmark is scored against, and
these produce no case files.

- **Click labels (`retrieval_eval.py --from-log`)** mine real
  `thread_search`→`thread_read` pairs from the trail: the gold is whatever thread
  the agent opened, a subset of what search surfaced *that day*. Censored by the
  incumbent ranker in exactly the way the admission rule describes — a change that
  surfaces different-better results scores as a loss — so this is an **alarm, not a
  baseline**: run it by hand to ask "did something collapse," never to credit a
  change, and never cite a from-log delta as evidence. Its lasting value is as a
  **sampling frame**: real query shapes to draw a population from. Nothing runs it on a
  cadence.
- **Behavioral signals (`retrieval_eval.py --behavior`)** report zero-label usage
  rates — for every search, whether the agent opened a result, searched again, or
  walked away. No labels at all, so nothing censors them; rates that move only when
  something real moves.

## External calibration

The commit golds score the archive's machinery on one corpus. The complementary
question — are the retrieval *components* competitive against published baselines
— is what three external benchmarks answer, each running the real pipeline over a
third-party corpus. None of these corpora resemble an agent's own session log, so
a strong number certifies the machinery, never archive-domain quality — read each
against that mismatch. Each also reports its own field's metric conventions rather
than this archive's (linear-gain nDCG, where the gold files use exponential), which
is the point of running them: a number is only a yardstick if it means what the
leaderboard beside it means. What they share with the gold bench is the
configuration under test — one arm-pinning path, so `lexical` names the same stack
everywhere — and one cache root, `~/.cache/thread-evals`.

| benchmark | task | metric | lexical | +vectors | published ref |
|---|---|---|---|---|---|
| BEIR scifact (`beir_eval.py`) | scientific-claim IR | nDCG@10 | 0.579 | 0.709 | 0.665 BM25 / 0.68 dense |
| CDR (`cdr_eval.py`) | conversational retrieval | nDCG@10 | 0.230 | 0.494 | 0.504 best-of-16 |
| LoCoMo (`haystack_eval.py`) | multi-session dialog, turn-level | recall@10 | 0.615 | 0.672 | 0.662 DRAGON |
| LongMemEval-S (`haystack_eval.py`) | long-history QA, session-level | recall@10 | 0.941 | — | 0.710 BM25 / 0.823 Contriever |

`python -m search_lab benchmark` records every row of this table with the corpus
and code that produced it (`~/.thread/archive/bench-runs.jsonl`), which is where
these numbers come from.

On the shipped default the fused stack meets or clears every comparable reference
except CDR's, where it sits at 98%. Nothing is tuned against these corpora, so they
are held out in the arithmetic sense — but they are out-of-domain, so a
disagreement between them and the commit golds is as easily a domain gap as an
artifact.

On **CDR** the stack reaches 0.494 against the 0.504 best-of-16 reference, at
recall@100 0.687. A weak number here is a ranking-weight symptom, not an
embedder-size one: the same `nomic-embed-text` spans a nearly two-fold range on
this benchmark under different ranking weights, so reach for the ranker before the
model.

**BEIR** is out-of-domain scientific IR, and the fused 0.709 sits above the BM25
reference (+0.044, past the harness's own ±0.05 "in BM25 ballpark" band), with
recall@100 0.962. The **lexical arm is the standing gap**: at 0.579 it still trips
that same harness's `BELOW BM25 — investigate`.

The gap between these corpora is itself a finding. The bm25 term's effective
strength scales **inversely with document length**, because density is normalized
to a fixed `density_norm_chars` window but never bounded — a 116-char turn matching
three terms scores `3 × 500/116 ≈ 13`, where a 1500-char abstract matching three
scores `1`. So a weight calibrated on archive-length documents barely registers on
turn-level corpora: LoCoMo (median turn 116 chars, every turn under the 500-char
window) is unmoved by it, while session-level LongMemEval and abstract-level BEIR
both move. Read a flat number on a short-document corpus as the term being out of
scale there, not as the term doing nothing.

**LongMemEval-S** is the easy split, scored over its 470 non-abstention questions —
its references are measured on the harder -M split — so read 0.941 as ballpark, not
a matched win. This is calibration, not the product measure: none of it scores the
archive on the agentic coding and design sessions it actually serves.

## What rides CI, and the quality ladder

Only two things gate every commit, and neither displays a quality number — by
design, because a per-commit metric invites being read as a quality score, which
the click-label protocols are censored against being:

- **Tier 0** — `tests/test_search_quality.py` (metric floors near-saturated on the
  checked-in synthetic corpus: they can only fall, a breakage detector),
  `tests/test_search_recall_shape.py` (the exhaustive and chronological shapes the
  ordering metrics can't score — every thread carrying a term is enumerable, first
  and last mention are answerable — on nonce-term golds that are true by
  construction) and `tests/test_reality_mechanisms.py` (deterministic pipeline
  contracts — content types indexed, MCP default scope, reindex preserving what was
  findable) run in every pytest pass.
- **`retrieval-gate`** (CI, `ci.toml`) — an **arm-liveness probe only**
  (`retrieval_eval.py --probes-only --require-semantic`): it asserts the embedding
  arm actually loads, so a dead model can't silently degrade fused search to
  lexical while every row stays green. No metric run rides it.

**Nothing else gates on a quality number, and no local protocol is eligible to.**
That is the conclusion at the top of this page, made operational: a per-commit
metric over labels this archive produced would be an optimistic bound presented as
a score, and the alternative — labels from a record outside search — carries
queries nobody asked. The benchmarks are the quality claim, run deliberately.

`python -m search_lab benchmark` runs tier 4 as one recorded set and skips whatever
it has already measured at the current ranking code.

The instruments stack into a **quality ladder**, fastest tier first — climb until
the evidence matches the stakes. Tiers 0–3 detect damage; only tier 4 supports a
positive claim:

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` + `tests/test_search_recall_shape.py` + `tests/test_reality_mechanisms.py` (every pytest run) | checked-in synthetic corpus, lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding model | minutes | touching the model arm |
| 2 | CI arm-liveness probes (`retrieval_eval.py --probes-only`) | live archive | ~a minute | every commit, via thread-ci |
| 3 | `latency_replay.py` (speed over real traffic), `graph_eval.py`, `--behavior` | the live archive | minutes | evaluating a deliberate ranking change |
| 4 | `python -m search_lab benchmark`; `pytest -m beir` | external IR / conversational-memory benchmarks | tens of minutes | the quality claim — calibrating against published baselines |

The tunables all live in one object — `SearchParams` (`_retrieval/params.py`) — and
a candidate configuration is another instance of it, passed through
`search(params=...)` and scored against the incumbent on identical cases. The gold
gate drives that seam one knob at a time; `tests/test_search_params.py` keeps it
open. A direction that looks good on the synthetic corpus is only a direction — the
gold-file delta says "on provenance-grounded labels, it measures better," and only
the second can promote a change.
