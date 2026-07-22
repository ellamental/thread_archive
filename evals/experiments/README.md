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
.venv/bin/python evals/search_lab.py            # lexical stack, seconds
.venv/bin/python evals/search_lab.py --models   # fused pipeline (embeds the corpus; minutes)
.venv/bin/python evals/search_lab.py --only no_recency,pool_order
.venv/bin/python evals/search_lab.py --json out.json
```

The leaderboard scores every configuration on the identical cases
(`tests/quality_corpus.CASES`) with the same MRR/recall loop as the
live-archive harness, baseline first, deltas against it. The corpus is synthetic and
lexically easy — a small delta here is a *direction*, not a shipping verdict;
promote a winner by re-measuring on the live tiers before changing `params.py`
defaults.

## Promoting

The promotion bar is the snapshot-bound gold files: score the challenger and
the shipped configuration with `evals/retrieval_eval.py --cases` on **every
minted gold file, each over its own corpus snapshot**, on both sides of the
change. A challenger that improves the gold-file delta (and doesn't collapse
`--from-log`, read as an alarm only) has earned a defaults change.

`tests/test_search_lab.py` keeps every module here loadable and
contract-conformant on every pytest run.
