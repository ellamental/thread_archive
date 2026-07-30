# Measuring search quality

What thread-archive's retrieval is measured against, what those numbers
license, and what nothing here can tell you. The instruments live in
`search_lab/` and stay there — an install ships no scoring surface.

The quality claim rests on public benchmarks: third-party corpora somebody else
labeled, read beside the baseline their own leaderboard publishes. Nothing
scores this archive's own corpus, because no protocol that labels it can be
trusted to — the reasoning is in "Why your own archive has no score" below.

## What search is measured against

Each row runs the real pipeline over a public benchmark. `lexical` is the base
install's stack; `+vectors` adds the optional embedding arm. Both name the same
pinned configuration everywhere on the bench.

| benchmark | task | metric | lexical | +vectors | published reference |
|---|---|---|---|---|---|
| BEIR scifact | scientific-claim IR, abstracts | nDCG@10 | 0.579 | 0.709 | 0.665 BM25 / 0.68 dense |
| BEIR nfcorpus | short medical documents | nDCG@10 | 0.291 | 0.356 | 0.325 BM25 / 0.33 dense |
| CDR | conversational retrieval | nDCG@10 | — | 0.490 | 0.504 best-of-16 |
| PerLTQA | personal-memory unit retrieval | nDCG@10 | 0.581 | 0.682 | none published |
| LoCoMo | multi-session dialog, turn-level | recall@10 | 0.615 | 0.672 | 0.662 DRAGON |
| LongMemEval-S | long-history QA, session-level | recall@10 | 0.941 | — | 0.710 BM25 / 0.823 Contriever |
| BEAM 100K | long-conversation memory, message-level | recall@10 | 0.639 | 0.663 | none published |

These are the numbers a release is held to, from
`search_lab/quality-baseline.json`; `tests/test_docs.py` holds the table to
them. A dash is an arm carrying no accepted number rather than one that fails —
CDR and LongMemEval are gated on a single arm each. PerLTQA is scored on a
deterministic 1,200-query sample of its 8,588 and CDR on 350 of its 1,583 — each a
different measurement from its full row, carrying its own accepted numbers.

**None of these corpora resemble an agent's session log**, so a strong number
certifies the retrieval machinery, never archive-domain quality. Read every row
against that mismatch. Each reports its own field's metric conventions
(linear-gain nDCG) rather than this archive's, which is the point of running
them — a number is a yardstick only if it means what the leaderboard beside it
means.

On the shipped default the fused stack meets or clears every comparable
reference except CDR's, where it sits at 97%. Nothing is tuned against these
corpora. Three readings worth having:

- **The lexical arm is the standing gap.** scifact at 0.579 trips the harness's
  own `BELOW BM25 — investigate` band, where the fused 0.709 clears the BM25
  reference by 0.044.
- **A flat number on a short-document corpus means the bm25 term is out of scale
  there, not idle.** Density is normalized to a fixed window but not bounded, so
  the term's effective strength scales inversely with document length: a
  116-char turn matching three terms scores about 13 where a 1500-char abstract
  matching three scores 1. LoCoMo's turns sit entirely inside that window and
  are unmoved by the term; session-level LongMemEval and abstract-level BEIR
  both move.
- **A weak CDR number is a ranking-weight symptom, not an embedder-size one.**
  The same `nomic-embed-text` spans a nearly two-fold range on that benchmark
  under different ranking weights.

LongMemEval-S is the easy split, scored over its 470 non-abstention questions
against references measured on the harder -M split, so read 0.941 as ballpark
rather than a matched win.

## What a release is gated on

`python -m search_lab gate --run --quick` holds the bench to those accepted
numbers, so a change that costs recall on somebody else's labels has to be fixed
or deliberately accepted before it ships. A green gate licenses exactly one
claim: **the retrieval components did not get worse in general, on corpora that
look nothing like an agent's session log.** It is not an archive-domain claim,
and no tightening would make it one.

Accepting a movement is `gate --quick --update`, which puts what was given up in
the release diff where a reader can see it.

Three things gate every commit, and none displays a quality number — a
per-commit metric invites being read as a score, which no local protocol can
support:

- **Tier 0**, in every pytest run: metric floors near-saturated on a checked-in
  synthetic corpus (`tests/test_search_quality.py` — they can only fall, a
  breakage detector), the exhaustive and chronological shapes ordering metrics
  can't score (`tests/test_search_recall_shape.py`, on nonce-term golds true by
  construction), and deterministic pipeline contracts
  (`tests/test_reality_mechanisms.py` — content types indexed, MCP default
  scope, reindex preserving what was findable).
- **`retrieval-gate`** — an arm-liveness probe only, asserting the embedding arm
  loads, so a dead model can't silently degrade fused search to lexical while
  every row stays green.
- **`latency-gate`** — replays the slowest recorded calls against a baseline of
  its own and reds when they got materially slower. A millisecond is not graded
  by the ranker that produced it, which is why speed can gate here and quality
  cannot.

`search_lab/README.md` carries the full ladder these sit in, and what each tier
licenses.

## What search costs

**Two numbers, and the gap between them is the finding.**

