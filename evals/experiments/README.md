# evals/experiments/ — the search lab's configurations

Each `.py` file here is one **retrieval configuration**: a candidate version of
the search stack, scored against the shipped one on the checked-in quality
corpus by `evals/search_lab.py`. A configuration is code — it can turn one
weight, or replace whole stages.

## Contract

A module (filename = experiment name; `_`-prefixed files are skipped) defines:

- `HYPOTHESIS: str` — the one-sentence claim the experiment tests. Required;
  an experiment without a falsifiable sentence is a knob twiddle.
- exactly **one** of:
  - `PARAMS: SearchParams` — a declarative configuration
    (`thread_archive._retrieval.SearchParams`; every ranking weight, pool
    size, and decay constant). The lab runs the production pipeline with it.
  - `SEARCH(query, **kw)` — a full search callable, for experiments params
    can't express (different fusion, a reordered stage, an extra filter). It
    receives the harness's kwargs (`limit`, `content_types`,
    `exclude_content_types`, `rerank`) and returns ranked hit dicts carrying
    `thread_id` — wrap `thread_archive._retrieval.search` and change what you
    like.

## Running

```
.venv/bin/python evals/search_lab.py                       # gold + synthetic (default)
.venv/bin/python evals/search_lab.py --gold                # gold bench only
.venv/bin/python evals/search_lab.py --synthetic --models  # synthetic only, fused pipeline
.venv/bin/python evals/search_lab.py --only pool_order --sample 0.15  # fast iterate
.venv/bin/python evals/search_lab.py --only no_recency,pool_order
.venv/bin/python evals/search_lab.py --json out.json
```

A bare run scores **both benches** (`--gold` / `--synthetic` narrow to one). The
leaderboard scores every configuration on identical cases with the same
MRR/success/true-recall/nDCG loop as the live-archive harness, baseline first,
deltas against it. `--sample FRAC` scores a deterministic subset of each gold
file (the same hash-selected slice every run, nested as `FRAC` grows) — pair it
with `--only <experiment>` to iterate in a couple of minutes instead of the full
bench's tens. A subset is a *direction* on grounded data, not the promotion
delta: read it while tuning, then drop `--sample` for the full-bench confirm. The gold bench is the snapshot-bound gold files (graded,
corpus-grounded pools over the frozen snapshot), one leaderboard per file — the
promotion-grade delta; the synthetic bench (`tests/quality_corpus.CASES`,
lexically easy) lands in seconds and only points a *direction*. Both together:
the fast break-check returns while the grounded verdict is still computing.

## Promoting

The promotion bar is the snapshot-bound gold files: a bare `evals/search_lab.py`
run (or `--gold`) races the challenger against the shipped configuration over
**every discovered gold file, each over its own corpus snapshot** — read the ΔMRR
(and nDCG@10) per file. A challenger that improves the gold-file delta (and
doesn't collapse `--from-log`, read as an alarm only) has earned a defaults
change. Tune against one file and confirm on a held-out one (`evals/README.md` →
"Hold-out discipline").

`tests/test_search_lab.py` keeps every module here loadable and
contract-conformant on every pytest run.
