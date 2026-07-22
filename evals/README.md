# evals/ — the search lab

Everything that measures search quality lives here: the harness scripts, the
experiment configurations, and this manual. Day to day none of it runs by
hand — tier 0 rides every pytest pass and the CI retrieval gate rides every
commit. Come here when you're *changing ranking*: this directory is the whole
workbench, and the ladder below is the order to climb it.

Each script's docstring is its own full manual (protocols, biases, caveats);
this README is the map.

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
| 2 | CI `retrieval-gate` (arm-liveness probes) + `retrieval-gold-gate` (gold-file regression floors) | live archive + the golds' frozen snapshot | ~a minute | every commit, via thread-ci |
| 3 | `retrieval_eval.py` by hand, `graph_eval.py`, `--behavior` | live archive | minutes | evaluating a deliberate ranking change |
| 3½ | `retrieval_eval.py --cases` on agent-mined golds (`retrieval_mine_gold.py` to mint them) | a frozen corpus snapshot, corpus-grounded labels | seconds to score; agent-minutes per mined case | scoring against grounded labels; mining is an occasional cadence |
| 4 | `pytest -m beir` | external BEIR benchmark | tens of minutes | calibrating against published baselines |

## The instruments

All run from the repo root with the repo venv, all read-only against the
archive (BEIR and the lab build throwaway homes and never touch it).

- **`retrieval_eval.py`** — the hub. Scores search with MRR / success@k /
  true recall@k / nDCG@k under
  three case protocols: `--auto-titles` (zero-curation proxy), `--from-log`
  (real search→read pairs mined from the archive's own tool-use trail —
  collapse alarm only), `--cases` (a snapshot-bound case file, e.g. mined
  golds — the baseline instrument). Success asks whether any answer ranks;
  recall measures how much of the complete grade-2 set ranks; nDCG scores the
  ordering of the whole 2/1/0 pool. `--probes-only` skips the metric run for
  the CI gate's arm-liveness checks. Every other live-archive instrument
  reuses its miner (`mine_log_cases`).
- **`search_lab.py`** — the experiment bench. Races every configuration in
  `experiments/` against the shipped defaults on the synthetic corpus and
  prints a leaderboard. Seconds by default; `--models` for the fused
  pipeline. A win here is a direction, not a verdict.
- **`retrieval_mine_gold.py`** — spends one `claude` agent per query to mint
  corpus-grounded gold cases (`~/.thread/archive/judged-cases.jsonl`) against a
  frozen corpus snapshot (`thread_archive snapshot`; point `THREAD_ARCHIVE_HOME`
  at it). Each case records the snapshot's `snapshot_id`; after the one-time
  spend, `retrieval_eval.py --cases` — run over that same snapshot — scores
  against them for free and deterministically, and refuses cases once the
  snapshot's id no longer matches (the corpus moved; re-mine).
- **`topic_mine_gold.py`** — mints golds from a curated **topic** dense with
  confounds instead of from real queries. A survey `claude` agent searches the
  topic, decides how many *angles* it warrants (its own call), and authors one
  query per angle with the intent, the confounds, and the candidate threads it
  found; then one labeler agent per angle builds on those candidates — verifying
  and expanding them with its own searches — and grades a comprehensive pool
  (2=intended, 1=partial, 0=confound). Same snapshot binding as the query miner;
  writes `topic-cases-<slug>.jsonl`.
- **`graph_eval.py`** — does the corpus-native embedding graph earn its
  ranking signal? Regression check for the shipped coherence re-rank, and the
  gate any new graph lever must pass.
- **`beir_eval.py`** — the external yardstick: the real pipeline over a public
  IR benchmark, next to published BM25/dense baselines. Answers "are the
  components embarrassing?", nothing about archive-domain quality.
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
per-shape split) or to score a *challenger* configuration on both sides of a change
— the delta the gate, a single-side floor, does not measure.

