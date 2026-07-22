# evals/ — the search lab

Everything that measures search quality lives here: the harness scripts, the
experiment configurations, and this manual. Day to day none of it runs by
hand — tier 0 rides every pytest pass and the CI retrieval gate rides every
commit. Come here when you're *changing ranking*: this directory is the whole
workbench, and the ladder below is the order to climb it.

Each script's docstring is its own full manual (protocols, biases, caveats);
this README is the map.

The scoring core these scripts share — the case protocols (title sampling, log
mining) and the MRR/recall loop — lives in the package at
`thread_archive._eval`, so the shipped `thread_archive eval` command (the operator's
read-only self-checkup over their own archive) and this dev bench score off one
code path. The bench is the *rest* of the ladder: the CI gate, the experiment
runner, and the LLM-judged tiers that answer "should we change ranking," none
of which ship.

## The quality ladder

Fastest tier first — climb until the evidence matches the stakes.
(`docs/search-quality.md` tells the same story with the measured numbers.)

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` (in every pytest run) | checked-in synthetic corpus (`tests/quality_corpus.py`), lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding + rerank models | minutes | touching the model arms |
| 2 | CI `retrieval-gate` row (`retrieval_eval.py --from-log`) | live archive, mined click labels | ~minutes | every commit, via thread-ci |
| 3 | `retrieval_eval.py` by hand, `graph_eval.py`, `retrieval_judge.py`, `search_arena.py`, `--behavior` | live archive | minutes–hours | evaluating a deliberate ranking change |
| 3½ | `retrieval_eval.py --cases` on agent-mined golds (`retrieval_mine_gold.py` to mint them) | a frozen corpus snapshot, corpus-grounded labels | seconds to score; agent-minutes per mined case | scoring against grounded labels; mining is an occasional cadence |
| 4 | `pytest -m beir` | external BEIR benchmark | tens of minutes | calibrating against published baselines |

## The instruments

All run from the repo root with the repo venv, all read-only against the
archive (BEIR and the lab build throwaway homes and never touch it).

- **`retrieval_eval.py`** — the hub. Scores search with MRR / recall@k under
  three case protocols: `--auto-titles` (zero-curation proxy), `--from-log`
  (real search→read pairs mined from the archive's own tool-use trail — the
  CI gate's protocol), `--cases` (a checked case file, e.g. mined golds).
  Every other live-archive instrument reuses its miner (`mine_log_cases`).
- **`search_lab.py`** — the experiment bench. Races every configuration in
  `experiments/` against the shipped defaults on the synthetic corpus and
  prints a leaderboard. Seconds by default; `--models` for the fused
  pipeline. A win here is a direction, not a verdict.
- **`search_arena.py`** — the promotion instrument. Duels a challenger from
  `experiments/` against the shipped configuration on real mined queries,
  blind, side-randomized, judged by a headless `claude`; sign test on the
  wins. The bar to clear before changing `_retrieval/params.py` defaults.
- **`retrieval_judge.py`** — pointwise LLM grading of what production search
  returns for real queries: graded precision, click-label calibration, and
  credit for relevant results the click protocol scores as misses.
- **`retrieval_mine_gold.py`** — spends one `claude` agent per query to mint
  corpus-grounded gold cases (`~/.thread/archive/judged-cases.jsonl`) against a
  frozen corpus snapshot (`thread_archive snapshot`; point `THREAD_ARCHIVE_HOME`
  at it). Each case records the snapshot's `snapshot_id`; after the one-time
  spend, `retrieval_eval.py --cases` — run over that same snapshot — scores
  against them for free and deterministically, and refuses cases once the
  snapshot's id no longer matches (the corpus moved; re-mine).
- **`graph_eval.py`** — does the corpus-native embedding graph earn its
  ranking signal? Regression check for the shipped coherence re-rank, and the
  gate any new graph lever must pass.
- **`beir_eval.py`** — the external yardstick: the real pipeline over a public
  IR benchmark, next to published BM25/dense baselines. Answers "are the
  components embarrassing?", nothing about archive-domain quality.
- **`experiments/`** — configurations-as-code for the lab and arena; the
  contract is in its README.

## Taking a baseline (measure → change → measure)

"Take a baseline" before touching ranking means capturing numbers that stay
comparable after the change. The instruments are not interchangeable: **only
the minted gold files can credit an improvement.** Everything else on the
bench detects damage.

- **Minted gold case files ARE the baseline.** Enumerate them first —
  `ls ~/.thread/archive/*cases*.jsonl` — and score **every file present, over
  its own snapshot, on both sides of the change**; a file minted by a parallel
  instance an hour ago is part of the baseline too. Two mining families
  produce them, and both yield graded pools (nDCG, via `grades`):
  - *Query-mined* (`retrieval_mine_gold.py`): one `claude` agent per real
    query reads the originating session for intent, sweeps the frozen
    snapshot with its own reformulated searches, reads candidates, and writes
    a graded, corpus-grounded case.
  - *Topic-mined*: starts from a large curated topic dense with confounds
    and mints queries plus graded result-sets from multiple angles
    (`topic-cases-*.jsonl`).

  Each case is bound by `snapshot_id` to the corpus snapshot it was mined
  against (`thread_archive snapshot`; point `THREAD_ARCHIVE_HOME` at it), and
  `retrieval_eval.py --cases` scores it over that snapshot — the freezing rule
  made mechanical. A file whose `snapshot_id` matches no snapshot on hand is
  stale: re-mine it, don't score it against a moved corpus.
- **Tier 0** is two shapes, both in every pytest run. The metric floors
  (`search_lab.py` / `tests/test_search_quality.py`) are near-saturated by
  design (MRR ≈ 1.0 on the synthetic corpus) — they can only fall: a
  breakage detector, not an improvement meter. The mechanism goldens
  (`tests/test_reality_mechanisms.py`) each reproduce, on a synthetic corpus,
  the failure shape behind a real incident and pin the property that keeps it
  fixed (recall a gold the flood would bury, reach an answer that lives only in
  tool/thinking content) — regression guards, run every pytest pass;
  `pytest tests/test_reality_mechanisms.py -q` re-checks them in seconds.
- **From-log numbers are alarms, not baselines.** The CI gate's `--from-log`
  protocol (trend ledger at `~/.thread/archive/retrieval-trend.jsonl`) mines
  click labels from the live trail: the gold is whatever thread the agent
  opened, which is a subset of what search surfaced *that day*. The labels
  are censored by the incumbent ranker — a change that surfaces
  different-better results scores as a loss, and a high score mostly means
  "ranks like the ranker that took the clicks." Read the trend for one
  question only — *did something collapse* — and never cite a from-log delta
  as evidence a change helped. The trail's lasting value to this bench is as
  a **sampling frame**: real query shapes to seed the gold miner with, not a
  labeler.

**Claim discipline.** Green tier 0 plus a quiet CI gate license exactly one
claim: "search didn't break." The claim "search improved" requires a gold-file
delta scored on both sides of the change, and changing shipped defaults
additionally requires the arena. Without those runs, report the change as
unverified — not as an improvement.

## Changing ranking, start to finish

1. Write the change as an experiment in `experiments/` (a `SearchParams`
   value, or a `SEARCH` callable) with a falsifiable `HYPOTHESIS`.
2. `search_lab.py` (and `--models` if the model arms are involved) — does the
   direction hold on the bench?
3. `retrieval_eval.py --cases` on every minted gold file (each over its own
   snapshot) — the delta that can actually credit the change — and, for
   promotion-grade evidence, `search_arena.py --experiment <name>` on real
   queries.
4. Promote: fold the winner into `_retrieval/params.py` defaults, delete or
   keep the experiment as documentation, and let tier 0/2 ratchet the new
   shape.

## Cost and hygiene

- `retrieval_judge.py`, `search_arena.py`, and `retrieval_mine_gold.py` spend
  real tokens (headless `claude` calls; `--sample` bounds them). Everything
  else is free.
- Judged/mined output quotes real usage — dumps and ledgers live under
  `~/.thread/archive/` (`retrieval-trend.jsonl`, `retrieval-judge.jsonl`,
  `judged-cases.jsonl`), never in the repo. The synthetic corpus is the one
  exception: no real data, so it's checked in.
- The fast tests guarding these harnesses live in `tests/`
  (`test_retrieval_eval.py`, `test_search_lab.py`, `test_search_arena.py`,
  `test_retrieval_judge.py`, `test_retrieval_mine_gold.py`,
  `test_graph_eval.py`, `test_beir_calibration.py`) and run in every pytest
  pass — the lab stays runnable even when nobody has tuned search in months.
