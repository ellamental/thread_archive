# The benchmark and corpus landscape

What external material exists to measure archive's retrieval against, what shape
each piece is in, and what it would cost to put on the bench. `search_lab/README.md`
is the manual for the instruments that exist; this is the survey of what could
feed them.

Two things are worth as much of as we can get:

- **Turnkey benchmarks with deterministic scoring** — a corpus, queries, and
  relevance labels somebody else produced, scored by id-match rather than by an
  LLM's opinion. These are the only labels on the bench that no ranker of ours
  chose.
- **Corpora shaped like agent sessions** — Claude Code / Codex / Cursor
  transcripts. These carry no labels, so each needs a mining pass; what makes one
  worth mining is the **signal** it ships that can fix gold outside retrieval.

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

## Part 1 — Turnkey benchmarks

### On the bench now

| benchmark | shape | task | labels | scoring | metric |
|---|---|---|---|---|---|
| BEIR scifact | shared corpus | scientific-claim IR | human (TREC-grade) | deterministic | nDCG@10 |
| CDR | shared corpus | conversational retrieval | human | deterministic | nDCG@10 |
| LoCoMo | per-question haystack | multi-session dialog, turn-level | human | deterministic | recall@k |
| LongMemEval-S | per-question haystack | long-history QA, session-level | human | deterministic | recall@k |

**LoCoMo carries a known label-quality problem.** An audit found 99 score-corrupting
errors across 1,540 questions (6.4%) in the answer key — hallucinated facts,
incorrect temporal reasoning, speaker misattribution. The audit targets the QA
answer key rather than the `evidence` turn ids archive scores against, but both come
out of the same annotation pipeline, so treat the LoCoMo number as having a noise
floor of several points and never read a small delta on it.

### Free — `beir_eval.py` already supports these

