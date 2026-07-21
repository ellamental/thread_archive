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
`thread_archive._eval`, so the shipped `archive eval` command (the operator's
read-only self-checkup over their own archive) and this dev bench score off one
code path. The bench is the *rest* of the ladder: the CI gate, the experiment
runner, and the LLM-judged tiers that answer "should we change ranking," none
of which ship.

## The quality ladder

Fastest tier first — climb until the evidence matches the stakes. (The
product README's "Measuring search quality" tells the same story with the
measured numbers.)

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` (in every pytest run) | checked-in synthetic corpus (`tests/quality_corpus.py`), lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding + rerank models | minutes | touching the model arms |
| 2 | CI `retrieval-gate` row (`retrieval_eval.py --from-log`) | live archive, mined click labels | ~minutes | every commit, via thread-ci |
| 3 | `retrieval_eval.py` by hand, `graph_eval.py`, `retrieval_judge.py`, `search_arena.py`, `--behavior` | live archive | minutes–hours | evaluating a deliberate ranking change |
| 3½ | `retrieval_eval.py --cases` on agent-mined golds (`retrieval_mine_gold.py` to mint them) | live archive, corpus-grounded labels | seconds to score; agent-minutes per mined case | scoring against grounded labels; mining is an occasional cadence |
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
  corpus-grounded, date-bounded gold cases (`~/.thread/archive/judged-cases.jsonl`);
  after the one-time spend, `retrieval_eval.py --cases` scores against them
  for free, deterministically.
- **`graph_eval.py`** — does the corpus-native embedding graph earn its
  ranking signal? Regression check for the shipped coherence re-rank, and the
  gate any new graph lever must pass.
- **`beir_eval.py`** — the external yardstick: the real pipeline over a public
  IR benchmark, next to published BM25/dense baselines. Answers "are the
  components embarrassing?", nothing about archive-domain quality.
- **`experiments/`** — configurations-as-code for the lab and arena; the
  contract is in its README.

## Changing ranking, start to finish

1. Write the change as an experiment in `experiments/` (a `SearchParams`
   value, or a `SEARCH` callable) with a falsifiable `HYPOTHESIS`.
2. `search_lab.py` (and `--models` if the model arms are involved) — does the
   direction hold on the bench?
3. `retrieval_eval.py --from-log` and, for the strongest offline evidence,
   `search_arena.py --experiment <name>` on real queries.
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
