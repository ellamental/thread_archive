# The benchmark and corpus landscape

What external material exists to measure archive's retrieval against, what shape
each piece is in, and what it would cost to put on the bench. `search_lab/README.md`
is the manual for the instruments that exist; this is the survey of what could
feed them.

One thing is worth as much of as we can get: **turnkey benchmarks with
deterministic scoring** — a corpus, queries, and relevance labels somebody else
produced, scored by id-match rather than by an LLM's opinion. Those labels are the
only ones on this bench that no ranker of ours chose, which is the whole reason a
number off them means anything.

Corpora shaped like agent sessions — Claude Code / Codex / Cursor transcripts —
are the domain match, and they carry no labels. Producing labels for them locally
is not on the table: see `docs/public/search-quality.md` → "Why your own archive has no
score" for why every scheme for doing so either grades the ranker with itself or
asks questions nobody asked.

## The two axes that decide whether a benchmark is worth anything

**Label provenance** — who decided what's relevant:

| provenance | what it means | independence |
|---|---|---|
| human | annotators judged query↔doc pairs | full |
| provenance | a git diff, a PR, a commit fixed the label mechanically | full, and no vocabulary leakage |
| LLM-generated | a model wrote or graded the labels | partial — inherits the model's blind spots |
| ranker-derived | labels established by searching with the engine under test | **none** — the ranker grades itself |

**Scoring** — how a run turns into a number: deterministic (qrels, evidence-id
match, recall@k) or LLM-judge. An LLM-judge benchmark cannot be a regression gate:
the judge is a moving part, so two runs of identical code disagree, and the whole
value of a frozen corpus evaporates.

A benchmark is useful here if it is **deterministic in scoring** and at least
partially independent in **provenance**. Domain match is the third axis and it is a
weighting, not a gate — a biased-but-independent measurement beats a circular one.

## Turnkey benchmarks

### On the bench now

Seven datasets, and what each is on the bench *for* — a row that measures nothing
the others don't is a number without a question behind it.

| benchmark | shape | task | labels | scoring | why it is here |
|---|---|---|---|---|---|
| BEIR scifact | shared corpus | scientific-claim IR, abstracts | human (TREC-grade) | nDCG@10 | the reference point — mid-length documents |
| BEIR nfcorpus | shared corpus | short medical documents | human | nDCG@10 | the **short** end of the document-length range |
| CDR | shared corpus | conversational retrieval | human | nDCG@10 | conversational query shapes |
| LoCoMo | per-question haystack | multi-session dialog, turn-level | human | recall@k | turn-granularity memory |
| LongMemEval-S | per-question haystack | long-history QA, session-level | human | recall@k | session-granularity memory |
| BEAM (100K tier) | per-question haystack | long-conversation memory, message-level | human-validated | recall@k | **completeness** — the only multi-answer gold on the bench |
| PerLTQA | shared corpus | personal-memory unit retrieval | by construction | nDCG@10 / MRR@10 | memory-*unit* granularity |

Every one is deterministic in scoring. Two carry no published retrieval baseline
(BEAM, PerLTQA) and say so in their own output rather than borrowing a number
from a different task.

**PerLTQA is the row the quick tier exists for.** Its full 8,588-question set takes
about 38 minutes across the lexical and vector arms — most of the full tier's whole
cost — so the quick tier scores a deterministic hash sample of 1,200 per arm,
preserving coverage across people and memory types while bringing the pair near 5
minutes. The `~1200` row names make that measurement boundary explicit, and the
full rows still owe every question.

**BEAM is scored narrower than it ships**, and both deviations are stated where a
reader of a number will meet them. Only the **100K tier** is carried: the 500K and
1M tiers are the same 20 conversations extended, so the length ladder they buy
costs ~11 hours of embed to re-ask questions the 100K tier already asks. And three
of its ten categories are skipped as not-retrieval-questions — `abstention` (no
gold by design), `summarization` (matches the whole corpus by construction, and
gold up to 16 messages caps a perfect retriever at recall@10 = 0.625), and
`event_ordering` (asks for a sequencing over a broad topic, with no distinguishing
content to match on). The list and the reasoning live in
`haystack_eval.BEAM_UNSCORED`. `instruction_following` scores low and is
deliberately kept: finding the message that answers a broad question is retrieval
doing its job.

### Built and runnable, held off the bench on cost

Both have a working harness, downloaded data, and a place they would earn — they
are off the default set because between them they are 537K documents and about 22
hours of embedding, against roughly 4 for everything above.