[beir_eval.py:74-90](../search_lab/beir_eval.py#L74-L90) carries published BM25 and
dense references for 13 BEIR datasets; only scifact has been run. Adding one is a
`--dataset` value and CPU time. The binding constraint is the embed pass: scifact's
5.2K docs cost ~15 min, so budget **~350 docs/min**.

| dataset | docs | embed | why it's informative here |
|---|---|---|---|
| `nfcorpus` | 3.6K | ~10 min | short medical docs — the short-document regime where the bm25 term goes out of scale |
| `arguana` | 8.7K | ~25 min | long argument passages, counterargument retrieval — the opposite length regime |
| `scidocs` | 25.6K | ~75 min | title+abstract, citation relevance |
| `fiqa` | 57.6K | ~3 hr | financial QA — the only BEIR set with conversational query shapes |
| `trec-covid` | 171K | ~8 hr | deep judgments per query, heavy |
| `quora` | 523K | ~1 day | duplicate-question, near-paraphrase task |
| `webis-touche2020` | 382K | ~18 hr | argument retrieval, notoriously BM25-favouring |

`fever` (5.4M), `hotpotqa` (5.2M), `climate-fever` (5.4M), `dbpedia-entity` (4.6M)
and `nq` (2.7M) are out of reach on one box. Corpus sizes here are the published
BEIR figures, not measured locally.

`nfcorpus` + `arguana` are the pair worth running first: they bracket document
length, which is the axis the bm25 term is known to be sensitive to, so they
measure a *named* sensitivity rather than adding an undifferentiated row.

### One loader away

Each of these needs a `*_groups()` generator in the shape of
[haystack_eval.py:142-195](../search_lab/haystack_eval.py#L142-L195) plus a dispatch
branch, or a HuggingFace fetch branch beside `beir_eval.py`'s UKP zip fetcher.

| benchmark | shape | labels | scoring | domain distance | note |
|---|---|---|---|---|---|
| **BRIGHT** | shared corpus | human | deterministic nDCG@10 | StackOverflow / LeetCode / Pony subsets are close | 1,384 queries, 12 subsets. Reasoning-intensive: best published nDCG@10 is ~24.3, so it has enormous headroom and won't saturate. `xlangai/BRIGHT` on HF |
| **CoIR** | shared corpus | human | deterministic nDCG@10 | code text-shape, uncovered today | 10 datasets, **BEIR schema** — reads nearly unchanged. `codesearchnet`, `stackoverflow-qa`, `cosqa`, `apps`, `codefeedback-mt/-st`, `codetrans-dl/-contest`, `synthetic-text2sql`, `codesearchnet-ccr`. Apache-2.0, `CoIR-Retrieval` on HF |
| **BEAM** | per-question haystack | human-validated | deterministic (`source_chat_ids`) | conversational, **has a coding split** | 100 conversations, 2,000 questions at 128K/500K/1M/10M token scales. CC BY-SA 4.0, `Mohammadta/BEAM` + `Mohammadta/BEAM-10M`. The 128K tier is cheap; 10M is not |
| **CORE-Bench L2** | shared corpus | **provenance** (patch-aligned git diffs) | deterministic nDCG@10 | issue→edit localization over real repos | 5,061 queries / 632 repos. Labels come from SWE-bench-family diffs — no LLM, no ranker. Corpus is 9.38M chunks, so it needs repo-scoping to be runnable. `zhangfw123/CORE-Bench` |
| **LongMemEval-V2** | per-question haystack | human-curated | deterministic (annotated answer trajectories) | **web-agent trajectories** — closest published thing to our corpus | 451 questions. Medium tier is up to 500 trajectories / 115M tokens. No published retrieval recall@k baselines, so it'd be an internal yardstick with no leaderboard beside it |
| **PerLTQA** | per-question haystack | human | deterministic | personal-memory dialogue | 8,593 questions, 30 characters, explicit *Memory Retrieval* subtask so labels exist by construction. Small and cheap |
| **FreshStack** | shared corpus | **LLM-generated** nuggets (GPT-4o, ~90% precision) | deterministic (α-nDCG@10, Coverage@20, Recall@50) | StackOverflow + GitHub code/docs — close | 5 niche technical domains. Included in RTEB. Labels are LLM-authored, so it sits one tier below the human-labeled sets — but the *scoring* is deterministic and the domain is right |
| **CORE-Bench L3** | shared corpus | LLM voting + agent traces | deterministic | broader-context retrieval | 2,580 queries, 106K labels. Same caveat as FreshStack — usable, but LLM-provenance |

**RTEB** (the MTEB leaderboard's retrieval section) is a source rather than a
benchmark: it aggregates public retrieval sets across legal / finance / code /
medical with held-back private splits. Worth mining for candidates; the private
splits are unavailable to us.

### Right task, wrong corpus size

TREC CAsT / iKAT, QReCC, and TopiOCQA are the classic conversational-search sets
and the closest thing in traditional IR to searching a conversation. They retrieve
from MS MARCO or full Wikipedia — 25–54M passages — which is not embeddable here.
Subsampling to the qrels pool makes them runnable but breaks comparability with the
published numbers, which is the only reason to run them. Skip unless someone wants
a pooled-corpus variant with the caveat stated in the output.

## Part 2 — Corpora to mine

No labels ship with these. What decides whether one is worth the mining spend is the
**signal** available to fix gold outside retrieval, because a corpus mined by
searching with our own stack reproduces the circularity on new data and buys
nothing.

Signals, best first:

- **`git` commit linkage** — the session lives in (or is linked to) a repo, so a
  commit fixes which session did the work. No search runs during labeling and the
  labeler never reads the target thread, so there's no vocabulary leakage either.
- **file-edit provenance** — the trajectory records which files a session touched,
  which yields multi-answer gold sets no ranker had a hand in choosing.
- **task/issue linkage** — the session resolves a known issue, which supplies a
  natural query.
- **none** — labels would have to come from an agent searching the corpus.
  Circular; don't bother.

| corpus | sessions | harnesses | shape | mining signals | status |
|---|---|---|---|---|---|
| **SWE-chat** | ~6,000 / 200+ repos | Claude Code (85%), OpenCode, Gemini CLI, Cursor, Factory Droid | native Claude Code JSONL for the CC share | **commit linkage with line-level authorship**, file edits, issue text | fully ingested — 5,124 sessions / 189 repos, no cap; Claude-Code-shaped only |
| **SpecStory** | 14,789 (2,588 CLI) / 1,441 repos | IDE + CLI agents | timestamped Markdown under `.specstory/history/` **committed into the repo** | **commit linkage free from `git log`** over the history dir; file edits | needs a Markdown importer + a license filter |
| **CORE-Bench L2 source repos** | 632 repos | n/a — PR/diff data, not sessions | git diffs | patch-aligned labels, already extracted | usable as a benchmark directly (Part 1) |
| **SWE-rebench / SWE-Hero OpenHands trajectories** | 34K (SWE-Hero) | OpenHands scaffold | agent step traces, JSONL | issue linkage, file edits; **synthetic** (Qwen3-Coder-generated, not human-driven) | large but model-generated — session *shape* without real user intent |
| **CLI trajectory analysis** (`xz-Sean/cli_trajectory_analysis`) | 1,794 runs / 63K steps | MiniSWE, OpenHands, Terminus2 × 7 models | annotated trajectories, CC BY 4.0 | task linkage (Terminal-Bench), success/failure labels | small; benchmark-task-driven rather than real usage |
| **trace-commons/agent-traces** | ~30 | Claude Code, Codex, Pi, Cursor, OpenCode | **native JSONL** + Parquet, donated from public repos | public-repo certification → commit linkage possible | seed-scale. Native format means our importer reads it unchanged — worth watching, not worth mining yet |
| **cfahlgren1/agent-sessions-list** | 20 (4 MB) | Claude Code, Codex, Hermes, Factory/Droid, Pi | native session traces | none stated | seed-scale, same note |
| **WildChat-1M** | 1.04M conversations | ChatGPT (not agentic) | chat turns, ODC-BY | none | conversation-shaped at scale, but no tool use, no repo, no agentic structure |
| **LMSYS-Chat-1M** | 1M conversations / 25 models | chat arena | chat turns | none | non-commercial terms only; same shape objection as WildChat |

### The cheapest real win is already downloaded

The embed-budget cap is gone: archive now ingests every readable SWE-chat
transcript, 5,124 sessions over 189 repositories, and the bench runs on all of it.
What is still untouched is **~900 sessions across four other harnesses** — OpenCode
(624), Gemini CLI (56), Cursor (19) and Codex (213) — each costing an importer or
an export path, and each buying the one thing this page cannot otherwise get:
cross-harness generalization, which nothing currently measures.

The bigger constraint moved with the cap. The corpus is now 5,124 documents and the
query set is 75, all single-gold, all from one protocol. More corpus does not fix
that; more *labels* do, and the only ungrounded-by-search signal left in the
download is the commit linkage, which already yields 1,284 attributable sessions
against the 75 currently mined.

### The signal nobody else has

Archive's own tool-use trail records which sessions edited which file. That is
**file-edit provenance over our own corpus**: multi-answer gold sets, in domain,
that no ranker selected. No external corpus can match it on domain, and no mined-
by-searching protocol can match it on independence.

### Rejected, with reasons

- **TraceLab** (UW SyFI, 8,058 sessions / 665K steps, Claude + Codex) — deliberately
  excludes prompt text, tool inputs, and file paths. It is a workload/telemetry
  dataset. There is nothing to search.
- **MemoryAgentBench, EvoMemBench, StreamMemBench, PersonaMem, DialSim, PERMA** —
  score the *answer* via LLM judge, not the *retrieval*. No evidence-id labels means
  no qrels, so nothing drops into this bench without inventing labels first.
- **MSC / ConversationChronicles** — multi-session dialogue, but the session
  structure is persona continuity rather than evidence location; no retrieval gold.

## Cost model

- Embedding runs at **~350 docs/min** on this box (measured: BEIR scifact, 5.2K docs,
  ~15 min). Multiply corpus size by that before committing to a row.
- Built corpora live under `~/.cache/thread-evals` (`<root>/<dataset>` for a
  download, `<root>/homes/<name>` for a built home), are large, and are entirely
  rebuildable — the whole tree is safe to delete.
- `python -m search_lab benchmark`'s standard tier is the set whose corpus is
  already built. Every row added here grows it; the plan estimator prices each row
  from what it actually took last time, so `--list` is the honest budget.
- A mining pass spends tokens per case; a turnkey benchmark spends none. That is
  the whole difference between Part 1 and Part 2 on the ledger.