*As actually served*, over searches the usage ledger recorded warm: **p50 ~2.7 s,
p95 ~14.5 s, p99 ~45 s**. The retrieval arms account for about a second of that
median (`fts_ms` p50 636 ms, `semantic_ms` p50 345 ms), so most of a served
search's wall clock is in no stage the probe names.

*On the bench*, warm search is FTS-dominated and an order of magnitude cheaper:
p50 ~620 ms, p95 ~1.7 s, p99 ~2.2 s, of which the vector arm's whole cost is
~50 ms at the median. Replaying the observed query population instead of a
curated one lands in the same regime (p50 ~230 ms, p95 ~1.4 s), so query shape
is not what separates the bench from the product. The conditions differ — the
bench runs one query at a time in an idle process, the ledger is a long-lived
MCP server under a pipelining client, against a store ingest is writing — and
nothing here establishes how the gap divides between those.

**Quote the served numbers when the question is what search costs an agent, and
the bench numbers only when the question is whether a ranking change moved the
clock.** No ranking decision is made against the served ones.

Two properties hold the bench tail where it is: code-identifier queries ride
indexed token-MATCH fallbacks rather than a full-table substring scan, and cold
model loads are deferred to warm so they never land inside a request. The
per-search usage ledger carries a per-stage breakdown — the two arm totals, the
vector arm's internal split, and what else was competing for the machine — which
is what makes a latency change self-diagnosing rather than a bucket with a big
number in it.

## Why your own archive has no score

**No protocol that labels this archive's corpus can certify that search is good,
so nothing gates on one.** Two independent failures put it there, and a protocol
has to clear both.

**Circular labels.** A labeler that sweeps the corpus with the production ranker
marks what that ranker already reaches. A thread the stack systematically cannot
surface never enters the gold, so it can never be counted as missing, and every
number scored that way is an optimistic upper bound on itself by a margin
nothing inside the protocol can see. That holds whether the labeler is a judge
grading a retrieved pool, an agent reformulating queries to build one, or a
click log recording what an agent opened out of what search showed it. The
archive's `thread_search`→`thread_read` pairs are censored in exactly this way:
a change that surfaces different-better results scores as a loss.

**Queries nobody asked.** Escaping circularity means fixing labels against a
record outside search — a commit, an edit in the tool-use trail — but the query
must then be authored from that same artifact, and a query written to have a
knowable answer is not shaped like one an agent types.

Underneath both is a hard constraint: **a real query and a complete answer set
are not recoverable from the same record.** Nobody ever enumerated the answers to
`watcher ingest lock`; the only trace is what search returned and what the agent
opened.

The two populations are not close. The usage ledger records what agents actually
ask:

| | authored | observed |
|---|---|---|
| query length, median | 20 words / 122 chars | 4 words / 31 chars |
| shape | grammatical descriptive sentence | bag of terms (`watcher ingest lock`) |
| carries a scope/shape param | 0% | 43% (`content_type` 37%, `match` 31%) |
| paginates | 0% | 49% |
| uses an operator (`OR`, quotes, `\|`) | 0% | ~11% |
| subject | "find the session that made this code change" | infra debugging, ontology work, personal and emotional material |

Two consequences. The **scoped and structural code paths are unmeasured
entirely** — `path`, `content_type`, `match='substring'` and the query-less
browse carry a third of real searches and none of the authored ones, and the
last two are different retrieval shapes rather than filters on this one. And a
weight swept against 20-word queries may not hold for 4-word ones: density
normalizes matched terms against a fixed window, and the OR-fallback tier fires
when the strict all-terms pass comes up short — far likelier on a long query
than a three-token one.

## What else is not measured

Naming the holes, because a bench this narrow is easy to over-read:

- **The operator's own archive.** Every archive-domain claim rests on the
  synthetic tier-0 corpus, which proves only that nothing broke, and on
  inference from external corpora with very different selectivity.
- **Completeness.** The bench reads *findability* — where the first right answer
  lands — and says nothing about whether a window holds the several threads
  bearing on a subject, which is what an agent's fan-out workflow needs. BEAM is
  the only multi-answer row here, and it is not archive-domain.
- **Whether the shipped weights are right.** They are inherited, not currently
  re-derivable: the defaults are what ships and what every candidate is scored
  against, not a configuration this bench has confirmed. The mechanism argument
  for each term is in `_retrieval/params.py`, where the knobs are.
- **Query shape as a variable.** One property is measured, on a public corpus of
  agent sessions carrying commit linkage: queries naming *what was done* find
  the session (success@10 0.92 on identifier-bearing queries), and queries
  naming *why* it was done often do not (0.32 on intent-shaped ones). That gap
  is invisible to any protocol whose labels came from retrieval.

The one local instrument whose population is real measures speed, not quality —
`latency_replay.py`, which replays recorded calls with their parameters. The
archive's tool-use trail also supports `retrieval_eval.py --behavior`, which
reports usage rates per search (opened a result, searched again, walked away)
and mints no labels at all. Read its trend, never a single run.

## What your install reports

Not a score — a state you can act on. `thread-archive status` and the viewer's
health page report whether search is **degraded**: whether the arms load and the
index is current. A metric with no baseline beside it isn't something you can
act on, which is why an install ships none.
