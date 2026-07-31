# search_lab/ — the search lab

Everything that measures search lives here — the harnesses that *score* quality,
the scoring core they share, corpus freezing, and the run ledgers. Nothing in this
directory is part of the product: an install carries no measurement surface at
all, which is the boundary — the package preserves and retrieves, the lab measures
how well. Come here when you're *changing ranking*: this directory is the whole
scoring workbench, and the ladder below is the order to climb it.

Each script's docstring is its own full manual (protocols, biases, caveats); this
README is the map.

## What a number here is worth

**No measurement on this archive's own corpus certifies that search is good, and
nothing in this lab gates on one.** That is a standing conclusion, not a gap
waiting on more tooling.

Two independent failures put it there, and a protocol has to clear both:

1. **Circular labels.** A labeler that sweeps the corpus with the production
   ranker marks what that ranker already reaches. A thread the stack
   systematically cannot surface never enters the labels, so it can never be
   counted as missing, and every number scored that way is an optimistic upper
   bound on itself by an amount nothing inside the protocol can see. That applies
   whether the labeler is a judge grading a retrieved pool, an agent reformulating
   queries to build a topic pool, or a click log recording what an agent opened
   out of what search showed it.
2. **Queries nobody asked.** Escaping circularity means fixing the labels against
   a record outside search — a commit, an edit in the tool-use trail — but the
   *query* must then be authored from that same artifact. A query written to have
   a knowable answer is not shaped like one an agent types, and a number over
   queries nobody asked is not evidence about the searches anyone runs.

Underneath both is a hard constraint: **a real query and a complete answer set are
not recoverable from the same record.** Nobody ever enumerated the answers to
`watcher ingest lock`; the only trace is what search returned and what the agent
opened. No label-production scheme escapes that, which is why none runs here.

So the quality claims this lab does make live on **public benchmarks** — corpora
somebody else labeled, read beside the baseline their own leaderboard publishes
(tier 4 below, `python -m search_lab benchmark`). They answer "are the retrieval
components competitive in general?", never "did search get better on this
archive". The honest local instruments are speed (`latency_replay.py`, over the
searches agents actually ran) and the tier-0 synthetic floors, which detect damage
rather than credit improvement.

So nothing in this directory labels this archive. What runs against it produces
*no labels at all*: `--behavior` reports zero-label trail rates, `latency_replay.py`
measures speed, and the CI probe asserts the arms load. They are diagnostics, and
none is ever cited as evidence a change helped.

## The quality ladder

Fastest tier first — climb until the evidence matches the stakes.
(`docs/search-quality.md` tells the same story with the measured numbers.)

