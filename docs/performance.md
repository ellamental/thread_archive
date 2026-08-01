# Retrieval performance — the situation, and what has been tried

The latency story of `thread_search` / `thread_read`: how the serving landscape
creates the numbers we see, what the instruments say, and the history of what has
been tried against them. Unlike the rest of the docs tree, this page is
deliberately historical — the failed and half-successful attempts are the record
that stops the next instance from re-deriving (or re-shipping) them. For the
instruments themselves see `search_lab/README.md`; for result *quality* see
`docs/public/search-quality.md`. Numbers here are from this archive (~274k packed
vectors, ~3.6M events) unless marked otherwise; treat them as orders of
magnitude, not constants.

## The one fact that frames everything

**The pipeline is fast; the serving landscape is slow.** The warm bench over
observed queries (`search_lab/latency_replay.py --baseline`, 2026-07-26) measured
p50 ≈ 230 ms, p95 ≈ 1.4 s. The usage ledger over the same weeks of real traffic
recorded p50 ≈ 2.7 s, p90 ≈ 12.8 s. That ~10x gap is not a slow ranker or a bad
SQL plan — it is **startup and residency cost landing inside requests**: model
loads, pack builds, page-cache fault-ins, and contention from the machine doing
other things. Every effective fix so far has been about *moving work off the
request path*, not about making the work faster (though that helps too).

After the 07-31 round the structural cold sources are closed: CLI one-shots
delegate, post-restart queries defer the matrix, warms serialize. Row-level
reads of the instrumented ledger show the shared server has **never** paid a
model load inside a search — warm serving works — and the remaining in-process
cold traffic is a handful of ad-hoc library scripts a week. The work left is
the **tail**, not the median: contention and honest broad scans (see the open
tails below).

## The serving landscape

Retrieval is served by whatever process imported the library, and the regimes
have wildly different cost structures:

- **The shared HTTP server** (`archive-mcp --http`, :8788, LaunchAgent) — the
  intended hot path. Warms at startup, keepalive holds pages, one resident
  ~2-3 GB copy for every client.
- **Per-client stdio MCP servers** — a deployment option, lean by policy
  (models load lazily on first use). On this box it carries no traffic: the
  local (untracked) `.mcp.json` points sessions at the shared :8788 server, and the
  ledger has never recorded an `mcp-stdio` search.
- **CLI one-shots** (`thread-archive search ...`) — pay whatever they need
  inside the one call, then exit; by construction nothing amortizes. `search`
  now delegates to the shared server when it is alive and serving the same
  (default) home (`_delegate.py`), so the common case rides the warm process
  (~0.5 s wall, mostly interpreter start) and only the fallback pays the
  ~5-10 s cold model load.
- **The web viewer / watcher cohost** — long-lived, warms, also *writes*
  (ingest + embed), so it is both a server and everyone else's contention.
- **Ad-hoc library consumers** (benches, sibling instances, lab code calling
  `api.search`) — uptime-zero processes that look like cold starts in the
  ledger because they are. With the CLI delegating, these are the *only*
  processes still paying model loads — 8 searches across the whole instrumented
  era, several of them the latency investigation's own cold-start probes.
  The model is per-process by nature (a live torch object; `model_slot.py`) —
  the mmap'd pack, graph partition, and index pages *are* shared via page
  cache, which is why even these processes' matrix cost is ~0.1 s, not 24 s.

The ledger (2026-07-20 → 31) recorded **233 warm passes in 11 days** — roughly
20 process starts a day paying a median ~21 s warm each (model load ~8 s, graph
~7 s, priming search ~2.5 s; since the 07-31 changes: ~5-10 s, graph served from
the persisted partition, matrix prime ~60-90 ms when a base pack is reusable).
Restart rate is therefore a first-order latency input: every restart is a cold
tail some request may catch.

## The instruments

- **`<home>/retrieval-usage.jsonl`** — the ground truth. Every search/read with
  `duration_ms`, per-stage timings (`_probe.py`), cold flags, contention sample
  (`load1`, `uptime_s`, `rss_mb`, `wal_age_s`, `refreshing`, `inflight`), plus
  `warm` / `refresh` / `serve` rows. Read it before theorizing. Caveats: rows
  before ~07-23 lack stage timings, rows before ~07-28 lack `surface`, and
  `uptime_s`/context landed mid-July — filter to the era whose fields you need.
- **`search_lab/latency_replay.py`** — replays the ledger's real calls (real
  parameters, not just real query text) warm and quiet; prints bench-vs-served
  side by side. `--baseline` records the reference the next run diffs against.
- **`search_lab/speed.py`** — the frozen-corpus bench for code deltas.

