# evals/ — the search lab

Everything that *scores* search quality lives here: the harness scripts, the
experiment configurations, and this manual. The tools that *mint* the graded
gold cases those scorers run against — the agent miners — moved into the product
as `thread_archive mine` (package `thread_archive._mine`), so they ship and run
from an install, not only a dev checkout; `thread_archive mine` alone lists them.
Day to day none of this runs by hand — tier 0 rides every pytest pass and the CI
retrieval gate rides every commit. Come here when you're *changing ranking*: this
directory is the whole scoring workbench, and the ladder below is the order to
climb it.

Each script's docstring — and each miner's module docstring — is its own full
manual (protocols, biases, caveats); this README is the map.

The scoring core these scripts share — the case protocols (title sampling, log
mining) and the MRR/success/true-recall/nDCG loop — lives in the package at
`thread_archive._eval`, so the shipped `thread_archive eval` command (the operator's
read-only self-checkup over their own archive) and this dev bench score off one
code path. The bench is the *rest* of the ladder: the CI gate, the experiment
runner, and the gold-mining tiers that answer "should we change ranking," none
of which ship.

## The quality ladder

Fastest tier first — climb until the evidence matches the stakes.
(`docs/search-quality.md` tells the same story with the measured numbers.)

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` (in every pytest run) | checked-in synthetic corpus (`tests/quality_corpus.py`), lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding + rerank models | minutes | touching the model arms |
| 2 | CI `retrieval-gate` (arm-liveness probes) | live archive | seconds | every commit, via thread-ci |
| 3 | `retrieval_gold_gate.py` (grounded regression floors), `retrieval_eval.py` by hand, `graph_eval.py`, `--behavior` | live archive + the golds' frozen snapshot | minutes | evaluating a deliberate ranking change |
| 3½ | `retrieval_eval.py --cases` on agent-mined golds (`thread_archive mine <miner>` to mint them) | a frozen corpus snapshot, corpus-grounded labels | seconds to score; agent-minutes per mined case | scoring against grounded labels; mining is an occasional cadence |
| 4 | `pytest -m beir`; `cdr_eval.py`, `haystack_eval.py --dataset …` by hand | external IR / conversational-memory benchmarks | tens of minutes (built homes cache for re-runs) | calibrating against published baselines |

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
- **`search_lab.py`** — the experiment bench. Races every configuration in
  `experiments/` against the shipped defaults and prints a leaderboard. A bare
  run scores **both benches** (`--gold` / `--synthetic` narrow to one): the gold
  bench over the snapshot-bound gold files (the fused pipeline over the frozen
  snapshot, one leaderboard per file — the promotion-grade delta, the same graded
  pools the gold gate floors) and the synthetic bench (`--models` for the fused
  pipeline) where a win is only a *direction*. The synthetic leaderboard lands in
  seconds while the gold pass is still running, so a gross regression shows
  immediately and the grounded verdict follows. `--sample FRAC` scores a
  deterministic subset of each gold file (the same hash-selected slice every run)
  — with `--only <experiment>` it turns the gold pass from tens of minutes into a
  couple, for fast iteration; it reads a *direction*, not the promotion delta, so
  drop it for the full-bench confirm before promoting.
- **`thread_archive mine`** — the gold miners (package `thread_archive._mine`),
  the only tokens-spending tier. Each mints snapshot-bound eval `--cases` files
  under `~/.thread/archive/`; `thread_archive mine` alone lists them, `thread_archive
  mine <miner> --help` documents one, and `thread_archive mine all [N]` sweeps the
  ones a count alone can drive. All bind by `snapshot_id` to the frozen corpus
  snapshot they run against (`thread_archive snapshot`; point `THREAD_ARCHIVE_HOME`
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
    `topic-cases-<slug>.jsonl`. Confound ranking. Batch (needs `--topic`).
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
    session↔commit provenance — `evals/swechat_corpus.py` builds one from SWE-chat
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
- **`experiments/`** — configurations-as-code for the lab; the contract is in
  its README.

## Taking a baseline (measure → change → measure)

"Take a baseline" before touching ranking means capturing numbers that stay
comparable after the change. The instruments are not interchangeable: **only
the minted gold files can credit an improvement.** Everything else on the
bench detects damage.

**Start here — the one-command read.** `python scripts/retrieval_gold_gate.py`
discovers every gold file, scores each over its bound snapshot with the
production ranker (at the canonical `limit=20`), and prints per-file MRR /
success@10 / recall@10 / nDCG@10. It is the CI regression gate, but the measured
numbers print on every run — floored files and freshly-mined ungated ones alike —
so it doubles as the fastest, most consistent read of where the baseline sits
right now, with no loop or aggregator to hand-roll (and no `limit` skew from
doing so). Drop to the per-file `retrieval_eval.py --cases` instrument below only
when you need the fuller metric set (success@1/5/20, recall@20, the natural-vs-code
per-shape split).

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
ones. `search_lab.py` remains the instrument for racing several *named*
experiments at once with a leaderboard; the gate is the instrument for one knob
at a time, against the floors that actually gate CI.

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
  present, over its own snapshot, on both sides of the change** (a file minted by
  a parallel instance an hour ago is part of the baseline too). The `thread_archive
  mine` miners produce them, each yielding graded pools (nDCG, via `grades`):
  - `mine query` (`judged-cases.jsonl`): one `claude` agent per real query reads
    the originating session for intent, sweeps the frozen snapshot with its own
    reformulated searches, reads candidates, and writes a graded, corpus-grounded
    case. The recall-capable rung.
  - `mine topic` (`topic-cases-<slug>.jsonl`): a survey agent searches a topic,
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
  against (`thread_archive snapshot`; point `THREAD_ARCHIVE_HOME` at it), and
  `retrieval_eval.py --cases` scores it over that snapshot — the freezing rule
  made mechanical. A file whose `snapshot_id` matches no snapshot on hand is
  stale: re-mine it, don't score it against a moved corpus.
- **Tier 0** is two shapes, both in every pytest run. The metric floors
  (`search_lab.py` / `tests/test_search_quality.py`) are near-saturated by
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
file, each over its own snapshot. Without those runs, report the change as
unverified — not as an improvement.

**Hold-out discipline.** A gold file tuned against repeatedly stops being a
measurement and becomes a training set. Keep at least two independently mined
files and tune against one while the other stays untouched until the
confirming run (`--only` narrows the gate to the tune side); re-mine on a cadence
when a file's snapshot goes stale.

**Read a delta in cases, not in points.** Scoring is deterministic — same code,
same snapshot, same numbers to the digit — so a movement is never noise. But on a
file of `n` cases a single case going from rank 1 to unfound moves any of these
metrics by at most `1/n`, which is the resolution the file actually has. A `+0.01`
on the 64-case findability file is two thirds of one case; on a 7-case topic file
it is a fourteenth of one. Anything under `1/n` is a rank shuffling within cases
that already worked, not a win — and it will not survive a hold-out.

## Changing ranking, start to finish

1. Write the change as an experiment in `experiments/` (a `SearchParams`
   value, or a `SEARCH` callable) with a falsifiable `HYPOTHESIS`.
2. `search_lab.py` — a bare run scores both benches: the synthetic leaderboard
   (does the direction hold?) lands in seconds, and the gold pass races that same
   experiment against the baseline over every minted gold file (each over its own
   snapshot), on the graded pools the gold gate floors — the delta that can
   actually credit the change. While iterating, `--only <experiment> --sample
   0.15` scores a deterministic slice of each file in a couple of minutes — a fast
   grounded direction; drop `--sample` for the full-bench run that credits the
   change. Tune against one file; confirm against the held-out one. (`--synthetic`
   / `--gold` narrow to one bench; `retrieval_eval.py --cases` scores a *single*
   production config over one file — reach for it to read a shipped config's
   absolute numbers, not to race a challenger.)
3. Promote once the gold-file delta holds (and `--from-log`, read as an alarm
   only, hasn't collapsed): fold the winner into `_retrieval/params.py` defaults,
   delete or keep the experiment as documentation, and let tier 0/2 ratchet the
   new shape.

## Cost and hygiene

- The `thread_archive mine` miners spend real tokens (headless `claude` calls;
  each miner's `--target` bounds them). Everything else on the bench is free.
- Mined output quotes real usage — case files, detail sidecars, and ledgers live
  under `~/.thread/archive/` (`retrieval-trend.jsonl`, `judged-cases.jsonl`,
  `topic-cases-<slug>.jsonl`, `rerank-cases.jsonl`, `findability-cases.jsonl`),
  never in the repo. The synthetic corpus is the one exception: no real data, so
  it's checked in.
- The fast tests guarding these harnesses live in `tests/`
  (`test_retrieval_eval.py`, `test_search_lab.py`, `test_mine_framework.py`,
  `test_retrieval_mine_gold.py`, `test_topic_mine_gold.py`, `test_graph_eval.py`,
  `test_beir_calibration.py`, `test_retrieval_gold_gate.py`) and run in every
  pytest pass — the lab stays runnable even when nobody has tuned search in
  months.
- The gold gate (`scripts/retrieval_gold_gate.py`) scores the gold files over
  their snapshot as a deliberate regression floor, run on a ranking change. It
  runs where the archive and the snapshot live; on a box
  without the snapshot, or while a gold file is mid-re-mine, the affected file is
  skipped, not failed. Its floors are calibrated a few points under measured —
  raise a floor when a shipped change lifts a number and holds; add a floor entry
  for a newly minted file (it rides ungated until you do).