| benchmark | cost | what it would buy | how to run it |
|---|---|---|---|
| **MTRAG** | 366K passages, ~15 hr | the only external read on **query shape**: one information need as a terse context-dependent last turn (median 46 chars, the closest thing anywhere to the ~31-char queries the usage ledger records) and as a standalone human rewrite, with a published BM25/BGE/Elser baseline for each | `search_lab/mtrag_eval.py --queries lastturn --vectors`. One domain (`--domain govt`, 49.6K passages, ~2 hr) buys the lastturn-vs-rewrite comparison for a seventh of the embed, giving up comparability with the published 4-domain macro-average |
| **BEIR trec-covid** | 171K docs, ~7 hr | the only set with deep enough per-query judgment pools to make a recall@100 mean something, and it buys that back with 50 queries — expensive once, near-free on every pass after | `search_lab/beir_eval.py --dataset trec-covid --vectors` |

**LoCoMo carries a known label-quality problem.** An audit found 99 score-corrupting
errors across 1,540 questions (6.4%) in the answer key — hallucinated facts,
incorrect temporal reasoning, speaker misattribution. The audit targets the QA
answer key rather than the `evidence` turn ids archive scores against, but both come
out of the same annotation pipeline, so treat the LoCoMo number as having a noise
floor of several points and never read a small delta on it.

### Free — `beir_eval.py` already supports these

[beir_eval.py](../search_lab/beir_eval.py) carries published BM25 and dense
references for 12 BEIR datasets; four are on the bench. Adding another is a
`--dataset` value and CPU time.

| dataset | docs | embed | why it would be informative |
|---|---|---|---|
| `scidocs` | 25.6K | ~1 hr | title+abstract, citation relevance — no *named* sensitivity, which is why it is not on the bench |
| `fiqa` | 57.6K | ~2 hr | financial QA with conversational query shapes — but MTRAG ships the same FiQA corpus chunked to passages, so a row here would measure one corpus twice |
| `quora` | 523K | ~19 hr | near-paraphrase duplicate detection. High lexical overlap, so it isolates the semantic arm — and its 10K queries make it the heaviest *recurring* row on the list, which is what keeps it off |
| `webis-touche2020` | 382K | ~14 hr | argument retrieval, the most BM25-favouring set in BEIR (BM25 0.367 beats strong dense at 0.20) with only 49 queries |

`fever` (5.4M), `hotpotqa` (5.2M), `climate-fever` (5.4M), `dbpedia-entity` (4.6M)
and `nq` (2.7M) are out of reach on one box. Corpus sizes are the published BEIR
figures; embed times are computed from this box's measured throughput below.