The recurring analysis mistakes, so they stop recurring: pooling eras across
code changes; pooling surfaces (a CLI one-shot and the shared server are
different products); reading arm totals as attribution (`fts_ms` and
`semantic_ms` overlap wall-clock — the substages are the real signal); comparing across restarts without splitting on the cold flags; and reading
process age as cost — a search in a young process did not necessarily pay a
model load (`embed_cold` and the stage timings are the cost record; bucketing
by `uptime_s` alone once produced a phantom "44% pay a model load").

## History: what was tried, in order

**Inline pack rebuild per token move (found 2026-07-22).** Original design
rebuilt the full vector matrix (~814 MB read + vstack) on the request thread
whenever the store token moved — which continuous ingest does every few minutes,
so under any concurrency searches serialized behind whole-corpus rebuilds and
timed out MCP clients. Fixed in two steps: **serve-stale + single-flight
background refresh** (a cached matrix answers immediately; staleness probed at
most once per 60 s cooldown; one rebuild in flight per key), then the **base +
delta pack** (`vector-pack/` mmap'd base + small in-RAM tail; a new vector costs
a delta read, a fresh base only past `_DELTA_MAX_ROWS`). This took the rebuild
off the *warm* request path entirely; background refreshes now measure p50
≈ 100 ms.

**Exact-set scans on page walks (found 2026-07-25).** Thread-granular tallies
(`matched_threads` / `count_matches`) rescanned the whole match set per page —
`set_ms` 30-50 s per page on a broad query walked to page 4. Fixed with the
watermark-keyed **set memo + bounded delta**: an unchanged index hands the memo
back (~1 ms), an appended-to index scans only the rows above the stored
watermark (~19 ms) instead of the full scan (~850 ms+). The `set_scans` /
`set_deltas` / `set_hits` counters exist so a regression here is visible again.

**Warm passes thrashing each other.** Restarts arrive in bursts (a deploy, a
crash loop), and two concurrent warms measured ~303 s each against ~7 s alone —
concurrent is far worse than the sum. Fixed with the machine-wide **warm flock**
(`.warm.lock`, bounded wait): passes queue, and `wait_ms` records the queue so a
slow restart is attributable.

**Eviction of idle servers.** A warmed server is a large quiet process — the
first thing the OS evicts — and eviction is invisible to every warmth flag
(objects constructed, pages gone). Measured: the same query 2.2 s then 0.34 s,
the difference being fault-in from swap. Fixed with the **keepalive** (a real
discarded search after 90 s idle) and, on unified-memory boxes, releasing the
torch allocator's peak after warm/drain (`release_accelerator_cache`).

**Queries racing the warm.** `set_defer_construction` — a warming server's
request path uses a model only if already resident, so the first query serves
lexical-only fast instead of blocking on (or duplicating) the cold load. The two
prongs are flagged separately on the search row: `embed_deferred` (model not
resident yet) and `matrix_deferred` (matrix cold). Neither is `cold` — a deferred
search pays no load, and counting it as the cold-model tail inverts the band.

**Attribution gaps.** Most of the above was hard to see before the ledger grew
its fields: per-stage timings and the semantic substages (07-23), `surface`
stamping and `serve` rows (07-28), contention/`uptime_s` context, `warm` and
`refresh` rows. Instrumentation-first has paid for itself every round; when a
number is confusing, the fix so far has usually been another recorded field,
not a guess.