(`python -m search_lab benchmark` runs tier 4 for you, skipping what a run has
already measured at this configuration — see "Running the whole bench".)

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` + `tests/test_search_recall_shape.py` + `tests/test_reality_mechanisms.py` (in every pytest run) | checked-in synthetic corpus (`tests/quality_corpus.py`), lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding model | minutes | touching the model arm |
| 2 | CI arm-liveness probes (`retrieval_eval.py --probes-only`) | live archive | ~a minute (it loads both models) | every commit, on the maintainer's local CI |
| 3 | `latency_replay.py` (speed over real traffic), `--behavior` | the live archive | minutes | evaluating a deliberate ranking change |
| 4 | `python -m search_lab benchmark --quick`; `pytest -m beir` | seven external IR / conversational-memory benchmarks, sampled where a row is too large to score whole | under 20 min once the corpora are built | detecting damage on published labels — what a release is gated on |
| 4 | `python -m search_lab benchmark` | the same seven, every judged query | about an hour, most of it PerLTQA; about a day of CPU to build the corpora the first time | the quality claim — calibrating against published baselines |
| gate | `python -m search_lab gate --run --quick` | tier 4's recorded numbers vs the checked-in accepted ones | whatever the quick tier costs, plus milliseconds | cutting a release |

Tiers 0–3 **detect damage**; tier 4 is the only one that supports a positive
quality claim, and only about the components in general (see "What a number here
is worth").

The gate is not a tier — it is a *decision* over tier 4's output, and it is the
one thing here that can fail a release on ranking. See "The release gate" below.

The MRR/success/true-recall/nDCG loop is `eval_core.evaluate`, right here. Exactly
one caller scores through it — the tier-0 synthetic corpus, whose labels are nonce
terms true by construction. Shared modules sit beside the harnesses, all of
them lab-only for the same reason: `eval_core.py` (scoring), `eval_home.py` (which
home a benchmark builds into, which arms it pins, whether a cached build still
describes the corpus asked for), `snapshot.py` (freeze a corpus — also a command:
`python search_lab/snapshot.py <dir>`), `speed.py` (the latency measurement core), `run_meta.py` (the commit and
configuration every ledger stamps its rows with), `bench_runs.py` (the run
ledger), `dataset_pins.py` (what each corpus *is*, as a content hash — see "The
corpora are pinned"), `retrieval_report.py` (the latency series off the ledgers,
`python search_lab/retrieval_report.py`), and `inventory.py` (what is on this box
— which rows can run and which corpora are built; `python search_lab/inventory.py`,
and the viewer's `/lab` dev page). The harnesses reach them by bare sibling import
and the tests by `search_lab.*` — the dependency runs lab → package and never
leaves a checkout.

Two scoring cores, split at the corpus. Tier 0 runs through `eval_core.evaluate`;
the external benchmarks implement their own
published metric conventions instead (linear-gain nDCG where this archive uses
exponential), because the point of those runs is to land beside a leaderboard.
What every harness shares regardless is `eval_home`: the same refusal to build
anywhere near a real archive, the same arm pinning (so `lexical` names one stack
everywhere, coherence included), and the same warm-before-scoring rule.

## Running the whole bench

```
python -m search_lab benchmark --quick          # the quick tier — the release bar, under 20 min
python -m search_lab benchmark                  # the full tier — every query, about an hour
python -m search_lab benchmark --only locomo    # just the rows whose name matches
python -m search_lab benchmark --list           # the plan: what runs, what is fresh
```

**Two depths, and that is the whole taxonomy.** There is no third: a row is either
scored whole or scored at its declared `quick_sample`.

The **quick** tier (`--quick`) is what a release is gated on. It samples the three
arms too large to sit in a preflight — `cdr[vectors]` at 1,583 queries and
PerLTQA's two at 8,588 each — and scores every query on the other nine rows, all
seven datasets either way. The sizing rule is in `benchmark.py`: no row much past
`QUICK_ROW_BUDGET_MIN` minutes, the set inside `QUICK_SET_BUDGET_MIN`, sampling
only where a row does not already fit. A row that fits is scored whole and keeps
one continuous history across both depths.

The **full** tier scores every judged query on every row and samples nothing. It is
the depth a published number would have to come from, and it costs about an hour —
most of that PerLTQA — so it is run deliberately rather than routinely. Nothing
gates on it; a green quick gate is what "releasable" means here.

What makes the sampling trustworthy is the sampler
(`eval_core.sample_queries`): the draw is a hash of each query's own id, so it is
deterministic (two runs of identical code score identically, which the whole
freshness-and-delta machinery depends on), independent of file order, and
*nested* — widening a sample adds queries rather than swapping them.

It has to be a hash rather than a head-`n` cap because none of these query files
are in random order: MTRAG's are grouped by domain, PerLTQA's by person and then
by memory type, BEAM's by memory-ability category. Head-`n` samples one stratum
and reports it as the corpus, and it is not a subtle error — PerLTQA's profile
questions score MRR@10 0.06 under a head-200 cap and 0.28 under a sample of the
same size, because the cap took one person's entire profile block.

Two rules follow, and the runner prints both:

- **A sampled row is a different measurement**, not a cheaper look at the same
  one, so it records the sample size in its own name
  (`perltqa[lexical]~1200`) and never mixes with a measurement over a different
  query set. Rows cheap enough to score whole keep one name and one continuous
  series across both depths.
- **Resolution is 1/n.** At a sample of 100 a row cannot read a delta finer than
  0.01. That is enough to gate a release on — damage detection is what a gate is
  for — and not enough to publish a number from; re-run the full tier before
  claiming a change helped.

Sampling cuts query time and nothing else — a corpus that has never been built
pays the same ingest and embed either way.

`benchmark.py` drives the published-baseline yardsticks as one recorded set, each
row a separate process (the stack caches a corpus graph and a vector pack per
engine, so a corpus must never be swapped underneath them mid-process) and one at
a time (two rows at once measure each other's contention). Every row is a public
benchmark — there is no local-corpus row and no tier that credits a change on this
archive. The plan estimates each row from what it actually took last time, so the
printed budget is measured rather than guessed; a row whose corpus has never been
built pays for building it once.

Seven datasets, in **cheapest-first order**. That ordering is deliberate: a cold
pass is dominated by whichever corpora have to be built, and a set that ran those
first would print nothing for hours. Within a dataset the lexical row precedes the
vectors one, which is the same principle — the lexical row pays the ingest, and the
vectors row adds only the embed pass on top of a corpus already built. A cold pass
over everything is a few hours of CPU on this box; every pass after it is minutes,
because the corpora are cached and unchanged rows are fresh.

Two more datasets have working harnesses and downloaded data but are **held off
the default set on cost** — 537K documents and about 22 hours of embedding between
them, against roughly 4 for everything on it. `mtrag_eval.py` is the only external
read on **query shape** (one need as a terse last turn and as a standalone
rewrite, published baseline for each; `--domain govt` buys that comparison for a
seventh of the embed), and `beir_eval.py --dataset trec-covid` is the only set
with deep enough judgment pools to make a recall@100 mean anything. Run either by
hand.

The estimates a never-run row shows are priced off this box's measured
throughput — **ingest ~3,600 docs/min, embed ~450 docs/min** — rather than
guessed, because the decision `--list` informs is whether to start something that
runs overnight. Each is replaced by that row's own elapsed time once it has run.

**It is built for the tuning loop.** Each row records its numbers against a
content hash of the ranking code *as it sits in the working tree*, so a row whose
code and corpus are unchanged is **fresh**: skipped in milliseconds and reported
from the ledger. Uncommitted edits count — a tuning pass never commits, and a
commit-keyed cache would skip every row after the first edit and report pre-edit
numbers. Move a `SearchParams` default and the whole set goes stale; touch the
viewer and none of it does. Every row prints its delta against **the last run at a
different configuration**, not the previous run, so re-running an unchanged
configuration shows zero movement instead of hiding the comparison you wanted.

The ledger is `~/.local/state/thread-search-lab/bench-runs.jsonl` (`bench_runs.py`)
— run-level: row, corpus id, code id, commit, measures, elapsed. Per-query detail
stays in each harness's own `--json-out` report. It sits in the lab's own state
root for two reasons: nothing on this bench measures the archive, so the product's
home has no business holding its numbers; and the corpus cache is documented as
safe to delete, while a measurement history is the one part of a bench pass that
cannot be rebuilt. Nothing here *builds* a corpus: a row whose
corpus is missing fails and names the builder, because an ingest-plus-embed is a
decision about hours of CPU, not something a benchmark run should take on its own.

## The instruments

All run from the repo root with the repo venv, all read-only against the
archive (BEIR and the lab build throwaway homes and never touch it).

- **`retrieval_eval.py`** — the two instruments that run against the live archive,
  neither of which produces a label. `--probes-only --require-semantic` asserts the
  model arms load and exits — the CI row's mode, and the check that catches a dead
  embeddings model silently degrading the fused stack to lexical-only. `--behavior`
  reports zero-label trail rates per search (clicked / reformulated / abandoned);
  read the trend, never a single run. It also re-exports `eval_core.evaluate`,
  which is how the tier-0 corpus scores.
- **`latency_replay.py`** — the speed bench over the queries agents actually ran,
  and the one local instrument whose population is real. Any curated query set is
  selected for something, and that selection excludes most of what real traffic
  looks like — time-scoped asks, browse walks, sentence punctuation are all common
  in the ledger and rare in anything hand-built, so a change to any of them reads
  flat on a curated pass while moving real searches by an
  order of magnitude. Replays real *calls*, parameters included (a recorded browse
  ask replayed as bare text understates it 12×), and prints the ledger's **served**
  distribution beside the bench's own. Expect those to diverge — the bench is warm
  with the pool cache off, production is whatever the serving process happened to
  be — and read the gap as a fact about conditions, not about the code. Runs against
  the live archive, not a snapshot; `--baseline` sets the reference, and the
  timeseries is tagged `query_set=observed` so it never averages with rows from
  another population. Speed is deliberately not a CI gate: a gate would run
  against the live, growing archive, so its verdict is not repeatable — growth and
  a code slowdown look identical to it. The usage telemetry
  (`retrieval-usage.jsonl`, read by `retrieval_report.py`) is the standing warning
  system; this is the instrument you point at a deliberate change.
- **`beir_eval.py`** / **`cdr_eval.py`** / **`mtrag_eval.py`** /
  **`haystack_eval.py`** / **`perltqa_eval.py`** — the external
  yardsticks: the real pipeline over public benchmarks, beside their published
  baselines. They answer "are the components competitive in general?" — never
  archive-domain quality (third-party corpora that look nothing like an agent's
  own session log; read every number against that mismatch). Two shapes:
  - **shared-corpus** — `beir_eval.py` (BEIR `scifact` / `nfcorpus`),
    `cdr_eval.py` (NVIDIA ChatRAG's CDR) and `perltqa_eval.py` (personal-memory
    unit retrieval) retrieve from one corpus, scored by nDCG@10 against
    BM25 / dense / best-of-N references. `mtrag_eval.py` (IBM's multi-turn RAG
    benchmark over four document corpora) is the same shape, held off the default
    set on cost.
  - **per-question haystack** — `haystack_eval.py`
    (`--dataset locomo|longmemeval|beam`): each question carries its own small
    conversation history, and the task is to pull the evidence turn(s)/session(s)
    out of *it*. Builds a small archive per corpus — cached by content and reused
    across runs, so a re-run over an already-embedded corpus
    skips ingest+embed — scored by recall@k against the datasets' published recall
    baselines.

  Four of these rows exist to measure something no other row does, and reading
  them as interchangeable third-party numbers wastes them:

  - **document length.** `nfcorpus`'s short medical documents sit below
    `scifact`'s abstracts on purpose. The bm25/density term's strength scales
    inversely with document length, so two corpora at two lengths is what turns
    that from an inference across unrelated datasets into a measurement — and
    MTRAG's 512-token passages are the third point when it is run.
  - **completeness.** `beam` is the multi-answer row: median 2–3 gold messages
    per question, up to 96, where everything else is effectively single-gold. Its
    `recall_all@k` is the one number here that asks whether a window holds
    *everything* bearing on a question rather than merely something. Its three
    tiers are the same conversations at growing lengths, so they read as a
    degradation ladder.
  - **retrieval granularity.** `perltqa` retrieves a curated memory *unit* rather
    than a turn or a session.

  `beam` and `perltqa` carry no published retrieval baseline — BEAM's paper scores
  end-to-end QA under a memory framework, and PerLTQA's retrieval subtask is not
  reported in a form these runs reproduce — so both print that instead of a
  borrowed number.

  All three build under one root (`~/.cache/thread-evals`: `<root>/<dataset>` for
  a download, `<root>/homes/<name>` for a built corpus), so what the bench costs
  in disk is one `du`. A build is cached by everything that decides what went into
  it, and a `--max-docs` run gets its own home — a smoke corpus can neither read
  back as the full one nor overwrite it. Their metrics are the field's, not this
  archive's (see `eval_core.py` on the two scoring cores); their *configuration*
  is shared, so `lexical` here is the same pinned stack `lexical` names anywhere
  else on the bench.

## Taking a baseline (measure → change → measure)

"Take a baseline" before touching ranking means capturing numbers that stay
comparable after the change. The instruments are not interchangeable, and the
split is sharp: **everything runnable against this archive detects damage. Only
the public benchmarks support a positive claim, and only about the components in
general.**

**The one-command read.** `python -m search_lab benchmark` scores every
published-baseline row whose corpus is on this box and prints each beside the
number that dataset's leaderboard reports. A row unchanged since its last run is
skipped and reported from the ledger, so the second pass costs only what an edit
actually invalidated. That is the before-and-after pair a ranking change is judged
on.

## The corpora are pinned

Every comparison the bench makes assumes the corpus held still, and no upstream
here guarantees that: BEIR is a plain zip URL, three of the datasets are a `git
clone`, and two are Hugging Face files. So the guarantee is local, and it is a
content hash of the files each harness actually reads.

```
python -m search_lab pins            # what is on disk vs what is accepted
python -m search_lab pins --update   # accept what is on disk as the pin
```

The accepted hashes live in `dataset-pins.json`, checked in beside the baseline
and for the same reason: a number is only auditable next to a statement of what
it was measured on. **A content hash is a stronger pin than a revision** — a
revision only binds a re-fetch, while the hash binds the file however it got
there, including a force-pushed branch, a half-extracted zip, or a local edit.

It does two jobs with the one hash. Each harness calls `verify()` before it
builds, so a corpus that moved fails the run where it moved rather than being
scored (*prevention*). And it supplies `corpus_id` for the per-question haystacks
— `locomo`, `longmemeval` and `beam` build one home per question, so they have no
snapshot manifest to read, and the hash is the only corpus identity they have.
Without it the scored query count is their whole guard, and a dataset that changes
content at a constant count reads as a ranking movement (*detection*).

**The revision is what makes this reproducible rather than merely checkable.** A
hash alone can only say *no*: a fresh box that fetches different bytes learns that
it did, with no way to obtain the right ones. So each source also records the
upstream revision producing exactly these bytes, and the documented fetch commands
use it — `resolve/<sha>` rather than `resolve/main`, a `checkout` after the clone.
**A revision is written down only once fetching it has been confirmed to reproduce
the pinned hash** — a clean clone at that commit, a Hugging Face LFS `oid`, a
GitHub blob sha, none of which need the corpus downloaded again. An unconfirmed
revision is worse than none: it reads as provenance while sending the next person
to bytes nobody compared.

Two sources carry no revision, and each says why in its `upstream` string rather
than leaving a blank that reads as an oversight. The three BEIR sets are
**detection only** — BEIR publishes a zip at a fixed URL with no version in the
path and no digest beside it, so there is no handle to record, and a drift there
means the corpus is gone rather than re-fetchable. MTRAG's revision is not
established: the fetched layout does not correspond to the repository's own paths.

An unpinned dataset verifies as a no-op and is listed as unpinned: which corpora
a box has is a fact about the box, and accepting a pin is a verb somebody types.
Verifying all nine costs milliseconds — per-file digests are memoized on
`(size, mtime_ns)` in the lab's state root, so the ~1 GB is read once.

## The release gate

`benchmark` measures; `gate` decides whether what it measured is releasable.

```
python -m search_lab gate --run --quick     # the release gate: measure what is stale, then compare
python -m search_lab gate --quick --update  # accept the current numbers as the bar
python -m search_lab gate --allow-stale     # read the ledger mid-tuning, not a release check
```

**A release is gated on the quick tier**, and the flag travels: `--run` hands the
bench the same tier the comparison will read, because a gate that scored one depth
and compared the other would report every row as never measured. Both depths keep
their accepted numbers in the one baseline file under their own row names, and an
`--update` at one leaves the other's alone.

The two are split because they answer different questions. The bench prints each
row against **the last run at a different configuration** — the number a knob
turn is read on, and a moving reference by construction. A release needs the
other thing: a fixed set of accepted numbers that a change has to clear, which
moves only when somebody decides it should. Those live in
`search_lab/quality-baseline.json` — checked in, unlike everything else this lab
writes, because the ledger is per-box and a release cut from any checkout has to
be gated against the same bar. It is a record of a decision, not a measurement,
which is why nothing writes it automatically.

Four states fail, and the distinction between them is the point — each names a
different fix:

- **regressed** — a measure fell past its band. Fix, revert, or accept.
- **stale** — last measured at other code. Not knowing is indistinguishable from
  having regressed, and a green gate over an unmeasured ranking change is the
  failure the whole thing exists to prevent. `--run` is the fix.
- **missing** — never measured on this box, usually an unbuilt corpus.
- **corpus-changed** — scored a different query count or a different snapshot.
  Not a worse measurement of the same thing; a measurement of something else.

A manifest row with no accepted numbers is **ungated** — reported, never fatal.
Which corpora a box has built is a fact about the box, and a gate demanding every
row would be unrunnable anywhere but the machine that built them.

**The band is two cases wide, in the row's own units.** Scoring is deterministic,
so it is not there for noise — it is there because a row of `n` queries cannot
express a movement finer than `1/n`, and anything under that is rank shuffling
inside cases that already worked. Points are not comparable across rows: 0.01 is
three cases on scifact and twenty on LoCoMo. A row can override its band
explicitly (`"tolerance"` on its entry, row-wide or per-measure) where its corpus
has a documented weakness.

That band is deliberately tight enough that a real trade — a change that lifts
five rows and costs one — trips it. The gate's job is to make that a decision
somebody makes, with the movement visible in the diff, rather than one that
happens.

**What a green gate licenses** is exactly what tier 4 licenses and no more: the
retrieval components did not get worse on corpora somebody else labeled. It is
not a claim about this archive — see "What a number here is worth". The gate
raises the stakes of the measurement; it does not change what the measurement is.
And it licenses that at the quick tier's resolution: damage detection, which is
what a gate is for. A number worth publishing comes from the full tier.

The baseline's *integrity* is pinned in the fast suite (`tests/test_search_gate.py`,
every pytest pass): every baselined row is still a row the bench runs, every entry
carries its provenance and scored size, every gated measure is one its row
actually reports. An orphaned entry gates nothing while reading as though it does,
and a release is far too late to discover that. The live gate is not a pytest
lane: its subject is this box's recorded history, and the suite is sandboxed off
every machine location by design (`tests/meta/test_isolation.py`). It runs as a
command, the same shape ci.toml's `retrieval-gate` row already takes.

**Read the set, not a row.** Datasets disagreeing is the useful part: a
deficit that holds across every corpus is an arm problem, one that tracks document
length is the density-window scale mechanism, and one that shows up on a single
corpus is an artifact of that corpus. No single row can tell those apart, which is
why the set is worth more than the sum of its numbers and why a change that lifts
one corpus while sinking another must not be read off whichever row moved most.

```
python -m search_lab benchmark --list      # what would run, what is fresh
python -m search_lab benchmark             # take the numbers
```

**The speed axis — a quality knob can cost latency.** Most don't: the ranking
weights re-score a pool the arms already built, so a weight sweep is free on the
clock. The exceptions are the knobs that change what gets *fetched* or *scored* —
`pool_floor` above all, which sets how deep the arms reach before anything is
ranked. `latency_replay.py` measures it over the searches agents actually ran:

```
.venv/bin/python search_lab/latency_replay.py --reps 5 --baseline   # the reference
.venv/bin/python search_lab/latency_replay.py --reps 5              # after the change
```

- Warm steady-state, pool cache off — the FTS scan, the embed and the matvec are
  the cost under measurement, not something to skip. Cold model-load is excluded
  by a discarded warmup pass; it is a real cost with different knobs, and averaging
  it in swamps what a ranking change moves.
- The run prints the ledger's **served** distribution beside the bench's own.
  Expect them to diverge — the bench is warm and controlled, production is whatever
  the serving process happened to be — and read the gap as a fact about conditions,
  not about the code.
- `--baseline` writes the reference the next run diffs against; the timeseries
  lands in `<home>/latency-runs.jsonl` tagged `query_set=observed`, and a row from
  another population never averages into it.

Read a latency delta as a distribution: the tail (p95/p99) is the number that
bites a client timeout, and a few-ms move in p50 is noise. Stage attribution says
*which* knob to reach for — if `semantic_ms` is flat and `fts_ms` grew, the pool
knobs are the lever, not the vector arm.

**What the local instruments are for.**

- **Tier 0** is three shapes, all in every pytest run. The metric floors
  (`tests/test_search_quality.py`) are near-saturated by design (MRR ≈ 1.0 on the
  synthetic corpus) — they can only fall: a breakage detector, not an improvement
  meter. The recall shapes (`tests/test_search_recall_shape.py`) cover what the
  ordering metrics can't score — every thread carrying a term is enumerable, first
  and last mention are answerable — on nonce-term labels that are true by
  construction. The mechanism contracts (`tests/test_reality_mechanisms.py`) pin
  deterministic properties of the pipeline's machinery — content types are indexed
  at all, the MCP default scope widens to tool/thinking content, reindex preserves
  what was findable — not ranking preferences.
- **The trail is a sampling frame, not a labeler.** Every `thread_search` an agent
  ran and every `thread_read` that followed is in the archive's own tool-use trail,
  and it is tempting to score against those pairs. Don't: the thread an agent
  opened is a pick from what *that day's ranker* surfaced, so a change that
  surfaces different-better results scores as a loss and a high score means "ranks
  like the incumbent." What the trail is genuinely good for is the query
  *population* — `latency_replay.py` replays it, and `--behavior` reports its rates.

- **A cold process would score a different number than a warm one**, which is why
  every scoring path builds the corpus graph before its first case
  (`search_lab.eval_core.warm_for_scoring`, called from `evaluate` and from the
  instruments that search directly). The coherence re-rank reads a graph built in
  the background and no-ops until it lands, so under a scoring loop the build
  arrives partway through and splits a run in two — cases before it ranked without
  coherence, cases after it with, the boundary set by wall-clock. Two runs of
  identical code then disagree. If you write a new instrument that calls
  `api.search` in a loop rather than going through `evaluate`, call it yourself.

**Claim discipline.** Green tier 0 licenses exactly one claim, synthetically:
"search didn't break." A flat `latency_replay.py` licenses "it didn't get slower."
Neither is "search improved," and no instrument that runs against this archive can
license that one. What can is a benchmark delta — `python -m search_lab benchmark`
on both sides of the change, on rows whose corpus somebody else labeled — and even
that says the *components* improved, on corpora that look nothing like an agent's
session log. Say which of those you have. Without them, report the change as
unverified, not as an improvement.

**Read a delta in cases, not in points.** Scoring is deterministic — same code,
same corpus, same numbers to the digit — so a movement is never noise. But on `n`
cases a single case going from rank 1 to unfound moves any of these metrics by at
most `1/n`, which is the resolution the run actually has. Anything under `1/n` is a
rank shuffling within cases that already worked, not a win.

## Changing ranking, start to finish

1. State the change as a falsifiable hypothesis about one knob on
   `SearchParams` (`_retrieval/params.py`), and write down what would refute it.
2. Take the before: `python -m search_lab benchmark` for quality and
   `latency_replay.py --reps 5 --baseline` for speed. Both are cheap to re-take
   and expensive to reconstruct after the fact.
3. Make the change, then re-run both. The benchmark rows re-run automatically —
   editing a `SearchParams` default moves the code hash every row is keyed on, so
   nothing reports pre-edit numbers.
4. Promote once the benchmark delta holds and latency has not regressed. Fold the
   winner into `_retrieval/params.py` defaults with its evidence in the docstring,
   and let tier 0/2 ratchet the new shape. If the benchmarks are flat, you have no
   result — say so rather than reaching for a local number to fill the gap.
5. Move the bar: `python -m search_lab gate --update`. A promoted change that
   leaves the baseline where it was means the next change is measured against a
   number nobody stands behind any more, and a row that gave ground in the trade
   stays permanently one breach from red.

## Cost and hygiene

- Nothing on this bench spends tokens. Every instrument here is CPU and disk.
- Two things the bench touches are checked in, and both are records of a decision
  rather than measurements: the synthetic tier-0 corpus (no real data in it) and
  `quality-baseline.json`, the numbers a release is gated on. Nothing else the
  bench reads or writes belongs in the repo — the ledgers are per-box and the
  corpora are rebuildable.
- Built benchmark corpora live under one root, `~/.cache/thread-evals`:
  `<root>/<dataset>` for a download, `<root>/homes/<name>` for a built home. They
  are large (tens of GB with vectors) and entirely rebuildable, so that whole tree
  is safe to delete when disk gets tight.
- The fast tests guarding these harnesses live in `tests/`
  (`test_retrieval_eval.py`, `test_search_params.py`, `test_eval.py`,
  `test_beir_calibration.py`, `test_eval_home.py`, `test_bench_runner.py`,
  `test_search_gate.py`) and run in every pytest pass — the lab stays runnable
  even when nobody has tuned search in months.
