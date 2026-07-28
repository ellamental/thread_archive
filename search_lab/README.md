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

Instruments that produce *no labels* are a separate category: `--from-log` and
`--behavior` below are diagnostics, fenced as alarms, never cited as evidence a
change helped.

## The quality ladder

Fastest tier first — climb until the evidence matches the stakes.
(`docs/search-quality.md` tells the same story with the measured numbers.)

(`python -m search_lab benchmark` runs tier 4 for you, skipping what a run has
already measured at this configuration — see "Running the whole bench".)

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` + `tests/test_search_recall_shape.py` + `tests/test_reality_mechanisms.py` (in every pytest run) | checked-in synthetic corpus (`tests/quality_corpus.py`), lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding model | minutes | touching the model arm |
| 2 | CI arm-liveness probes (`retrieval_eval.py --probes-only`) | live archive | ~a minute (it loads both models) | every commit, via thread-ci |
| 3 | `latency_replay.py` (speed over real traffic), `graph_eval.py`, `--behavior` | the live archive | minutes | evaluating a deliberate ranking change |
| 4 | `python -m search_lab benchmark`; `pytest -m beir` | seven external IR / conversational-memory benchmarks | minutes once the corpora are built; about a day of CPU to build them all the first time | the quality claim — calibrating against published baselines |

Tiers 0–3 **detect damage**; tier 4 is the only one that supports a positive
quality claim, and only about the components in general (see "What a number here
is worth").

The scoring core these harnesses share — the case protocols and the
MRR/success/true-recall/nDCG loop — is `eval_core.py`, right here, so every
harness scores off one code path. Shared modules sit beside the harnesses, all of
them lab-only for the same reason: `eval_core.py` (scoring), `eval_home.py` (which
home a benchmark builds into, which arms it pins, whether a cached build still
describes the corpus asked for), `snapshot.py` (freeze a corpus — also a command:
`python search_lab/snapshot.py <dir>`), `gold_files.py` (what counts as a case
file), `speed.py` (the latency measurement core), `run_meta.py` (the commit and
configuration every ledger stamps its rows with), `bench_runs.py` (the run
ledger), `retrieval_report.py` (the latency series off the ledgers,
`python search_lab/retrieval_report.py`), and `inventory.py` (what is on this box
— which rows can run and which corpora are built; `python search_lab/inventory.py`,
and the viewer's `/lab` dev page). The harnesses reach them by bare sibling import
and the tests by `search_lab.*` — the dependency runs lab → package and never
leaves a checkout.

Two scoring cores, split at the corpus. Everything scoring *case files* runs
through `eval_core.evaluate`; the external benchmarks implement their own
published metric conventions instead (linear-gain nDCG where this archive uses
exponential), because the point of those runs is to land beside a leaderboard.
What every harness shares regardless is `eval_home`: the same refusal to build
anywhere near a real archive, the same arm pinning (so `lexical` names one stack
everywhere, coherence included), and the same warm-before-scoring rule.

## Running the whole bench

```
python -m search_lab benchmark                  # the full set — the release bar
python -m search_lab benchmark --quick          # the quick check — minutes
python -m search_lab benchmark --only locomo    # just the rows whose name matches
python -m search_lab benchmark --list           # the plan: what runs, what is fresh
```

**Two depths, and they are for different questions.**

The **full** set scores every judged query of every dataset. It is what a release
is cut against and the only depth a quality claim may cite.

The **quick** check (`--quick`) scores a deterministic sample on the rows heavy
enough to need one and every query on the rest — same seven datasets, minutes
instead of hours. What makes it trustworthy is the sampler
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
  one, so it records under its own name (`perltqa[lexical]~800`) and never mixes
  into the full run's history. Rows cheap enough to score whole keep one name and
  one continuous series across both depths.
- **Resolution is 1/n.** At a sample of 100 a row cannot read a delta finer than
  0.01. Use the quick check to catch damage; re-run the full set before claiming
  a change helped.

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

The ledger is `~/.thread/archive/bench-runs.jsonl` (`bench_runs.py`) — run-level:
row, corpus id, code id, commit, measures, elapsed. Per-query detail stays in each
harness's own `--json-out` report. Nothing here *builds* a corpus: a row whose
corpus is missing fails and names the builder, because an ingest-plus-embed is a
decision about hours of CPU, not something a benchmark run should take on its own.

## The instruments

All run from the repo root with the repo venv, all read-only against the
archive (BEIR and the lab build throwaway homes and never touch it).

- **`retrieval_eval.py`** — the hub. Scores search with MRR / success@k /
  true recall@k / nDCG@k under
  three case protocols: `--auto-titles` (zero-label proxy), `--from-log`
  (real search→read pairs mined from the archive's own tool-use trail —
  collapse alarm only), `--cases` (a snapshot-bound case file — the baseline
  instrument). Success asks whether any answer ranks;
  recall measures how much of the complete grade-2 set ranks; nDCG scores the
  ordering of the whole 2/1/0 pool. `--probes-only` skips the metric run for
  the CI row's arm-liveness checks. `graph_eval.py` scores the same log-drawn
  cases off `eval_core.mine_log_cases`.
- **`bm25_baseline.py`** — plain BM25 over a snapshot, the reference a case-file
  number is read against. A score in isolation means nothing; the question a
  baseline answers is whether the machinery above the lexical arm is earning its
  place on this corpus. On the SWE-chat cases it currently answers *no* — see
  `docs/search-quality.md`.
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
  another population.
- **`gold_stats.py`** — what is actually *in* a case file
  (`python search_lab/gold_stats.py <cases.jsonl>`, `--json`, `--corpus`). Every
  other instrument here measures search; this one measures the benchmark, which is
  the question nothing else asks. A file can be internally fine and measure almost
  nothing — every case single-gold, every graded pool one document, every query in
  a tier the same sentence with the subject swapped, every case from one repo — and
  none of that shows up in a score. Three panels: gold-set and graded-pool sizes,
  the query distribution (length, and opening-n-gram concentration per tier, which
  is the template detector), and coverage concentration. Run it on a file before
  citing a number off it.
- **`swechat_corpus.py`** — builds the SWE-chat corpus home and its linkage file.
  [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat) is public
  agent-session data (ODC-BY, arXiv:2604.20779) whose transcripts are native
  Claude Code JSONL, so the shipped importer ingests them unchanged. It is the
  corpus the bench runs on for two reasons. It **ships session ↔ commit
  provenance**, which is what a label has to be fixed by, and no other corpus here
  does. And it is an **independent hold-out**: domain-matched (agent session logs,
  not a mismatched third-party IR corpus) yet written by other people about other
  codebases, so nothing on it was authored by whoever tunes the ranker. The build
  also folds the code axis before it stamps the snapshot — a corpus home has no
  watcher and nothing else would, and an unfolded `event_paths` leaves `edited` with
  no path to qualify. The build
  takes every transcript it can read; one thing bounds it, the **transcript
  shape**. OpenCode and most Gemini CLI sessions are pretty-printed JSON, Cursor's
  are neither, and Codex's are line-delimited under a different envelope — all
  unreadable by the line-stream importer, and archive's OpenCode/Cursor importers
  are DB scanners with no JSON-export path. That costs about a sixth of the
  download and is the benchmark's coverage bound; `docs/search-quality.md` carries
  the built corpus's size and its scores.

  Its gold dir — the linkage file and any case files — lives in `gold/` beside the
  download rather than under `~/.thread/archive`, because nothing here quotes the
  operator's conversations. `swechat_bench.py` exports a case file as a standalone
  benchmark anyone can run.
- **`graph_eval.py`** — does the corpus-native embedding graph earn its
  ranking signal? A damage check for the shipped coherence re-rank. Scored on
  log-mined click labels, so it is an alarm like `--from-log`, never evidence the
  re-rank helps.
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
  borrowed number. `bm25_baseline.py` is what would give either a local reference.

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
split is sharper than it used to be: **everything runnable against this archive
detects damage. Only the public benchmarks support a positive claim, and only
about the components in general.**

**The one-command read.** `python -m search_lab benchmark` scores every
published-baseline row whose corpus is on this box and prints each beside the
number that dataset's leaderboard reports. A row unchanged since its last run is
skipped and reported from the ledger, so the second pass costs only what an edit
actually invalidated. That is the before-and-after pair a ranking change is judged
on.

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
- **From-log numbers are alarms, not baselines.** The `--from-log` protocol mines
  click labels from the live trail: the label is whatever thread the agent opened,
  which is a subset of what search surfaced *that day*. The labels are censored by
  the incumbent ranker, so a change that surfaces different-better results scores
  as a loss, and a high score mostly means "ranks like the ranker that took the
  clicks." Nothing runs it on a cadence — a per-commit click-MRR invites being read
  as a quality score. Run it by hand for one question only — *did something
  collapse* — and never cite a from-log delta as evidence a change helped. The
  trail's lasting value to this bench is as a **sampling frame**: real query shapes
  to draw a query population from, not a labeler.
- **Case files are for hand-read experiments.** `retrieval_eval.py --cases` scores
  a graded case file over the snapshot it names. Read the per-case detail when you
  are investigating a specific suspicion — *does the intent stratum collapse under
  this weight?* — and stop there. It does not roll up into a headline number,
  nothing floors it, and no change is credited by it (see "What a number here is
  worth"). A file whose `snapshot_id` matches no snapshot on hand is stale; don't
  score it against a moved corpus.

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
4. Promote once the benchmark delta holds and latency has not regressed, with
   `--from-log` read as an alarm only. Fold the winner into
   `_retrieval/params.py` defaults with its evidence in the docstring, and let
   tier 0/2 ratchet the new shape. If the benchmarks are flat and only a case
   file moved, you have a lead worth investigating — not a result.

## Cost and hygiene

- Nothing on this bench spends tokens. Every instrument here is CPU and disk.
- Everything a run writes lands in the gold dir it worked in, so a corpus is one
  directory and no ledger describes cases that live somewhere else. A case file
  that quotes an operator's real conversations belongs under `~/.thread/archive`
  and never in the repo; the SWE-chat corpus's cases and ledgers sit beside that
  public download. The synthetic corpus is the one exception: no real data, so
  it's checked in.
- Built benchmark corpora live under one root, `~/.cache/thread-evals`:
  `<root>/<dataset>` for a download, `<root>/homes/<name>` for a built home. They
  are large (tens of GB with vectors) and entirely rebuildable, so that whole tree
  is safe to delete when disk gets tight.
- The fast tests guarding these harnesses live in `tests/`
  (`test_retrieval_eval.py`, `test_search_params.py`, `test_graph_eval.py`,
  `test_gold_stats.py`, `test_beir_calibration.py`, `test_eval_home.py`,
  `test_bench_runner.py`)
  and run in every pytest pass — the lab stays runnable even when nobody has tuned
  search in months.