**The 2026-07-31 round** (see CHANGELOG): the deferred-construction policy was
extended to the **matrix build** — the one remaining inline pack assembly, which
was still landing 24 s builds inside requests that raced the warm pass or a
cold scope (`matrix_deferred` now marks a search that sat the arm out while the
background refresh built it); `warm_models` primes the matrix as its own
recorded stage; **base-pack builds serialize on a pack-dir flock** (two rival
repacks measured ~156 s each vs ~20-40 s alone; the loser now usually mmaps the
winner's published files); and pack assembly decodes blobs in one `frombuffer`
pass instead of 274k per-row arrays. First search through the restarted shared
server after this round: 130 ms. The same round turned up a same-process tmp
race: the matrix refresher and the corpus-graph build both assemble packs, and
pid-only tmp names let one thread's `os.replace` consume the other's
half-written file (a `FileNotFoundError` killed a graph refresh on 07-30) —
fixed by the build flock plus pid+thread-id tmp names. The round closed with
**CLI search delegation**: a `thread-archive search` resolving to the default
home now asks the shared :8788 server over one stateless `tools/call` POST and
falls back in-process on any failure (`--local` forces the fallback; a
tool-level error is re-run locally so the CLI keeps its own error contract).
Measured same-minute: 0.47 s delegated vs 6.2 s local for the same query. The
serve row carries `delegated: true` — the door was the CLI, the engine row is
the server's.

## What deliberately hasn't been done, and the open tails

- **CLI `read` still runs in-process.** `search` delegates (see the landscape
  above); `read` doesn't, on purpose — it never loads a model (engine p50
  ~12 ms; a one-shot's cost is interpreter + import + open), and its exit code
  is decided by resolving the ref locally, a contract delegation would have to
  re-infer from rendered prose. Revisit only if terminal reads ever measure
  slow enough to matter.
- **Restart rate is development churn, not a fault.** The watcher log shows
  10-36 restarts *every day* (2026-07-20 → 31), all clean exits with no monitor
  recoveries — that is parallel instances restarting daemons after edits, which
  the house rules require. On a live-edited production box the answer is cheap
  restarts, not fewer; don't burn time hunting a crash loop that isn't there.
- **Substring/OR fallback scans** can cost ~9-13 s (`scan_ms`) on broad
  queries; bounded by design (they are the honest full answer), unbounded in
  feel. The other tail source alongside contention — if it ever needs work,
  the shapes are early-results-with-continuation, a scan budget, or term
  planning that keeps more queries off the scan path. The one recorded served
  instance of this tail turned out to be a semantics bug, since fixed:
  `match='substring'` stripped `OR`/`|` and scanned for the whole query as one
  literal, and a nothing-matches literal is the scan's worst case (no LIMIT
  ever stops it — full corpus walked twice, pool + exact set, for zero hits).
  Substring mode now unions the alternatives, so a matching-anything OR query
  terminates at its LIMIT instead. The honest-broad-scan tail proper (a rare
  substring that genuinely isn't there) remains open and by-design.
- **Contention is real and recorded, not fixed** — and it is now the
  first-order tail source: the worst warm-server rows (e.g. a 40 s search with
  6.4 s of *warm* embed time) are a busy box, not a cold anything. Test
  suites, embed drains, graph rebuilds, and warm bursts beside a search all
  show up in `load1`/`refreshing`/`inflight`. The ledger can tell a slow
  pipeline from a busy box; nothing yet *does* anything with that distinction.
  Plausible levers: yield background refresh/drain work while a request is in
  flight; bound what the cohosted writer does while serving. Before building
  any of that, let post-07-31 traffic accumulate and read the served tail
  *then* — the instrumented sample is small and its worst rows predate the
  fixes; aim at the tail that still exists, not the one just closed.
- **A stale-window ranking cost**: serve-stale means a vector written in the
  last refresh cooldown isn't semantically searchable yet. Accepted on purpose —
  the lexical arm covers the freshest rows.

## Where the numbers stand (baseline 2026-07-31)

The regimes have different honest numbers; quote them separately or the quote is
noise. As measured after the 07-31 round (bench: `latency_replay.py --limit 40
--reps 2`, baseline pinned in `<home>/latency-baseline-observed.json`):

| regime | p50 | tail | what moves it |
|---|---|---|---|
| pipeline (warm bench, quiet) | ~110 ms | p95 ~1.1 s | code changes — this is the regression number |
| shared server, warm, live | expect 0.1-1 s | 1-3 s | contention, wide sets, broad OR/substring asks |
| shared server, first ~10 s after restart | lexical-only, fast | — | queries pay neither the model load nor the matrix build; `embed_deferred` / `matrix_deferred` mark them |
| any process, warm pass | ~5-10 s once | — | model load ~5 s dominates; matrix ~0.1 s, graph from disk |
| CLI `search`, server up | ~0.5 s | ~1-3 s | interpreter start + whatever the warm server's answer costs |
| CLI one-shot, in-process (server down, `--local`, non-default home; all of `read`) | ~5 s | ~10 s+ | structural — the model load lands inside the call |

The bench p50 halved against the 07-26 baseline (230 → 109 ms; semantic arm
-70%, hydrate -66%) — read the exact delta loosely, since the observed query set
drifts between baselines. The served-side ledger over any window that spans a
code change is a mixed population; for "what are agents getting *now*", filter
served rows to dates after the last entry in the CHANGELOG's latency work and
split by `surface` and the cold flags, or just run the replay and read its
served panel against the bands it prints.

## Working on this

Same-day before/after only (the corpus grows under any longer comparison), split
by surface and cold flags, `latency_replay.py --baseline` before you start. The
suite's latency-adjacent tests (`test_vectors.py`,
`test_warm_serialization.py`, `test_warm_contention.py`,
`test_search_probe.py`) encode most of the invariants above — a change that
makes one of them awkward is probably re-opening a closed hole.