- **Minted gold case files ARE the baseline.** The gate enumerates and scores
  them for the current-state read; for a challenger delta, score **every file
  present, over its own snapshot, on both sides of the change** (a file minted by
  a parallel instance an hour ago is part of the baseline too). Two mining families
  produce them, and both yield graded pools (nDCG, via `grades`):
  - *Query-mined* (`retrieval_mine_gold.py`): one `claude` agent per real
    query reads the originating session for intent, sweeps the frozen
    snapshot with its own reformulated searches, reads candidates, and writes
    a graded, corpus-grounded case.
  - *Topic-mined* (`topic_mine_gold.py`): a survey agent searches a curated
    topic, decides the angles it warrants, and authors one query per angle with
    the candidates it found; one labeler agent per angle builds on those and
    grades a pool over the snapshot (`topic-cases-<slug>.jsonl`).

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

**Claim discipline.** Green tier 0 plus a quiet CI gate license exactly one
claim: "search didn't break." The `retrieval-gold-gate` CI row makes that "didn't
break" grounded rather than synthetic — it scores the gold files over their
frozen snapshot on every commit and fails on a drop below a calibrated floor
(`scripts/retrieval_gold_gate.py`) — but it is a **one-way floor, not a displayed
score**: it holding means the ranking didn't regress past the baseline, never
that it improved. The click-label protocols stay off the per-commit path for the
opposite reason (they are censored by the incumbent, so a per-commit click-MRR
invites being misread as quality); the gold files can ride CI precisely because
they are grounded and gated as a floor. The claim "search improved" still
requires a gold-file delta scored on both sides of the change — every minted
file, each over its own snapshot. Without those runs, report the change as
unverified — not as an improvement.

**Hold-out discipline.** A gold file tuned against repeatedly stops being a
measurement and becomes a training set. Keep at least two independently mined
files and tune against one while the other stays untouched until the
confirming run; re-mine on a cadence when a file's snapshot goes stale.

## Changing ranking, start to finish

1. Write the change as an experiment in `experiments/` (a `SearchParams`
   value, or a `SEARCH` callable) with a falsifiable `HYPOTHESIS`.
2. `search_lab.py` (and `--models` if the model arms are involved) — does the
   direction hold on the bench?
3. `retrieval_eval.py --cases` on every minted gold file (each over its own
   snapshot) — the delta that can actually credit the change. Tune against
   one file; confirm against the held-out one.
4. Promote: fold the winner into `_retrieval/params.py` defaults, delete or
   keep the experiment as documentation, and let tier 0/2 ratchet the new
   shape.

## Cost and hygiene

- `retrieval_mine_gold.py` and `topic_mine_gold.py` spend real tokens
  (headless `claude` calls; `--sample` bounds them). Everything else is free.
- Mined output quotes real usage — dumps and ledgers live under
  `~/.thread/archive/` (`retrieval-trend.jsonl`, `judged-cases.jsonl`,
  `topic-cases-<slug>.jsonl`), never in the repo. The synthetic corpus is the
  one exception: no real data, so it's checked in.
- The fast tests guarding these harnesses live in `tests/`
  (`test_retrieval_eval.py`, `test_search_lab.py`,
  `test_retrieval_mine_gold.py`, `test_graph_eval.py`,
  `test_beir_calibration.py`, `test_retrieval_gold_gate.py`) and run in every
  pytest pass — the lab stays runnable even when nobody has tuned search in
  months.
- The `retrieval-gold-gate` CI row (`scripts/retrieval_gold_gate.py`) scores the
  gold files over their snapshot on every commit as a regression floor. It runs
  where the archive and the snapshot live (same as the arm-probe row); on a box
  without the snapshot, or while a gold file is mid-re-mine, the affected file is
  skipped, not failed. Its floors are calibrated a few points under measured —
  raise a floor when a shipped change lifts a number and holds; add a floor entry
  for a newly minted file (it rides ungated until you do).