**A lexical-only row skips the embed pass entirely**, and ingest runs about eight
times faster than embedding. That reframes the two heavy BM25-favouring sets:
`webis-touche2020` costs a couple of hours of ingest rather than fourteen of
embed, and its 49 queries make it near-free to re-run. Since the lexical arm is
the standing gap — scifact lexical trips the harness's own `BELOW BM25 —
investigate` — that is the cheapest available test of whether the gap is real or
a scifact artifact.

### One loader away

Each of these needs a `*_groups()` generator in the shape of the loaders in
[haystack_eval.py](../search_lab/haystack_eval.py) plus a dispatch branch, or a
HuggingFace fetch branch beside `beir_eval.py`'s UKP zip fetcher.

| benchmark | shape | labels | scoring | domain distance | note |
|---|---|---|---|---|---|
| **BRIGHT** | shared corpus | human | deterministic nDCG@10 | passage retrieval, not conversation | 12 subsets, 1,384 queries total; `pony` is 7,894 docs / 112 queries, `stackoverflow` 107,081 / 117, `leetcode` 413,932 / 142. Reasoning-intensive: best published nDCG@10 ~24.3, so it has enormous headroom and cannot saturate. The counter-argument is that it is *designed* so retrieval-without-reasoning fails — the intended solution is LLM query expansion, which this stack does not do, so the likely outcome is a number pinned low that never moves, which credits improvement as poorly as a saturated row. Unknown until run once. `xlangai/BRIGHT` |
| **CoIR** | shared corpus | human | deterministic nDCG@10 | **wrong task** | 10 datasets in BEIR schema, so it would read nearly unchanged (`cosqa` 20,604 docs, `stackoverflow-qa` 19,931, `apps` 8,765, `codetrans-contest` 1,008). But it retrieves *code given natural language*, where archive retrieves *conversations that contain code* — the document is a snippet, not a session discussing one. A good number here says the embedder is not confused by code tokens, which is narrower than it sounds. `CoIR-Retrieval`, Apache-2.0 |
| **CORE-Bench L2** | shared corpus | **provenance** (patch-aligned git diffs) | deterministic nDCG@10 | issue→edit localization over real repos | 5,061 queries / 632 repos. Labels come from SWE-bench-family diffs — no LLM, no ranker; the best label provenance on this page. 9.38M chunks: **runnable lexical-only** (~43 hr of ingest, unattended, no scoping needed), out of reach with the semantic arm (~14 days of embed at this box's rate). Its 5,061 queries also make it heavy to re-run. `zhangfw123/CORE-Bench` |
| **FreshStack** | shared corpus | **LLM-generated** nuggets (GPT-4o, ~90% precision) | deterministic (α-nDCG@10, Coverage@20, Recall@50) | StackOverflow + GitHub code/docs — close | 5 niche technical domains. Included in RTEB. Labels are LLM-authored, so it sits one tier below the human-labeled sets — but the *scoring* is deterministic and the domain is right |
| **CORE-Bench L3** | shared corpus | LLM voting + agent traces | deterministic | broader-context retrieval | 2,580 queries, 106K labels. Same caveat as FreshStack — usable, but LLM-provenance |

**RTEB** (the MTEB leaderboard's retrieval section) is a source rather than a
benchmark: it aggregates public retrieval sets across legal / finance / code /
medical with held-back private splits. Worth trawling for candidates; the private
splits are unavailable to us.

### Rejected on inspection, not on paper

**LongMemEval-V2 is not a retrieval benchmark.** It is the closest published
corpus to this one — web-agent trajectories, which carry tool calls where every
other conversational set is plain dialogue — and it still cannot go on the bench.
`questions.jsonl` carries exactly `{id, domain, environment, question_type,
question, image, answer, eval_function}`: an answer string and a string
evaluator, with **no annotation of which trajectory holds the answer**. The
haystacks are shared across all questions in a domain, so there is no per-question
ordering to recover a label from either. Scoring it as retrieval would mean
inventing the labels, which is the one thing this lab is built around not doing.
Its QA task is real; it is simply a different task.

Worth stating because it is not visible from the dataset card, the paper's
framing, or the file listing — only from the schema.

### Right task, wrong corpus size

MTRAG covers most of what these would be wanted for — multi-turn conversational
retrieval with human qrels, at a corpus size that fits — so these are a residue
rather than a gap.

TREC CAsT / iKAT, QReCC, and TopiOCQA are the classic conversational-search sets
and the closest thing in traditional IR to searching a conversation. They retrieve
from MS MARCO or full Wikipedia — 25–54M passages — which is not embeddable here.
Subsampling to the qrels pool makes them runnable but breaks comparability with the
published numbers, which is the only reason to run them. Skip unless someone wants
a pooled-corpus variant with the caveat stated in the output.

## Cost model

- Two rates, and the ratio between them is what decides most of this page.
  **Embedding runs at ~450 docs/min; ingest runs at ~3,600 docs/min** — both
  measured on this box, ingest from a fresh nfcorpus build and embed from the
  same run's vector pass. Ingest also *decays* as the FTS index grows (188/s at
  the start of a 3K-document build, 102/s by the end), so treat its rate as an
  optimistic ceiling on a large corpus and the embed rate as flat.
- The 8× gap is the lever. A **lexical-only row pays no embed pass at all**, which
  is what puts several corpora written off as multi-day embeds back within an
  afternoon of ingest — and the lexical arm is where the standing gap is.
- Embed throughput is the binding constraint on this whole page, and it is an
  engineering number rather than a law: batching, or the local model daemon, would
  move every estimate here.
- Built corpora live under `~/.cache/thread-evals` (`<root>/<dataset>` for a
  download, `<root>/homes/<name>` for a built home), are large, and are entirely
  rebuildable — the whole tree is safe to delete.
- **Every downloaded corpus is pinned to a content hash** (`search_lab/dataset-pins.json`,
  `python -m search_lab pins`). None of the upstreams offer an immutable handle —
  BEIR is a bare zip URL, three are a clone of a default branch, two are Hugging
  Face `resolve/main` — so a re-fetch is exactly where a corpus would silently
  become a different one. Harnesses verify before they build; deleting and
  re-downloading the tree is safe only if the bytes come back the same, and the
  pin is what tells you they did.
- `python -m search_lab benchmark` runs the set whose corpus is already built.
  Every row added here grows it; the plan estimator prices each row from what it
  actually took last time, so `--list` is the honest budget.
