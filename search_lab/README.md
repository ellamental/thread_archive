# search_lab/ — the search lab

Everything that measures search lives here — the harnesses that *score* quality,
the miners that *mint* the graded gold cases they score against (`mine/`,
`python -m search_lab.mine`), the scoring core both share, corpus freezing, and
the run ledgers. Nothing in this directory is part of the product: an install
carries no measurement surface at all, which is the boundary — the package
preserves and retrieves, the lab measures how well.
Day to day none of this runs by hand — tier 0 rides every pytest pass and the CI
retrieval gate rides every commit. Come here when you're *changing ranking*: this
directory is the whole scoring workbench, and the ladder below is the order to
climb it.

Each script's docstring — and each miner's module docstring — is its own full
manual (protocols, biases, caveats); this README is the map.

The scoring core these scripts share — the case protocols (title sampling, log
mining) and the MRR/success/true-recall/nDCG loop — is `eval_core.py`, right here,
so every harness scores off one code path. Nothing in this directory ships: an
install carries no scoring surface, because a metric read without its protocol's
limits beside it misleads, and those limits are what this manual is. What the
product reports instead is whether search is *degraded*, which is actionable
(`thread_archive status`, the viewer's health page).

Five shared modules sit beside the harnesses, all of them lab-only for the same
reason: `eval_core.py` (scoring), `eval_home.py` (which home a benchmark builds
into, which arms it pins, whether a cached build still describes the corpus asked
for), `snapshot.py` (freeze a corpus — also a command:
`python search_lab/snapshot.py <dir>`), `gold_runs.py` + `mine_runs.py` (the run
ledgers), and `retrieval_report.py` (latency and quality series off the ledgers,
`python search_lab/retrieval_report.py`). The `_mine` miners in the package reach
these by importing `search_lab.*` — they are repo-only too (the wheel excludes
them), so the dependency never leaves a checkout.

Two scoring cores, split at the corpus. Everything scoring *gold cases* runs
through `eval_core.evaluate`; the external benchmarks implement their own
published metric conventions instead (linear-gain nDCG where this archive uses
exponential), because the point of those runs is to land beside a leaderboard.
What every harness shares regardless is `eval_home`: the same refusal to build
anywhere near a real archive, the same arm pinning (so `lexical` names one stack
everywhere, coherence included), and the same warm-before-scoring rule.

## The quality ladder

Fastest tier first — climb until the evidence matches the stakes.
(`docs/search-quality.md` tells the same story with the measured numbers.)

(`python -m search_lab benchmark` climbs tiers 3 and 4 for you, skipping what a
run has already measured at this configuration — see "Running the whole bench".)

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` (in every pytest run) | checked-in synthetic corpus (`tests/quality_corpus.py`), lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding + rerank models | minutes | touching the model arms |
| 2 | CI `retrieval-gate` (arm-liveness probes) | live archive | seconds | every commit, via thread-ci |
| 3 | `retrieval_gold_gate.py` (grounded regression floors), `retrieval_eval.py` by hand, `graph_eval.py`, `--behavior` | live archive + the golds' frozen snapshot | minutes | evaluating a deliberate ranking change |
| 3½ | `retrieval_eval.py --cases` on agent-mined golds (`python -m search_lab.mine <miner>` to mint them) | a frozen corpus snapshot, corpus-grounded labels | seconds to score; agent-minutes per mined case | scoring against grounded labels; mining is an occasional cadence |
| 4 | `pytest -m beir`; `cdr_eval.py`, `haystack_eval.py --dataset …` by hand | external IR / conversational-memory benchmarks | tens of minutes (built homes cache for re-runs) | calibrating against published baselines |

## Running the whole bench

```
python -m search_lab benchmark              # the standard set
python -m search_lab benchmark --tier smoke # the two gold gates alone
python -m search_lab benchmark --list       # the plan: what runs, what is fresh
```

`benchmark.py` drives every instrument below as one recorded set, each row a
separate process (the stack caches a corpus graph and a vector pack per engine,
so a corpus must never be swapped underneath them mid-process) and one at a time
(two rows at once measure each other's contention). Three nested tiers, warm:
**smoke** is the two gold gates — the only instruments that can credit a change —
at ~7 min; **standard** adds every external yardstick whose corpus is already
built, ~20 min; **full** adds the cross-encoder passes, hours, measuring an arm
production ships with off. The plan estimates each row from what it actually took
last time, so the printed budget is measured rather than guessed; a row whose
corpus has never been built pays for building it once.

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
row, corpus id, code id, commit, measures, elapsed. Per-file detail stays in each
corpus's own `gold-runs.jsonl`. Nothing here *builds* a corpus: a row whose corpus
is missing fails and names the builder, because an ingest-plus-embed is a decision
about hours of CPU, not something a benchmark run should take on its own.

## The instruments

All run from the repo root with the repo venv, all read-only against the
archive (BEIR and the lab build throwaway homes and never touch it).

- **`retrieval_eval.py`** — the hub. Scores search with MRR / success@k /
  true recall@k / nDCG@k under
  three case protocols: `--auto-titles` (zero-label proxy), `--from-log`
  (real search→read pairs mined from the archive's own tool-use trail —
  collapse alarm only), `--cases` (a snapshot-bound case file, e.g. mined
  golds — the baseline instrument). Success asks whether any answer ranks;
  recall measures how much of the complete grade-2 set ranks; nDCG scores the
  ordering of the whole 2/1/0 pool. `--probes-only` skips the metric run for
  the CI gate's arm-liveness checks. Every other live-archive instrument
  reuses its miner (`mine_log_cases`).
- **`window_fill.py`** — the product measure: how much of what is relevant comes
  back in the window an agent reads, rather than where the first hit lands. Scores
  **window fill** (`hits@k / min(k, |gold|)` — ceiling-normalized, so it measures
  ranking rather than gold-set size) and **union coverage** (fire every query a
  topic file carries, union the windows, dedupe, and measure the share of the whole
  subject assembled — the fan-out workflow end to end), both against
  `bm25_baseline`'s plain BM25 over the same snapshot. Multi-answer `topic` files
  are the protocol with the resolution to measure it; single-gold files reduce it
  to success@k. It warms the models and corpus graph first, which the other
  instruments do not — see the caution below.
- **`latency_replay.py`** — the speed bench over the queries agents actually ran.
  Everything else here scores *curated* cases; this replays the usage ledger, which
  is a different population and the only one that answers "did this help **us**". A
  gold case is mined to be gradeable, and that selection excludes most of what real
  traffic looks like — time-scoped asks, browse walks, sentence punctuation are all
  common in the ledger and near-absent from the golds, so a change to any of them
  reads flat on `retrieval_gold_gate.py --latency` while moving real searches by an
  order of magnitude. Replays real *calls*, parameters included (a recorded browse
  ask replayed as bare text understates it 12×), and prints the ledger's **served**
  distribution beside the bench's own. Expect those to diverge — the bench is warm
  with the pool cache off, production is whatever the serving process happened to
  be — and read the gap as a fact about conditions, not about the code. Runs against
  the live archive, not a snapshot; `--baseline` sets the reference, and the
  timeseries is tagged `query_set=observed` so it never averages with the gold rows.
- **`python -m search_lab.mine`** — the gold miners (`mine/`), the only
  tokens-spending tier. Each mints snapshot-bound eval `--cases` files
  under `~/.thread/archive/`; bare `mine` lists them, `mine <miner> --help`
  documents one, and `mine all [N]` sweeps the
  ones a count alone can drive. All bind by `snapshot_id` to the frozen corpus
  snapshot they run against (`python search_lab/snapshot.py <dir>`; point `THREAD_ARCHIVE_HOME`
  at it), so after the one-time spend `retrieval_eval.py --cases` scores them for
  free and refuses them once the snapshot's id no longer matches. Five miners,
  covering complementary failure modes:
  - **`query`** — one `claude` agent per real trail query reads the originating
    session for intent, sweeps the snapshot deep and wide with its own
    reformulated searches, and writes a graded, corpus-grounded case
    (`judged-cases.jsonl`). Deep enough to credit a thread the incumbent buries —
    the recall signal a pool-bounded judge can't see. Precision + recall.
  - **`topic`** — mints golds from a **topic** dense with confounds. A survey
    agent decides how many *angles* the topic warrants and authors one query
    each with candidate threads; one labeler agent per angle verifies, expands,
    and grades a comprehensive pool (2=intended, 1=partial, 0=confound),
    `topic-cases-<token>.jsonl`. Confound ranking. Batch (needs `--topic`).
  - **`rerank`** — the cheap rung: production search returns a deep pool per
    query, one judge grades it 2/1/0 in a single pass (`rerank-cases.jsonl`).
    Scores ordering *within what search retrieved* (nDCG is the sharp signal);
    blind to recall by construction, but the judge's "nothing in the pool
    answers" verdict surfaces as a measured `none-of-pool` rate. Precision.
  - **`querygen`** — the mirror of `rerank`: sample a random thread, one agent
    reads it and authors difficulty-laddered queries (verbatim / paraphrase /
    vague) to find it; each becomes a findability case whose single gold is that
    thread (`findability-cases.jsonl`). Recall / findability, corpus-representative.
  - **`commit`** — the non-circular rung. Every miner above establishes its labels
    by searching with the engine under test, which bounds what any of them can
    measure: a systematic retrieval blind spot is invisible to labeler and ranker
    alike, so it can never score as a miss. Here the gold comes from
    **provenance** — a *linkage file* pairs each session with the commits it
    demonstrably authored, one agent reads only the commit (message + diff) and
    authors queries for it, and the linked session is the answer
    (`commit-cases.jsonl`). No search runs during labeling, and since the agent
    never reads the target thread there is no vocabulary leakage either. Sibling
    sessions in the same repo grade themselves structurally (overlapping files 1,
    disjoint 0), so the confound pool costs no tokens. Needs a corpus that ships
    session↔commit provenance — `search_lab/swechat_corpus.py` builds one from SWE-chat
    — so it is `○ direct`, never in `mine all`. Grades 1/0 are structural proxies;
    only the 2 is grounded.
- **`swechat_corpus.py`** — builds the SWE-chat corpus home and its linkage file.
  [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat) is public
  agent-session data (ODC-BY, arXiv:2604.20779) whose transcripts are native
  Claude Code JSONL, so the shipped importer ingests them unchanged. Its point is
  the one thing a gold file mined from this archive can never be: an **independent
  hold-out**. Every other gold file is mined from one corpus by one author, so
  hold-out discipline *within* it cannot see overfitting *to* it — and unlike the
  external yardsticks below, SWE-chat is domain-matched (it is agent session logs,
  not a mismatched third-party IR corpus). The built corpus is **Claude Code
  only** (5144 of 5850 transcripts): the other harnesses SWE-chat collects ship
  shapes the line-stream importer can't read, and archive's OpenCode/Cursor
  importers are DB scanners with no JSON-export path.

  It is a gold corpus like the archive's own, and carries the same furniture:
  case files, a floor sidecar per file, and the run ledgers — all of them in
  `gold/` beside the download rather than in the private gold dir, because
  nothing here quotes the operator's conversations. Scoring it is the gate's
  second invocation (`--snap <corpus home> --gold-dir <that gold dir>`); the
  numbers are not comparable to the archive's, and are not meant to be — its
  value is the *direction* a ranking change moves on a corpus nobody tuned
  against. `swechat_bench.py` exports the same golds as a standalone benchmark
  anyone can run.
- **`graph_eval.py`** — does the corpus-native embedding graph earn its
  ranking signal? Regression check for the shipped coherence re-rank, and the
  gate any new graph lever must pass.
- **`beir_eval.py`** / **`cdr_eval.py`** / **`haystack_eval.py`** — the external
  yardsticks: the real pipeline over public benchmarks, beside their published
  baselines. They answer "are the components competitive in general?" — never
  archive-domain quality (third-party corpora that look nothing like an agent's
  own session log; read every number against that mismatch). Two shapes:
  - **shared-corpus** — `beir_eval.py` (BEIR scifact, scientific-claim IR) and
    `cdr_eval.py` (NVIDIA ChatRAG's CDR, conversational retrieval) retrieve from
    one corpus, scored by nDCG@10 against BM25 / dense / best-of-N references.
  - **per-question haystack** — `haystack_eval.py`
    (`--dataset locomo|longmemeval`): each question carries its own small
    conversation history, and the task is to pull the evidence turn(s)/session(s)
    out of *it*. Builds a small archive per corpus — cached by content and reused
    across runs, so a re-run (or the rerank pass over an already-embedded corpus)
    skips ingest+embed — scored by recall@k against the datasets' published recall
    baselines.

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
comparable after the change. The instruments are not interchangeable: **only
the minted gold files can credit an improvement.** Everything else on the
bench detects damage.

**Start here — the one-command read.** `python scripts/retrieval_gold_gate.py`
discovers every gold file in one gold dir, scores it over that corpus's snapshot
with the production ranker (at the canonical `limit=20`), and prints per-file MRR
/ success@10 / recall@10 / nDCG@10. It is the regression gate, but the measured
numbers print on every run — floored files and freshly-mined ungated ones alike —
so it doubles as the fastest, most consistent read of where the baseline sits
right now, with no loop or aggregator to hand-roll (and no `limit` skew from
doing so). Drop to the per-file `retrieval_eval.py --cases` instrument below only
when you need the fuller metric set (success@1/5/20, recall@20, the natural-vs-code
per-shape split).

**One corpus per run**, so a full read is two: the archive's own, then the
SWE-chat hold-out.

```
python scripts/retrieval_gold_gate.py
python scripts/retrieval_gold_gate.py \
    --snap ~/.cache/thread-evals/homes/swe-chat --gold-dir ~/dev/swe-chat-data/gold
```

Each writes its ledger and per-case baseline into the gold dir it scored, and a
gold file bound to another snapshot skips rather than scoring — the two corpora
can't be averaged into a number describing neither. Separate processes are
deliberate: the stack caches a corpus graph and a vector pack per engine, and
swapping homes underneath those inside one process is how a corpus gets scored
against another's cached structures.

**A newly mined file rides ungated until it has a floor**, and `--calibrate`
writes one: after a clean full run of the shipped configuration it drops an
`X.floor.json` beside every scored file at `1/n` under what it measured — the
calibration rule made executable, the same rule the tables above quote. It never
lowers an existing floor, so running it after a regression cannot write the
regression in as the new expectation.

**Iterating on a ranking knob — the gate is the loop.** The gate also scores a
*candidate* configuration, which makes it the fastest way to find out whether an
idea is dead:

```
python scripts/retrieval_gold_gate.py --cache --fail-early --set fusion_weight=500
```

- `--set field=value` (repeatable) swaps a `SearchParams` field for the shipped
  one. The run is flagged `overrides` in the ledger and never writes the per-case
  baseline, so an experiment can't be mistaken for the baseline moving.
- `--cache` persists the candidate *pools* between processes. The arms and the
  fusion don't read ranking weights, so a second run at new weights re-scores
  pools it already has — measured over the full gold set, 132 s → 19 s, with the
  scores identical to the digit. The cache keys on the pool-shaping params
  (`rrf_k`, and `pool_floor` folded into the resolved depth), so sweeping *those*
  correctly misses rather than silently reading back the first value's pool.
  Sized for the real corpus: ~140 MB under `~/.thread/archive/gold-pool-cache/`,
  namespaced by `snapshot_id` and safe to delete.
- `--fail-early` stops as soon as a floor is provably out of reach — every
  unscored case counted as perfect still lands under it. Exact: it can only cut
  short a run that was going to fail. `--max-regressions N` adds the impatient
  companion, aborting once N cases that used to rank stop ranking at all.
- `--only <fragment>` narrows to some gold files — the tune/hold-out split, run
  by hand.

A candidate that survives this still owes the full-bench confirm: drop `--cache`
and `--fail-early` for the run that credits it, and read it under the hold-out
discipline below. Fast iteration is for killing bad ideas, not for promoting good
ones. The gate is the instrument for one knob at a time, against the floors that
actually gate CI.

**The speed axis — the same knob costs latency.** The dominant quality lever (the
cross-encoder re-rank) is also the dominant latency, so a quality change is
usually a latency change; `--latency [REPS]` measures both in one run:

```
python scripts/retrieval_gold_gate.py --set rerank_auto=true --set rerank_pool=6 --latency 3 --fail-early
```

- `--latency [REPS]` measures warm latency (default 3 timed reps/query, a warmup
  discarded) over the same queries the quality pass scores, and prints
  p50/p95/p99 by stage and by query shape with a delta against the recorded
  latency baseline. The **pool cache is forced off** here — the FTS scan, the
  embed, and the matvec are the cost being measured, not skipped — so `--latency`
  and `--cache` describe different runs (cache the quality pass, never the latency
  one). Cold model-load is excluded (a warmup pass hides it); this is warm
  steady-state, the regime a ranking knob moves. The full pass is ~10 min (one
  cache-off search per query per rep), so it's the *confirm*, not the loop.
- `--latency-smoke` is the loop: **only** the pathological-query smoke test
  (below), skipping the full pass — a ~1-minute speed check (mostly one-time model
  load; the measurement is seconds). This is the interactive-iteration lever, the
  speed counterpart of `--cache` for quality. The quick loop is
  `--cache --latency-smoke --set field=value`: cached quality (~20 s) plus the
  smoke, comfortably inside a warm window.
- With `--fail-early`, a **latency smoke test** runs first: the `--smoke-queries`
  (default 8) queries that were *slowest at baseline* — empirically the corpus's
  pathological cases — against a p95 ceiling (`--budget-ms`, else 1.5× the
  baseline p95). A change that uniformly slows the pipeline or worsens a heavy
  path fails here in tens of seconds instead of after the full pass. Impatient and
  not sound (a change can turn a baseline-fast query into the new slow one), so it
  is a tuning shortcut, like `--max-regressions` on the quality side.
- `thread_archive._ops.speed` records a `latency-runs.jsonl` timeseries and a
  `latency-baseline.json` (the smoke test's cherry-pick source), written on a
  clean full shipped run exactly as the quality baseline is.

Read a latency delta the way you read a quality one: warm latency is a
distribution, so the tail (p95/p99) is the number that bites a client timeout, and
a few-ms move in p50 is noise. Stage attribution tells you *which* knob to reach
for — if `rerank_ms` is flat and `fts_ms` grew, the pool knobs are the lever, not
the re-rank budget.

- **Minted gold case files ARE the baseline.** The gate enumerates and scores
  them for the current-state read; for a challenger delta, score **every file
  present, in both gold dirs, on both sides of the change** (a file minted by a
  parallel instance an hour ago is part of the baseline too). The `thread_archive
  mine` miners produce them, each yielding graded pools (nDCG, via `grades`):
  - `mine query` (`judged-cases.jsonl`): one `claude` agent per real query reads
    the originating session for intent, sweeps the frozen snapshot with its own
    reformulated searches, reads candidates, and writes a graded, corpus-grounded
    case. The recall-capable rung.
  - `mine topic` (`topic-cases-<token>.jsonl`): a survey agent searches a topic,
    decides the angles it warrants, and authors one query per angle with the
    candidates it found; one labeler agent per angle builds on those and grades a
    pool over the snapshot. The confound-ranking rung.
  - `mine rerank` (`rerank-cases.jsonl`): one judge grades a retrieved pool per
    query in a single pass — cheap, measures ordering within what search
    retrieved, and reports a `none-of-pool` rate as its recall-failure signal.
  - `mine querygen` (`findability-cases.jsonl`): generate difficulty-laddered
    queries for a random thread and test it ranks — corpus-representative
    findability, the recall counterpart to `rerank`'s precision.
  - `mine commit` (`commit-cases.jsonl`): author queries from a commit and test
    that the session which produced it ranks. The only rung whose labels are not
    established by searching with the engine under test, and the only one whose
    confounds are structural rather than judged.

  Each case is bound by `snapshot_id` to the corpus snapshot it was mined
  against (`python search_lab/snapshot.py <dir>`; point `THREAD_ARCHIVE_HOME` at it), and
  `retrieval_eval.py --cases` scores it over that snapshot — the freezing rule
  made mechanical. A file whose `snapshot_id` matches no snapshot on hand is
  stale: re-mine it, don't score it against a moved corpus.
- **Tier 0** is two shapes, both in every pytest run. The metric floors
  (`tests/test_search_quality.py`) are near-saturated by
  design (MRR ≈ 1.0 on the synthetic corpus) — they can only fall: a
  breakage detector, not an improvement meter. The mechanism contracts
  (`tests/test_reality_mechanisms.py`) pin deterministic properties of the
  pipeline's machinery — content types are indexed at all, the MCP default
  scope widens to tool/thinking content, reindex preserves what was findable,
  the cross-encoder's gate/window/boundary code paths behave — not ranking
  preferences; ranking quality belongs to the gold files.
- **From-log numbers are alarms, not baselines.** The `--from-log` protocol
  mines click labels from the live trail: the gold is whatever thread the
  agent opened, which is a subset of what search surfaced *that day*. The
  labels are censored by the incumbent ranker — a change that surfaces
  different-better results scores as a loss, and a high score mostly means
  "ranks like the ranker that took the clicks." Nothing runs it on a cadence
  (the CI gate is arm-probes only, precisely because a per-commit click-MRR
  invites being read as a quality score); if you run it by hand, read it for
  one question only — *did something collapse* — and never cite a from-log
  delta as evidence a change helped. The trail's lasting value to this bench
  is as a **sampling frame**: real query shapes to seed the gold miner with,
  not a labeler.

- **A cold process would score a different number than a warm one**, which is why
  every scoring path builds the corpus graph before its first case
  (`thread_archive._eval.warm_for_scoring`, called from `evaluate` and from the
  instruments that search directly). The coherence re-rank reads a graph built in
  the background and no-ops until it lands, so under a scoring loop the build
  arrives partway through and splits a run in two — cases before it ranked without
  coherence, cases after it with, the boundary set by wall-clock. Left alone that
  is worth ~0.02 window fill on a file, concentrated in whatever ran first, and two
  runs of identical code disagree. If you write a new instrument that calls
  `api.search` in a loop rather than going through `evaluate`, call it yourself.

**Claim discipline.** Green tier 0 licenses exactly one claim, synthetically:
"search didn't break." Running `scripts/retrieval_gold_gate.py` on a ranking
change makes that "didn't break" grounded rather than synthetic — it scores the
gold files over their frozen snapshot and fails on a drop below a calibrated floor
— but it is a **one-way floor, not a displayed score**: it holding means the
ranking didn't regress past the baseline, never that it improved. It stays a
deliberate floor rather than a per-commit number for the same reason the
click-label protocols never run automatically: they are censored by the incumbent,
so a per-commit click-MRR invites being misread as quality; scored as a one-way
floor on a deliberate change, the grounded golds answer only "did it regress." The claim "search improved" still
requires a gold-file delta scored on both sides of the change — every minted
file, in both gold dirs. Without those runs, report the change as
unverified — not as an improvement.

**Hold-out discipline.** A gold file tuned against repeatedly stops being a
measurement and becomes a training set. Two layers hold that line. Within a
corpus: keep at least two independently mined files and tune against one while the
other stays untouched until the confirming run (`--only` narrows the gate to the
tune side); re-mine on a cadence when a file's snapshot goes stale. Across
corpora: the SWE-chat golds are the hold-out proper — a different corpus, a
different author, a domain-matched task — and nothing is ever tuned against them.
A gain that shows up on the archive's golds and not there is a gold-file artifact
until something else explains it.

**Read a delta in cases, not in points.** Scoring is deterministic — same code,
same snapshot, same numbers to the digit — so a movement is never noise. But on a
file of `n` cases a single case going from rank 1 to unfound moves any of these
metrics by at most `1/n`, which is the resolution the file actually has. A `+0.01`
on the 64-case findability file is two thirds of one case; on a 7-case topic file
it is a fourteenth of one. Anything under `1/n` is a rank shuffling within cases
that already worked, not a win — and it will not survive a hold-out.

## Changing ranking, start to finish

1. State the change as a falsifiable hypothesis about one knob on
   `SearchParams` (`_retrieval/params.py`), and write down what would refute it.
2. `retrieval_gold_gate.py` — the tuning loop drives that knob through
   `search(params=...)` over the minted gold files (each scored on its own
   snapshot, on the graded pools the gate floors) and reports the delta against
   the incumbent. While iterating, `--cache` and `--fail-early` cut a bad idea
   short; `--only <fragment>` narrows to the tune half of the split. Tune against
   one file; confirm against the held-out one. (`retrieval_eval.py --cases`
   scores a *single* config over one file — reach for it to read a shipped
   config's absolute numbers, not to measure a change.)
3. Promote once the gold-file delta holds on the full-bench confirm — `--cache`
   and `--fail-early` dropped — and `--from-log`, read as an alarm only, hasn't
   collapsed: fold the winner into `_retrieval/params.py` defaults with its
   evidence in the docstring, and let tier 0/2 ratchet the new shape.

## Cost and hygiene

- The `mine/` miners spend real tokens (headless `claude` calls;
  each miner's `--target` bounds them). Everything else on the bench is free.
- Mined output quotes real usage — case files, detail sidecars, floor sidecars and
  ledgers live under `~/.thread/archive/` (`retrieval-trend.jsonl`,
  `judged-cases.jsonl`, `topic-cases-<token>.jsonl`, `rerank-cases.jsonl`,
  `findability-cases.jsonl`), never in the repo. Everything a run writes lands in
  the gold dir it worked in, so the SWE-chat corpus's cases, floors and ledgers
  stay beside that download — a corpus is one directory, and no ledger describes
  cases that live somewhere else. The synthetic corpus is the one exception: no
  real data, so it's checked in.
- Built benchmark corpora live under one root, `~/.cache/thread-evals`:
  `<root>/<dataset>` for a download, `<root>/homes/<name>` for a built home. They
  are large (tens of GB with vectors) and entirely rebuildable, so that whole tree
  is safe to delete when disk gets tight.
- The fast tests guarding these harnesses live in `tests/`
  (`test_retrieval_eval.py`, `test_search_params.py`, `test_mine_framework.py`,
  `test_retrieval_mine_gold.py`, `test_topic_mine_gold.py`, `test_graph_eval.py`,
  `test_beir_calibration.py`, `test_eval_home.py`, `test_retrieval_gold_gate.py`)
  and run in every pytest pass — the lab stays runnable even when nobody has tuned
  search in months.
- The gold gate (`scripts/retrieval_gold_gate.py`) scores a corpus's gold files
  over its snapshot as a deliberate regression floor, run on a ranking change. It
  runs where the corpus and the snapshot live; on a box
  without the snapshot, or while a gold file is mid-re-mine, the affected file is
  skipped, not failed. Floors sit at most one case (`1/n`) under measured —
  `--calibrate` writes them at exactly that, and never lowers one; raise a floor
  by re-calibrating after a shipped change lifts a number and holds.
