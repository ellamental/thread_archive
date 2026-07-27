# Changelog

## Unreleased

- **The search lab treats its three corpus families the same way.** The bench had
  grown a private-archive path with real discipline and two side paths without it,
  and the divergence was in the load-bearing places. Now one module
  (`search_lab/eval_home.py`) decides what every harness had been deciding
  separately: *which home is safe to build into* — the refusal now covers overlap
  in both directions and protects whatever `THREAD_ARCHIVE_HOME` names, where
  BEIR's and CDR's equality check would have let `--home ~/.thread` through to an
  `rmtree`; *which arms are pinned* — `lexical` now means the same stack in every
  harness, coherence stood down with the semantic arm it reads vectors from,
  rather than three spellings of it; *whether a cached corpus still describes what
  was asked for* — a `--max-docs` smoke build gets its own home, so it can neither
  overwrite the full corpus nor read back as one; and *warming before the first
  scored query*, which only the gold bench did, leaving every external number to
  be split by whenever the background graph build happened to land. All benchmark
  corpora now share one cache root (`~/.cache/thread-evals`).
- **The SWE-chat hold-out is gated like the archive's own corpus.** It was
  documented as the independent hold-out and plumbed as a side project: no floors,
  no ledger, its mining denominators landing in the private gold dir describing
  cases that were not there. Its 22 gold files now carry floor sidecars, its
  ledgers live beside its cases, and the gate reaches it through `--snap` /
  `--gold-dir` (env vars still work). A run is one `(snapshot, gold dir)` pair and
  the second corpus is a second invocation — separate processes, because the stack
  caches a corpus graph and a vector pack per engine and swapping homes under
  those is how one corpus gets scored against another's structures.
- **`--calibrate` writes a gold file's floor.** The rule — each metric at most
  `1/n` under measured, one case of tolerance — was documented in three places and
  implemented in none, so a freshly mined file stayed ungated until someone did
  the arithmetic by hand. It runs only after a clean full pass of the shipped
  configuration, and never lowers an existing floor: calibrating after a
  regression cannot write the regression in as the new expectation.
- **The gold and mining ledgers record the commit again.** `git_commit()` resolved
  the repo root by a path depth that stopped being true when the module moved into
  `search_lab/`, so it had been walking up past this checkout and recording
  nothing. It now derives the root from the module's own location and verifies the
  toplevel matches before trusting a SHA — archive is a nested repo, and a ledger
  row naming another repository's HEAD is worse than one naming none.
- **The exported SWE-chat benchmark says which nDCG it means.** The manifest
  advertised its measures as reproducible under `ir_measures`/`trec_eval`; three
  of the four are, but nDCG is not — the reference scorer uses exponential gain
  and `trec_eval` uses linear, and every pool here carries the grade-1 rows that
  make them disagree. The gain function is now named in the manifest and in the
  scorer's own contract.

- **Health notices can be silenced, and a silenced one is still counted.** The
  action queue had no answer to "yes, I know" — a warning about a backup that
  shares a disk on purpose, or an account export nobody is going to re-download
  this week, sat in the queue forever and taught the reader to skim past the
  whole board. Every notice now carries a **Silence** control; the queue heading
  shows an *N silenced* indicator that opens the held-aside notices in full, each
  with **Unsilence**. Two rules keep a silence from becoming a blindfold: it is
  bound to the condition's fingerprint (the notice's text with counts and ages
  elided), so a fault that changes shape — a second failing stage, a different
  degradation reason — speaks up again while a warning that only ages stays
  quiet; and it retires the moment its notice stops firing, so a fixed-then-
  regressed fault is never hidden by the silence made about the first occurrence.
  Silences live in `<home>/silenced-notices.json` beside the health records, and
  `thread_archive status` prints what the page is holding back so the terminal
  never omits a warning by inheriting a UI choice. The notices themselves moved
  server-side (`_ops/notices.py`, `GET /api/notices`) — the judgment over the
  health records now has one implementation instead of living in the viewer's
  bundle, which is what lets a silence be honored everywhere. The write guard the
  export upload already carried is now the general one: `X-Archive-Write`.

- **The retrieval page has margins.** It rendered flush to the window edges —
  its root carried a class no stylesheet defined, so the page had no container at
  all. It now shares the measure the health page and the import page use, at all
  three widths.

- **The gold corpus is discovered, never named.** A gold file used to be named for
  the topic it was mined from, so a private subject travelled into every place the
  file was referenced. Mining now names a file by `case_token()` — a digest of the
  topic's own id, which is already opaque — and the repo names no gold file at all.
  Floors moved from a central manifest to a sidecar beside each file (`X.jsonl` is
  gated by `X.floor.json`), so the calibrated set falls out of discovery with no
  list to drift. `--require`'s fail-closed check reads the expectation from the run
  ledger, which already recorded what the last run measured: a newly mined file
  joins by being scored once, a retired one leaves after a single run without
  `--require`. The published quality table reports per *miner* rather than per file.
  One discovery rule now serves all three readers (`search_lab/gold_files.py`) —
  the gate, the bench export, and the window-fill harness previously each had their
  own glob, and the bench's skipped `X-detail.jsonl` while missing `X.detail.jsonl`.

- **The gold corpus and its calibration both stay with the operator.** The case
  files were already outside this repo, but the gate's floor table was not, and
  its keys are gold filenames — which topic mining derives from the subject titles
  of a private archive. Publishing the mechanism meant publishing the names of
  what someone talks about. Floors now load from `<gold_dir>/gold-floors.json`,
  beside the cases they calibrate: `{basename: {mrr, success10, recall10,
  ndcg10}}`, optionally with `by_difficulty`. The repo keeps the machinery, the
  operator keeps the corpus and its manifest. A missing floors file reads as
  *nothing calibrated* — every present file scored and reported, none gated — and
  `--require` treats that as the failure it is, so the fail-closed lane cannot go
  quiet by losing a file. The published quality table anonymizes the two rows that
  named private subjects.

- **The retrieval page is back, as a dev page.** The view that reports on the
  search *pipeline* — served latency split by warm and cold regime, per-stage
  costs, gold-run quality — went away with the search lab's extraction, but it is
  the only place any of that is legible. It returns at `/retrieval`, reading the
  lab's report rather than a copy of it, so the extraction stands. What changed is
  its prominence: the navigation no longer advertises a maintainer's instrument to
  someone who came to read their conversations. `thread_archive web dev` turns the
  dev pages on and the viewer remembers the choice; `thread_archive web --no-dev`
  puts them away. The route itself always resolves — hiding a page from the only
  person who can reach a loopback viewer would be theatre — and since the report
  lives in `search_lab/`, an install without the lab gets a `404` that names why
  instead of an empty report pretending to be a measurement.

- **The measurement surface is out of the product: `search_lab/` is where it
  lives, and an install no longer carries any of it.** Four things went, and each
  was an instrument wearing a product's clothes. The viewer's `/retrieval` page —
  warm-vs-cold latency percentiles and a gold-run MRR series, sitting in a
  stranger's primary navigation, and never in the README's supported-URL table.
  The `eval` verb, which reported MRR/nDCG over your own archive: a number with no
  baseline beside it isn't actionable, and the click-label protocol behind it is
  censored against being read as a quality score in exactly the way a bare metric
  invites. And `snapshot` + `archives`, which existed to serve the bench — frozen
  corpus homes, role-tagged registry entries — while contradicting the README's own
  *Not supported: more than one machine, and merging archives*. What an install
  reports about search instead is whether it is **degraded**, which is a state you
  can act on: the capability matrix on `status` and the health page, unchanged.

  Six modules moved to `search_lab/`, where the harnesses that read them already
  live: the scoring core (`eval_core.py`, was `_eval.py`), `snapshot.py`,
  `speed.py`, `gold_runs.py`, `mine_runs.py`, and `retrieval_report.py`. The two
  that were also commands kept one: `python search_lab/snapshot.py <dir>` freezes
  a corpus, `python search_lab/retrieval_report.py` prints the series. `_ops/` is
  now the durability kit and nothing else — backup, restore, drill, verify,
  nightly, coverage, health — which is what its name claimed.

  The **archive registry is deleted outright**, not moved: `~/.thread/archives.json`,
  the auto-register on every open, `--set-role`, the `/api/archives` endpoint, and
  the health page's "Archives on this machine" section. As a toe in the water for
  multi-archive support it never got past listing — no merge, no federated search,
  no selection — so it advertised a capability that wasn't there. The backup and
  restore paths lose their `suppress_registration` guards with it (staging homes and
  drill temps had to be kept *out* of a registry that no longer exists), and the
  health page's load history now reads this archive's own `/api/loads` ledger, live
  phase progress included.

  **Mining moved with it: `thread_archive._mine` is now `search_lab/mine/`, and
  the `mine` verb is gone from the CLI.** The miners drive headless `claude`
  agents to mint the graded gold cases the lab's harnesses score against — their
  output has never been useful anywhere but beside those harnesses, and they were
  already excluded from the wheel, so the verb was a command that could only ever
  print a pointer to the repo. Run them as `python -m search_lab.mine` (bare
  lists, `<miner> --help` documents one, `all [N]` sweeps). The agents' corpus
  seam is now an absolute path to `search_lab/mine/__main__.py` rather than
  `-m thread_archive._mine`: it is also their Bash allowlist prefix, so it has to
  resolve from whatever directory a session starts in, which `-m` on a
  repo-root-relative package cannot. With mining out of the tree, `src/` holds no
  bench code at all and the wheel needs no exclusions to say so.

  One repo-wide consequence: the dependency-tier meta ratchet scans a product's
  *shipped* modules, and it now reads the wheel's own exclude list to decide what
  those are — so repo-only code may import repo-only code (which is what lets a
  wheel-excluded package reach the lab; lab's own `experiments/` gains the same
  latitude). Propagated byte-identically to all nine products' copies, as the
  lockstep test requires.

- **`status` and the health page now say what the archive costs on disk, and how
  much of that is rebuildable.** An archive grows several times larger than the
  conversations in it — the SQLite projection routinely exceeds the JSONL it is
  built from, the vector pack sits beside it, the source mirror and drift
  quarantine keep provider files the provider itself has pruned, and migrations
  and repairs leave payloads behind that nothing collects — and none of that was
  visible anywhere in the product. "Why is this 30 GB" had no answer short of
  `du`. Both surfaces now split the total four ways, which is what makes the
  number answerable rather than alarming: truth (irreplaceable), index
  (rebuildable with `reindex` + `embed`), retained raw sources (kept on purpose,
  never auto-pruned), and everything else — with the largest entries named,
  because an unlabelled remainder is exactly where a stale migration backup or a
  bench cache hides. The walk is deliberately kept off `status`'s own API, which
  the viewer polls every 30 seconds; the viewer reads it from `/api/disk` on a
  five-minute cadence of its own, and a failed walk degrades that one section
  instead of the page.

- **The viewer's first search no longer pays the model load.** The watcher warms
  the retrieval stack at startup when it cohosts the web viewer, the way the shared
  MCP server already did for its clients. Before this, a new user's first search was
  the one that loaded the torch models — 15–40 seconds behind a bare `searching…`,
  with every search after it sub-second, so the whole cost landed on the one query
  that forms someone's impression of the product. The window before the warm lands is
  covered too: the process defers model construction to the warm pass, so a query
  racing it serves lexical-only in milliseconds and the vector / re-rank arms rejoin
  once the models are resident, rather than the query blocking on a load of its own.

- **`evals/` is now `search_lab/`, and the old experiment runner is gone.**
  The directory is the search lab — the name it went by in prose while the
  directory said something vaguer. What blocked the rename was a `search_lab.py`
  inside it: a leaderboard runner that raced configuration modules from
  `experiments/` against the shipped defaults. It measured on the synthetic
  corpus, where a win is only a direction, and the instrument that can actually
  promote a change — `retrieval_gold_gate.py`, one knob at a time against the
  floors CI enforces — had superseded it. Both are deleted along with the 16
  checked-in experiment configs. The `SearchParams` seam they rode is untouched
  and still load-bearing for the gate and `quality_corpus.run_cases(params=...)`;
  the three tests that hold it open moved to `tests/test_search_params.py`. The
  bench's on-disk cache stays at `~/.cache/thread-evals` — renaming it would
  invalidate tens of gigabytes of downloaded BEIR/LoCoMo corpora for nothing.

- **The base install is lexical-only all the way down: Leiden moved behind an
  extra.** `leidenalg` + `igraph` are the only dependencies with a narrow
  wheel matrix (no musllinux-aarch64 at all, a manylinux floor of 2.28), so they
  were the only reason `pip install thread-archive` could turn into a source
  build needing a C toolchain. They now live in a `leiden` extra that
  `[embeddings]` pulls in, because the graph they partition is built from the
  vector pack — a lexical-only install never builds it and never calls the
  engine. A new `[all]` extra is every runtime feature under one name. The
  librarian, whose topic graph needs the engine without needing vectors, depends
  on `thread-archive[leiden]` directly.

- **A degraded search feature is now visible instead of silent.**
  `thread_archive status` and the viewer's health page report the capability
  matrix behind search — the Leiden community engine, the vector arm, the
  cross-encoder — and distinguish a feature this install simply doesn't have
  (`off`, a choice) from one that is running on a lesser substitute
  (`degraded`). Only the second raises an action. That distinction is what makes
  the extra safe: with vectors on but Leiden absent, the coherence re-rank runs
  on Louvain and sits below the archive's own gated recall floor while every
  other check on the page stays green.

- **A re-run of `setup` can no longer re-enable a source you turned off.** The
  flow rewrote the whole source policy from each run's answers, so a second run
  that pressed Enter (or `s`, or came from `--yes`) cleared every per-source
  opt-out — capture the operator had declined, quietly restored by the command
  that exists to revisit choices. Policy now changes only where a policy is
  stated: the edit pass, whose per-source question is seeded with what that
  source is set to now, so Enter through it changes nothing and a "yes" is what
  lifts an opt-out. Disabled stores are listed unchecked (`[ ] … (off — e to
  change)`), stay out of the import, and the offer says "import the checked
  ones" rather than "import all" when any is off; with every found store off,
  only the edit is offered. An unreadable `config.json` still seeds nothing —
  it is the file the run replaces.

- **Setup ends in the archive, not at a URL.** With the watcher installed, its
  cohosted viewer is already serving — so the wizard's last question offers to
  open it, and a first install finishes looking at its own conversations
  instead of at a localhost address to copy by hand. The offer waits for the
  just-installed watcher's port to answer before opening anything, and is asked
  only of a terminal: `--yes` (agents, scripts) records `not-offered` and never
  puts a window on someone's desktop. Both host actions — the port probe and
  the browser — go through `_setup.machine.Machine` like every other thing
  setup does outside the archive home, so the whole flow stays scriptable.

- **The install story is the package, not the clone.** The README, releasing
  doc, and install docs now lead with `pip install thread-archive &&
  thread_archive setup`; the clone with an editable venv is the from-source/
  development path (claude-install.md now says so up front), and releasing
  gains a build + PyPI publish step plus registry yank guidance. `self-update`
  stays the source-clone updater and, on a wheel install, now points at
  `pip install -U thread-archive` instead of just reporting "nothing to update
  against". Alongside: repo files that spoke only to the maintainer's machine
  were generalized (host/README grants table and its stale `_launchd.py`
  pointer, ci.toml/ci.yml comments, monorepo-ancestry narration in docstrings),
  the root TODO scratch note and stale `dist/` artifacts are gone, the empty
  `docs/plans/` directory is removed, and the local `host/repair-dumps/` undo
  payloads moved out of the repo tree into `~/.thread/archive/repair-dumps/`.

- **Retrieval has a terminal now: `thread_archive search` and `thread_archive
  read`.** The archive could be searched by an agent over MCP or browsed in the
  viewer, but not from the shell it lives in — a question you wanted answered
  before opening a client meant starting a client. The two verbs close that: every
  tool parameter is a flag (`--since 7d`, `--source cursor`, `--path rank.py
  --path-ops edit,write`, `--group browse`, `--output linkable` for pipeable
  JSON), an omitted query browses recent threads, and `read` takes the same three
  ref shapes the tool does — ULID, legacy integer id, or the session uuid the
  harness knows.

  They are not a second implementation, which is the point. The tools moved out
  of the MCP server into `thread_archive/_tools.py`, and both doors call it: the
  clamping, the default scope, the degradation notice, the commit note, and the
  usage-ledger record are one copy of the code, so an answer typed at a prompt is
  the answer the agent would have gotten. The MCP server keeps what is genuinely
  its own — transport, bind plan, and the cohosted catch-up ingest a one-shot CLI
  process has no business kicking — and registers the shared functions as its
  tools, schema and description built from their real signatures.

  Retrieval verbs had been kept out on purpose ("one retrieval surface, not
  three"); what changed is the reading of that rule. The thing worth refusing is
  a second implementation of search, not a second way to reach the first one.
  `search` and `read` join the public surface the tools already carried; the rest
  of the CLI stays private operational tooling. Ledger rows from a terminal carry
  `surface: "cli"` — an operator's queries are a different population from an
  agent's, and the evals sample from that file.

- **An account export can be dropped on the viewer.** Web chats were the one
  source with no path in that didn't involve a terminal: you downloaded a ZIP,
  then had to find `~/.thread/archive/dumps/` in a file manager or know
  `thread_archive import-export` existed. The new `/upload` page takes the drag —
  and, as much to the point, says where each provider's export is hidden
  (claude.ai, ChatGPT and xAI each bury it under a differently-named settings
  page), what the ZIP becomes, and what happens to the download afterward.

  `POST /api/upload` spools the body to a dot-prefixed temp file, classifies it
  through the same provider `detect` the drop watcher uses, then renames it into
  `<home>/dumps/`. It imports nothing itself: the export-drop watcher already owns
  settling, merging a re-export, retaining the download as the recovery copy and
  quarantining what needs a look, and a second implementation of those rules
  living in a request handler is how they drift apart. So the page reports
  progress by reading the drop zone (`GET /api/drops`) rather than by being told.

  A ZIP no provider claims is refused with the reason rather than dropped — the
  watcher would only quarantine it seconds later, and the uploader still has the
  file. The name is reduced to a basename in a safe charset, and a collision
  numbers *before* the extension: the watcher scans only `.zip`, so a
  `foo.zip.1` would have sat in the drop zone forever, present and never looked at.

  This is the read surface's first write, so it carries its own guards. The Host
  check every request passes cannot see a cross-origin form post — the browser
  sends *this* server's name as Host — so a write additionally needs a loopback
  `Origin` and an `X-Archive-Upload` header, which no form can set without first
  winning a preflight the server never answers. The body streams in 1 MiB chunks
  and must leave a gigabyte of headroom behind it
  (`THREAD_ARCHIVE_UPLOAD_FREE_MARGIN`): filling the disk would break the very
  import the upload exists for. A body the router declines to read closes the
  connection rather than being drained — the unread remainder is a whole export.

- **A retrieval page in the viewer** (`/retrieval`, `GET /api/retrieval`). Every
  other view there is about the corpus; this one is about the pipeline that reads
  it. It assembles the three ledgers that record retrieval — served latency from
  `retrieval-usage.jsonl`, the controlled bench from `latency-runs.jsonl`, quality
  from `gold-runs.jsonl` — because none of them answers alone: latency without
  quality is half a verdict, since most of the cheap ways to make search faster are
  ways to make it worse.

  Three rules live in `_ops/retrieval_report.py` rather than in the caller, each
  because getting it wrong produces a plausible chart that is simply false. Probe
  queries are excluded (a one-character bench leftover returns in ~1 ms and pulls
  every percentile toward a number nobody experienced). Cold and warm are drawn as
  separate series and never averaged — measured on the live archive the same day,
  warm p50 is 104 ms against 7.5 s for a process's first search, so a blended median
  tracks the restart rate rather than the code. Query sets and pooled runs stay
  apart. Searches predating the process-age field are their own bucket and the page
  says how many, rather than folding them into whichever regime flatters.

  The charts are inline SVG on a **log** axis: these series span ~30 ms to ~30 s,
  and linearly every warm number is a flat line pinned to zero under one cold spike
  — the whole question lives in the bottom 2% of a linear chart. Restarts are on the
  page because they are the largest single influence on what agents feel.

  The window is in hours and the resolution follows it: 6h/24h/3d come back
  bucketed hourly, longer windows by day. A day is the coarsest thing a daily
  bucket can say, which is no help when a change lands at noon and the question is
  whether the afternoon is worse than the morning. Hourly labels are drawn on the
  operator's clock rather than the ledger's UTC — "when did it get slow" is a
  question about the wall in this room — while daily buckets keep their UTC date,
  since shifting a calendar day into local time would name a different day than the
  one it aggregates. The bucket span is **dense**: emitting only the buckets that
  saw traffic compresses the axis onto the times something happened, and a line
  drawn over that joins 3am to noon as though the hours between were steady.

- **`search_lab/latency_replay.py` — the speed bench over the queries agents actually
  ran.** Every existing instrument scores curated cases, and a gold case is mined to
  be *gradeable*: that selection excludes most of what real traffic looks like.
  Time-scoped asks, browse walks and sentence punctuation are all common in the
  usage ledger and near-absent from the golds, so today's scan and term changes read
  flat on `retrieval_gold_gate.py --latency` while moving real searches by an order
  of magnitude. The ledger now supplies the query set and `_ops.speed` supplies the
  controlled conditions.

  It replays real *calls*, not real query text — the parameters are part of the
  cost, and a recorded `group='browse', match='substring'` ask replayed as bare text
  at the default limit understates it 12×. `speed.measure` accordingly accepts
  `(query, kwargs)` beside a bare string, and a replayed `limit` overrides the
  bench's own so a walk's later pages keep the pools they had.

  It also prints the ledger's **served** distribution beside its own, because those
  diverged here by 20× while every bench read "fast" — the bench is warm with the
  pool cache off, production is whatever the serving process happened to be. A run
  that can only report the flattering half of that is the failure mode this exists
  to make un-ignorable. Latency rows and baselines are now tagged with the query set
  (`gold` / `observed`) and given separate baseline files: the two populations are
  not comparable, and one file would mean whichever set ran last defined the
  reference for both.

- **A gold run scored from persisted pools now says so.** `--cache` leaves the
  scores meaningful and the `p50_ms` meaningless — the arms never run, so a pooled
  run's median lands near 60 ms against ~900 ms for the same files uncached. The
  run record did not distinguish them, so the latency timeseries held two
  populations in one column and read as a tenfold speedup no code change caused.
  `pool_cache` marks the row, present-when-true like `overrides` beside it.

- **Every ledger record now says how old the process serving it was.** The ledger
  exists partly to catch latency regressions result-quality evals cannot see, and
  it could not: every cache retrieval leans on — the vector matrix, the embedding
  and cross-encoder models, the exact-set memo, SQLite's page cache — is
  process-local and starts empty, restarts are frequent (34 warm passes in one
  day), and nothing in a record distinguished a process's first search from its
  thousandth. Measured on the live archive the gap is an order of magnitude:
  `embed_ms` 5310 against 20, `knn_ms` 2368 against 25. Any before/after over the
  file was comparing cache states and calling the result a measurement.

  `uptime_s` rides in the contention context, so both surfaces and both tools pick
  it up. Recorded as a duration rather than a cold/warm verdict, for two reasons:
  the threshold for "cold" then belongs to whoever asks the question rather than to
  whoever wrote the field, and `at - uptime_s` is the process's start time — so it
  doubles as the process identity that groups a run's records together, which the
  file otherwise has no way to express. It is the one contention field always
  present: there is no reading of it that means *nothing to report*.

- **A ranking term no longer carries the punctuation prose hangs off a word.**
  `search_terms` stripped brackets and colons but not the comma, period, question
  mark or exclamation mark a natural-language query is written with, so
  `"losing my thread, waking up"` yielded the term `thread,`. That term matches
  *nothing*: the density pattern anchors both ends on word boundaries, and a
  boundary after a comma needs a word character beside it, so `thread,` finds
  `thread,x` and never the `thread,` of ordinary prose. A document holding the
  query verbatim scored 6 of 7 terms rather than 7 — quietly, under the count that
  decides `quality=strong` and the re-rank stand-down. It also slipped the word
  past the stopword filter: `this,` survived where `this` is dropped, putting a
  corpus-wide term into the ranking set and into the OR union that is the lexical
  arm's largest stage.

  Stripped from both ends only — interior punctuation is what makes `foo.bar` and
  `0.45` one term — and quoted spans are left verbatim, punctuation included. Over
  the ledger's real queries this removed every self-unmatchable term (12 across
  141 queries, 5% of which carried one). The gold gate holds and is otherwise
  flat (weighted MRR 0.7451 → 0.7476, nDCG unchanged): the curated cases are
  written without sentence punctuation, so they under-represent the shape this
  fixes.

- **The exact-set scan is now bounded for a scanning predicate, not just a matching
  one.** `SET_SCAN_CAP` counts rows that *matched*, which describes a `MATCH`'s work
  and not a `LIKE`'s: a `LIKE` has no index to walk, so it reads every row to find
  out whether it matched. Left at that the cost inverted — measured over this
  corpus, `LIKE '%the%'` hit the cap after 20k rows and cost 27 ms, while
  `LIKE '%zzqqxx%'` matched once, never engaged the cap, and read all 794k rows for
  316 ms. The *selective* query, which is what `match='substring'` exists for, was
  the expensive one, and its cost grew with the corpus without bound.

  `SET_EXAMINE_CAP` bounds it in the only currency that describes the work: rows
  examined, as a rowid window off the newest end. fts5 takes a rowid bound as a
  range constraint on the walk the ordering already uses, so the window truncates
  the scan rather than filtering its output (measured: 373 ms → 60 ms → 13 ms as
  the window narrows to 200k then 20k rows), and it truncates the old end, which is
  what the match cap already drops. When it engages the answer reports `capped`,
  degrading to the honest floor the exhaustive shape already knows how to serve. An
  unreadable watermark declines to bound the scan at all: slow is recoverable,
  silently truncated is not. At 2M rowids it does not engage on today's corpus — it
  is a ceiling on growth, not a haircut.

- **`thread_archive web` opens the viewer.** The read UI had a persistent URL and
  no front door: knowing it meant knowing `127.0.0.1:8787` by heart. The CLI
  deliberately refused a `web` verb because a verb that *served* the viewer would
  fork the one read surface into two — a second SQLite engine reading a store the
  watcher is writing. That rules out a server, not an opener, and this is the
  opener: print the URL, hand it to the browser, done. The pin in
  `test_public_api.py` still bars `search`/`read`; a `web` that served would be
  the thing it exists to catch.

- **The web viewer is a supported interface — the public API is four things, not
  three.** It was documented as private support machinery, which had stopped
  being true: lab's navbar links `/search`, the editor's "open in archive" button
  opens `/archive/<id>`, the patcher allowlists `127.0.0.1:8787` in a CSP, and
  the family manifest probes `/api/health`. Those URLs live in other repos'
  source, where archive's private-tree churn can't reach them, which is the same
  argument that makes the provider plugin API public.

  So the commitment is now stated and ratcheted: the page routes (`/`, `/search`,
  `/threads`, `/stats`, `/stats/model/<model>`, `/health`, `/archive/<thread_id>`)
  and two JSON endpoints — `/api/health` and `/api/archive-link`. Everything else
  under `/api/` backs the viewer's own bundle and stays private, as does the
  markup: the interface is the URL, not the DOM. Each half is pinned where it
  lives — the endpoints against the router in `test_public_api.py`, the page
  routes against `App.tsx`'s own route table in `e2e/route-coverage.spec.ts`,
  beside the bijection it already enforces between routes and browser cases. So
  dropping one is a deliberate act, not a silent break in a product that doesn't
  run these tests.

- **A time-scoped search no longer queries `events` at all — the vector pack carries
  its own dates.** With the redundant `agents` clause gone, what remained in the KNN
  scope mask was the time bound itself, and it was still the largest thing a
  `since`-scoped search did: measured on the live archive, `since='180d'` spent
  ~1.05 s of a ~1.4 s search materializing event ids.

  The mask was doing arithmetic on the wrong set. `since='180d'` selects **3.6M** ids
  out of `events` to narrow a pack holding **275k** vectors — 93% of the ids fetched
  name rows the matrix does not contain, and the cost scaled with the window
  precisely because of the ones that were never candidates. The pack already knows
  every row's date; it simply wasn't carrying it.

  So it carries it now: a `ts` array beside `ids` and `cts`, written by the base
  packer and stitched across the base+delta split like every other positional array.
  A window becomes a comparison over an array already in hand — **flat ~14 ms
  regardless of width**, against 94 ms at `7d` and 1117 ms at `180d`, with results
  byte-identical to the id-mask path across every query and window checked. The
  array costs 7.1 MB against the matrix's 845 MB, and `scope_ms` now reads 0.0 for a
  purely time-scoped search. Thread, source and path scopes still pay an id query —
  they need facts the pack does not hold — but the time clause is gone from that
  query too, and dropping it turns out to matter far more than the redundancy
  argument for it: a `source` scope carrying both clauses cost **~20 s** at every
  window measured, against **~1.1 s** for the same scope with the time bound left to
  the pack. It fetches three times as many ids and is eighteen times faster, because
  the two clauses together drove a plan neither one does alone.

  Two details that are load-bearing rather than incidental. The timestamps are
  stored as **bytes, not epoch integers**: the query being replaced compared the
  column as SQLite TEXT, which is a bytewise comparison, so comparing the same bytes
  gives the same answer for every value the column can hold — no parser, and no
  values a parser would reject or reinterpret. And a row with **no** timestamp is
  excluded from a window explicitly, not by ordering: it packs to empty bytes, which
  sort below every real stamp and would otherwise ride into every `until` bound on
  the grounds of being "before" it.

  Older packs simply lack the array. A pack is derived and disposable, so this is
  not a migration: the reuse check gates on the files a complete pack has, declines
  one that is missing any, and the next background build writes a whole one.

- **Redaction is removed.** `redact` / `unredact`, the keyring, `truth/redactions.jsonl`,
  the `_redacted` payload marker, and the `cryptography` dependency are all gone. The
  feature promised a lot of surface — crypto-shredding across truth, index, FTS,
  vectors, blobs, and citation quotes, with a three-state key lifecycle — and none of
  it was ever used: no archive this shipped to had ever minted a key or written a
  redaction record. What it cost was real, though: every reader carried marker-envelope
  vocabulary, the hash gates had a skip arm, amendment had a sealed-payload arm, and
  the backup's recovery bundle existed half to escrow keys.

  Consequences worth knowing:

  - The recovery bundle survives, minus the keyring: it carries `config.json`, the
    retained exports, and the health/ledger snapshots. The `backup.include_keyring`
    config key is gone, as are the `keyring_in_bundle` / `keyring_opted_out` result
    fields. Head-only still holds, now for its own reason — the bundle tracks the
    install's current state, so a member deleted at the home leaves the backup on
    the next run instead of persisting in dated snapshots.
  - `amend.check_patch` keeps the content-identity and non-object arms and loses the
    sealed-marker one. That made three of its call sites unreachable — the two
    pairing gates in the export-annotations backfill and the patch gate in the
    dropped-fields backfill all coerce the stored payload to a dict and filter
    content keys out of the patch before calling, so only a redaction marker could
    ever have made them fire. Those gates and their `pairing_refused` /
    `patch_refused` counters are removed rather than left as unreachable defense.
  - `_truth.blobs.collect_blob_hashes` is removed: shredding was its only caller.
  - `truth/redactions.jsonl` leaves the on-disk format spec. No migration — the file
    was only ever created by a redaction, so no existing archive has one, and the
    truth format version stays at 2.

- **The stage probe now covers the half of a search that happens after the pool.**
  The ledger attributed wall-clock to the two pool arms and the re-rank; everything
  the pool was then *put through* — ranking, the coherence pass, the thread fold,
  the browse shape's exact-set reconciliation, and the per-hit enrichments — was
  unmeasured. On the recorded traffic that blind spot read as roughly half of all
  search latency going somewhere nobody could name, which is exactly the condition
  under which a latency investigation invents a cause.

  `SHAPE_SUBSTAGES` (`rank_ms`, `coherence_ms`, `group_ms`, `extend_ms`,
  `enrich_ms`) closes it. Unlike the arms — which run concurrently and so can sum
  past the total — these run strictly in sequence and genuinely sum, to whatever a
  search spent past its pool. The split matters because the two halves scale with
  different things: an arm gets slower when the corpus or the index does, and these
  get slower when the *pool* is large, which a caller controls through `over` and
  `group`. `extend_ms` deliberately contains `set_ms` and the fold is billed apart
  from the reconciliation, so a slow exact set can never be read as a slow grouping
  pass. The bench (`_ops/speed.py`) reports the same stages production does.

  Three things the ledger said and the reading was wrong about, now that the
  stages are separable:

  - **Concurrency is not a factor** — and the sensor that should have said so was
    broken. Reconstructing call intervals from the ledger, 93% of recorded searches
    overlapped no other retrieval call at all, and the ones that did overlap were
    *faster* on average than the ones that didn't. But the `inflight` field had
    never fired in the ledger's whole history, including on calls that provably
    overlapped, for two independent reasons: it was sampled once at request start,
    so every peer arriving during a seven-second search was invisible to it, and
    the web surface sampled contention without ever entering the in-flight span, so
    its own load counted for nobody. A field that reports all-clear by construction
    is worse than no field, because an investigation reads it as evidence.

    `in_flight()` now yields a `Span` carrying a **high-water mark**: an arriving
    call raises the peak of everyone already running, and each reads its own peak
    on the way out, so a search that started alone and finished in a crowd reports
    the crowd. `peak_inflight(span)` is deliberately separate from `sample()` —
    the two are taken at opposite ends of the work and for opposite reasons, since
    a rebuild that finished mid-search still shaped it while a peer that arrived
    mid-search is only knowable at the end. Both MCP surfaces fold the peak in from
    their `finally`, so a search that *raised* still reports how busy the process
    was; the web surface now enters the span as well as sampling it.
  - **A concurrent writer is not a factor either.** Within one repeated query, a
    search running against a WAL written seconds ago and one running against a
    quiet database cost the same (mean exact-set scan 3.25 s against 3.45 s).
  - **The worst rows in the ledger were a stale daemon, not a slow pipeline.** The
    single largest attributed cost after the exact-set scan was hydration — 172 s,
    concentrated almost entirely in one three-minute browse walk whose candidate
    pool grew with page depth, reaching 33 000 candidates hydrated to serve 50 rows.
    The pool was made page-independent hours *before* that walk; the process serving
    it had never reloaded. Against current code the same walk holds a flat 244-row
    pool and hydrates in ~10 ms. Restarting a daemon after an edit is not
    housekeeping — it is the difference between measuring the system and measuring
    its ghost.

- **The gold miners no longer ship in the wheel.** `thread_archive._mine` is
  development machinery: the miners spend real tokens driving headless `claude`
  agents, and the cases they mint are only useful beside the scoring bench and
  gold files under `evals/` — which live in the repo and never shipped. So the
  install carried the engine without the bench it feeds, on a verb almost no
  user would run. Excluded from the wheel (`tool.hatch.build.targets.wheel`);
  the sdist keeps it, since the sdist ships the tests that cover it.

  `mine` stays registered in the parser — identical in both environments, so
  the CLI-verb ratchet stays deterministic — but it no longer carries a `help=`
  string, which is what keeps it out of `--help` where every listed verb should
  be one an install can actually run. Invoked anyway, it prints a pointer to the
  repo and exits 2 rather than raising ImportError; the availability check is
  `find_spec`, so a *broken* `_mine` in a checkout still raises its real error
  instead of being misreported as a missing one. Mining from a checkout is
  unchanged. `eval` — the read-only, token-free search self-checkup — is
  untouched and remains part of the product.

- **A `since` filter cost the vector arm seconds, and it was one redundant clause.**
  The ledger's slowest real searches all carried a time bound, and the cost scaled
  with the width of the window rather than with anything about the query: measured
  on the live archive, `since=7d` cost 580 ms, `since=60d` 4.6 s, `since=180d`
  11.6 s — with the lexical arm flat at ~80 ms throughout and essentially all of the
  rest in the vector arm's scope mask.

  The mask pre-restricts the KNN to in-scope event ids so ranking happens *within*
  the scope, and it carried the `agents='exclude'` filter alongside the time bound.
  That one clause is what made it expensive: a `thread_type` lookup per row turns an
  index-only range scan over `idx_events_occurred` into a table probe per matched
  event — 9.2 s against 0.9 s for a six-month window, with the row count identical
  either way. It was also redundant. Hydration re-applies the same filter, and that
  is already the *only* thing keeping agent threads out of an **unscoped** search,
  which builds no mask at all — so a time-scoped search was paying seconds for a
  guarantee the unscoped path gets for free.

  Dropped from the mask, kept at hydration. `agents='only'` keeps its clause, and
  the asymmetry is the reason: 'only' selects a small minority, so a corpus-wide
  top-k would be almost entirely rows the filter then discards, while 'exclude'
  removes a minority the other way and leaves the head unchanged. Measured
  end-to-end through the MCP server: `since=180d` 11.6 s → 1.33 s, `60d` 4.6 s →
  0.46 s, `30d` 1.55 s → 0.32 s. Gold gate unchanged on all seven floored files.

  What remains in that stage is the ~1 s it takes to materialize 3.6M ids and build
  the numpy mask, which no SQL change reaches. The fix for that one is to carry
  `occurred_at` in the vector pack so a time scope becomes a numpy comparison over
  an array already in RAM, with no id query at all.

- **Three latency findings off the retrieval-usage ledger, and one non-finding.**
  The ledger is the only record of what search costs an agent in practice, and its
  post-fix window read p50 352 ms / p90 4.2 s / p99 16 s — a distribution the gold
  gate's warm bench cannot see, because the expensive shapes are the ones no bench
  repeats. Three of them were addressable.

  *The two pool arms ran in series.* The lexical FTS pass and the vector pass share
  only the query and the scope — neither reads the other's output — yet the pool
  waited on their sum. The vector arm now runs on its own thread while the lexical
  arm stays on the caller's (which is where a caller-supplied session has to stay),
  and the pool waits on the slower of the two. The overlap is real rather than
  bookkeeping: both arms spend nearly all their time inside code that releases the
  GIL. Worth ~50 ms of a 630 ms search on the gold-query mix, which is inside that
  bench's own run-to-run noise, and rather more on real traffic, where the arms are
  closer in size (measured over the ledger's own query mix: p50 312 → 261 ms). The
  tail is the real target — the ledger holds searches whose vector arm ran 9 s and
  28 s beside a lexical arm that had long since finished.

  *A browse walk re-resolved the whole match set on every page.* `group='browse'`
  resolves its thread list from the exact set rather than from the ranked pool, and
  that scan does not depend on which page was asked for — so a caller paging to the
  end paid the identical scan once per page, and it was the largest stage of the
  walk. It is memoized now, keyed on the SQL it would run plus the index's append
  watermark, so ingest invalidates it rather than the memo hiding rows that arrived
  after it. Redaction and reindex drop it outright: a delete leaves the watermark
  where it was, and a rebuild re-mints the rowids. Measured over full walks on the
  live archive, 27–54% off the wall-clock, with the set scan falling from 0.1–1.9 s
  to ~2 ms. Freezing the set for the walk also makes the pages *more* coherent than
  re-resolving them did — they are sold as slices of one ordering, and a set
  re-resolved per page against a moving index can drop a row a later page was
  counting on.

  *The warm pass built the corpus graph before it primed search.* The graph is the
  longest of the four warm stages and the only one no search blocks on — the
  coherence re-rank serves whatever is cached and leaves the ranking alone when
  nothing is — so every query arriving in that window paid full cold-search latency
  for a stage it was not waiting for. It runs last now. Over the last twelve warm
  passes on this box, time-to-search-ready falls from a median 16.3 s to 10.4 s
  (worst case 80 s to 29 s).

  The non-finding is worth as much as the three. The **OR top-up tier** — the
  any-of-these-terms MATCH that runs when the strict pass leaves the pool short —
  is the single largest stage of a natural-language search, and its cost is set
  entirely by its commonest token: one corpus-wide word puts six figures of rows
  through bm25 to fill slots the strict pass declined. Ordering it by rowid instead
  of by rank runs 3–9× faster and looked like free money. It is not: it puts
  findability and judged-cases below floor, the vague shape hardest. Pruning the
  high-document-frequency terms out of the union is not order-preserving either —
  it changes 10–85% of the pool's own top 20. That breadth *is* the recall for the
  queries the tier exists to serve. Both dead ends are recorded at the tier itself,
  so the next attempt starts past them.

  One consequence for anyone reading a stage breakdown: `fts_ms` and `semantic_ms`
  now cover overlapping wall-clock and no longer sum toward the total. They are
  durations, not shares — which is the point of recording them apart, since what a
  search waits on is the slower of the two.

- **The claude-code parser was marked degraded for shipping releases.** The
  version tripwire records a first-sighted harness version to the validation-drift
  ledger — deliberately, since format changes ride version bumps and the sighting
  names the release when a field later drifts. But the coverage check counted
  those advisories as drift volume, and Claude Code ships a release most days:
  2.1.218, 2.1.219 and 2.1.220 landed inside a week, cleared the
  three-records-in-seven-days threshold, and pinned the source as
  `validation_drift` degraded with a parser that had nothing wrong with it. The
  real findings the ledger held before — `user.toolEndsTurn`, `assistant.agentId`,
  the `permission-mode` line type — had all been fixed already. The verdict was
  self-sustaining: the tripwire refills the window faster than it drains, so the
  source could never return to healthy, and every genuine drift arriving later
  would land on a board that already read degraded. It also kept the drift-snapshot
  quarantine armed against a non-problem.

  Advisory records are now marked as such at the point of writing and held out of
  `recent_substantive`, which is what the coverage warning and the degradation
  verdict key on — the same split the capture-skip ledger already drew for routine
  empty-session skips. The records stay in the trail and in `recent`; the ledger is
  still the place to look for which release grew a field. A record carrying no flag
  is classified by its findings, since the ledger is append-only and outlives any
  one writer, and an unrecognized record reads as drift rather than as noise.

- **The latency smoke test was built to fail at random.** It measures the corpus's
  eight *slowest* queries — cherry-picked from the baseline precisely because they
  are pathological — and then held the result against a ceiling of 1.5× the
  **corpus-wide** p95, a number summarizing all 138 queries including the fast ones.
  The two numbers are incomparable, and not by a little: on a freshly recorded
  baseline the bar comes out at 2326 ms while the slowest query in the smoke set
  cost 2491 ms *on the very run that recorded it*. The ceiling sat 165 ms below the
  baseline's own measurement, so the check was not merely noisy — it was
  self-contradicting, failing unchanged code against a reference taken from that
  same code. Two runs duly disagreed, one passing and one failing by 65%, and the
  failing one cost an investigation into a ranking change measured at ~1% slower.

  The ceiling now references the queries actually being measured — the slowest of
  their own recorded timings, which is what a p95 over their samples approximates —
  and falls back to the corpus-wide p95 only for a caller measuring the whole set.
  Same instrument, same impatience (it is still a heuristic, not a sound bound); it
  now fails on a change rather than on the weather.

- **The ranker threw away both of its arms' scores and ranked on their rank.**
  The lexical arm's contribution was a positional proxy — a hit's reciprocal rank
  within the pool — on the stated reasoning that "FTS5 orders by bm25 but does not
  surface the score." FTS5 does surface it: the hidden `rank` column reads as an
  ordinary output column, is NULL rather than an error on a non-MATCH pass, and
  leaves the query plan untouched (still the streaming rank-sort, `INDEX …:M`,
  measured at parity), so the score was one SELECT-list entry away the whole time.
  The vector arm's cosine was already on every hit, carried through fusion as
  provenance and never scored. What the ranker saw of either arm was therefore an
  *ordinal*: at `rrf_k` 60 the proxy spans 1.00 down to 0.23 across a 200-deep pool,
  a gradient flat enough to nudge and never to decide, and RRF cannot tell a 0.72
  cosine from a 0.55 one.

  Both magnitudes are now weighted signals — `bm25_score_weight` 100 and
  `semantic_weight` 200 — pool-normalized so the weights read against a fixed scale.
  The cosine is spread min-max across the pool rather than used raw, which is worth
  roughly three times as much: unspread it is mostly a constant offset, and the
  content-type multiplier scales the whole sum, so a flat semantic term would have
  amplified content-type preference instead of relevance. Over all 25 mined gold
  files (317 cases, scored on their frozen snapshot), pooled recall@10 rises
  0.5331 → 0.5434 and nDCG@10 0.6124 → 0.6263; on the three protocol files held out
  from the tuning, +0.020 recall@10 / +0.025 nDCG@10. Findability's hardest strata
  are where it lands — vague recall@10 0.850 → 0.900, paraphrase 0.909 → 0.955,
  verbatim already saturated — which is the shape a dense magnitude should buy: the
  queries whose wording the lexical arm cannot match. Two of the seven floored files
  give a little back (frustration −0.047 recall@10, needle −0.019, each inside one
  case of that file's resolution); every floor holds.

  The score costs about 1% of a search. It rides a SELECT-list column FTS5 already
  computed for its sort, but "already computed" is not the same as free: measured per
  pass over the corpus's eight pathological queries, adding it costs +158 ms across
  them on the strict AND pass (+28% of that pass, which is cheap at ~70 ms/query) and
  −67 ms on the broad OR fallback (−1.1%, the pass that dominates at ~790 ms/query) —
  so ~20 ms on a ~1600 ms query. End-to-end agrees: alternating the configurations
  inside one process gives median p50 1641 ms against 1610 ms.

  Both halves of that need stating, because the obvious experiment cannot see the
  first one. Scoring the shipped weights against zeroed weights holds the *code*
  fixed — the column is still selected, the normalization still runs — so it prices
  the weights and nothing else. Only removing the column from the SQL prices the
  column.

- **A thread's ranking ignored how much of it matched.** Every ranking signal
  scores one event, and grouping then represents a thread by its best one, so a
  conversation that returns to a subject twenty times ranked exactly like one that
  mentioned it once — on whichever event happened to score highest.
  `thread_evidence_weight` weighs the pool's distinct matches per thread, log-damped,
  and it is the largest lever measured on subject-shaped queries: +0.022 nDCG@10 and
  +0.017 recall@10 across the 22 `topic` gold files, where the standing headroom is.

  It ships **off**, because of what buys that. Evidence favours the thread that
  keeps returning to a subject over the one that settles it in a single exchange, so
  a broad query whose answer is one specific conversation loses it: on the
  recall-capable `judged` file, "how can we improve thread_search" falls from rank 1
  to past 20, and it falls at every weight down to 25. Log damping bounds how far a
  chatty thread climbs, not whether it climbs past a single-mention answer. The seam
  that would earn the signal is a query-shape gate — the subject-shaped queries it
  helps are the ones `rank.should_rerank` already classifies — and until that exists
  the hold-out's verdict stands. Its cost is skipped entirely at weight 0, so the
  disabled knob is free per search.

- **Two fifths of the corpus was outside the truth re-emit's content gate.**
  `rebuild_truth_from_store` is the one operation that overwrites truth from the
  index, and its content pre-flight validated each payload against the hash tail
  in its own `dedup_key`. That is unavailable to a row with no key — 1,598,464
  events of 3,955,358 on the development archive (`api_request_started` 803k,
  `tool_execution_completed` 249k, the thinking/text deltas 333k, `progress`
  128k). Those rows were checked for *existence* by the containment pre-flight
  and never for *content*, so rot in one would be written over the good truth
  line, destroying the redundant copy that proves it — the same laundering
  `_content_divergence` already names in the reindex direction, but permanent,
  since a re-emit leaves nothing to compare against afterward.

  A fourth pre-flight (`_unkeyed_store_rows_diverging_from_truth`) now compares
  the unkeyed rows against their truth lines by canonical payload fingerprint —
  the comparator is shared with `verify --hashes` so the two gates can't drift.
  Scoped to unkeyed rows deliberately: a *keyed* row that disagrees with its
  truth line while passing its own key hash means the truth line rotted, and the
  re-emit is the repair, so blocking there would refuse the operation in the one
  case it fixes. `force=True` overrides, as with the other pre-flights.

  This moves detection for the unkeyed half from "whenever the 30-day
  `verify --hashes` tier next runs" to "at the moment the destructive operation
  runs." The cadence is unchanged; it is early warning, not the last line.

- **The backup mirror's rot scan needed two age gates to coincide.** The scan
  sits behind `hashes` *and* a non-None `backup`, and the nightly passed the
  destination only when the 7-day `deep` gate was due — so on a night the 30-day
  `hashes` gate came due alone, the live stores were re-hashed and the mirror was
  skipped, with the hashes clock reset either way. Expected interval for the
  mirror's only content check was therefore ~7× its nominal one. Either
  escalation now pulls the mirror in.

  This matters because the mirror is the fallback copy, and the check it was
  missing is the one that sees rot at rest: an unchanged destination file is
  never re-copied (size+mtime skip), and the parse-and-count scan stays green on
  a payload that rotted into still-valid JSON. The nightly restore drill proves
  the mirror parses and reconstructs; it does not re-hash payloads.

- **The dedup-key backfill could not reach a single account export.** Its scope
  was "threads present in `import_state`", on the reasoning that `dedup_key`
  exists for import idempotence and only watermarked threads are re-imported.
  Export importers never write a watermark — they record provenance on
  `Thread.source_id` — so the scope excluded every ChatGPT, claude.ai and xAI
  thread outright. Those are the threads that get re-imported *most*: an account
  export is cumulative, so every fresh download re-delivers the entire history
  and dedup is the only thing standing between that and a doubled conversation.

  Scope is now either marker — a watermark or a `source_id`. Live-captured
  threads (neither) still stay out, since their event structure differs from the
  importer's and the recompute is not validated against it. On the development
  archive this moves ChatGPT from 100% NULL keys to 0.4%, claude.ai to 0%, and
  Cursor to 0.1%; 1,115,535 keys backfilled across 3,947 threads with zero
  warnings and zero plan errors.

  Finishing such a pass with `rebuild_truth_from_store()` needs `force=True`, and
  the reason is worth knowing before anyone reaches for it: the containment
  pre-flight keys a truth unit on `dedup_key` *falling back to the event id*, so
  a backfilled row is an id-unit in the truth and a key-unit in the store, and
  every backfilled event reads as missing. The gate's count should equal the
  backfill count exactly — anything above it is a real gap, not the shift.

- **Nothing updates itself any more.** The watcher's hourly release probe, the
  once-a-day check it spawned, the `update.auto_apply` opt-in to unattended
  apply, and the `update.enabled` / `update.check_interval_hours` switches that
  configured all of it are gone. `thread_archive self-update` is the whole
  mechanism now — the operator runs it, `--check` reports without touching the
  clone, and `update.remote` is the only knob left.

  The 48-hour soak window went with them. It existed so a bad release could be
  yanked before an unattended install took it; with no unattended install to
  protect, it was only a delay between pushing a tag and being able to apply it,
  and the plan's tag-by-tag walk (skip the young ones, maybe stop at an older
  matured tag) collapses to "the newest release tag." Every guardrail on the
  explicit operation stays: clean tree, fast-forward only, the truth-format gate
  behind `--allow-format-bump`, smoke-check and rollback, patch retirement.

- **Thread ids minted in the same millisecond sorted arbitrarily.** A ULID's
  timestamp orders ids *between* milliseconds; within one, order came from 80
  freshly-random bits, so two threads created in the same millisecond could sort
  either way and `ORDER BY id` — the property the id format exists to provide —
  silently stopped being creation order at that resolution. A clock stepped
  backwards by NTP or a suspend/resume was the same bug over a wider window: ids
  that sort before ones already handed out.

  Minting for *now* now carries the previous id's random field forward and
  increments it whenever the clock has not advanced past the last mint, which
  covers both cases with one rule. The carry is locked (the watcher imports on a
  threadpool) and dropped on a pid change, so a fork can't leave two processes
  incrementing from the same value. Minting with an explicit timestamp — import
  backfill, migration — stays outside the sequence in both directions: it neither
  consults nor advances the carry, so historical or bad future-dated timestamps
  cannot drag live minting with them.

- **Tool output is no longer indexed, and `thread_search` reads the whole
  transcript by default.** These are one change: the narrow default scope existed
  because the index was mostly machine output, and once that output is gone the
  reason for the narrowness goes with it.

  Tool results and tool errors were 1.72 of the 2.06 GB indexed and 36% of the
  documents — a grep dump or a re-read file putting thousands of incidental term
  occurrences behind whichever conversation happened to run the command, so a
  query matched the machine's words rather than anyone's. They are extracted as
  before and preserved in full; `thread_read` still replays them. They simply do
  not reach the index, filtered at the one seam every writer and `verify` share,
  so no path can disagree about what should be there. Tool *calls* stay indexed:
  they are 0.15 GB, they carry the tool name and its arguments, and excluding them
  too measured worse on every metric.

  The default scope was `('user', 'title')` — 4% of the corpus — which did not
  make the lexical arm cheaper (FTS5 scores its whole match list whether or not a
  content-type filter follows), left the candidate pool short so the fallback
  ladder fired on nearly every query, and then tripped the one-shot widen that
  ran the entire search a second time. Measured on 120 real search→read pairs
  from the usage ledger: the retry fired 56 times and was adopted 40. The scope is
  now every indexed type except the librarian's derived summaries, which stay
  opt-in, and the widen retry and its note are gone — there is nothing left to
  widen to.

  Both eval protocols moved the right way. Against real usage: MRR 0.3195 →
  0.3190, nDCG@10 0.3196 → 0.3229, recall@10 0.4579 → 0.4618, at 2.15x the speed.
  Against title recall: MRR 0.9461 → 0.9513, success@1 0.925 → 0.933, nDCG@10
  0.9534 → 0.9591, at 1.33x. Dropping the scope alone (keeping tool output
  indexed) was faster still and *lost* MRR on both — the exclusion is what pays
  for the widening, which is why the two ship together.

  The comparison needed a harness that did not exist: `eval` scores `api.search`
  with the wide scope, so it had never exercised the narrow-then-widen path an
  agent actually calls. The warm pass now shares one scope constant with the
  surface, since the vector matrix caches per content-type scope and a drift
  between them leaves the first real query building a matrix inside the request.

- **The MCP tool's own guards were the untested half of search.** `search()` is
  covered exhaustively; `thread_search` — the surface an agent actually calls —
  had a layer of logic above it that no test reached. It bounds what a caller can
  ask for (`limit` to [1, 500], `page` to [1, 200], `context_lines` to ±50) because
  each one multiplies work inside the engine and the engine takes them at face
  value; none of the three clamps had a test, and `page` had never been passed
  through the tool at all. Nor had `tool_name` or `until`. The clamps are now
  pinned against the usage ledger, which records the parameters a search *actually
  ran with* — the one place a bound that never reached the engine is observable
  without reaching inside the call. Two rejections joined them: an unusable `match`
  mode is answered rather than raised (the caller is a model, and an MCP exception
  is a failed tool call it has to guess its way out of), and a commit whose
  authorship walk hit its bound now provably says so — past that bound there is no
  floor to find, so every prior edit gets credited and the list is knowably too
  generous. Silence there is indistinguishable from a list that is exactly right.

- **A NUL byte or a lone surrogate in a query raised out of search; generated
  queries now hold the never-raise contract.** The pipeline's answer to a
  malformed query is supposed to be results or no results, never a raw FTS5
  syntax error — and every malformed shape demotes to a fully-quoted literal to
  guarantee it. Two code points defeated that demotion. NUL is C's string
  terminator, so it truncates a bound parameter mid-token: SQLite reported
  `unterminated string`, and because the NUL survived into the quoted phrase, the
  retry-quoted fallback the arm keeps for exactly this case raised too. A lone
  surrogate isn't encodable to UTF-8 at all, so the driver raised before SQLite
  saw the statement. Both ride in freely over MCP, where a JSON query string may
  carry `\u0000` or an unpaired `\ud800`. Each builder now drops them immediately
  before its text becomes a bound param, so every entry point into the lexical arm
  inherits the guard.

  Found by the thing that shipped alongside the fix: `tests/test_search_fuzz.py`
  puts generated queries through the contract instead of a list of shapes someone
  thought of. It runs through `search_events` rather than any one builder, because
  `to_match_query` is only one of four MATCH expressions the classifier picks
  between — a fuzzer pointed at it alone would leave the pipe-OR join, the
  code-mode phrase, and the identifier-token fallbacks unexercised. Every property
  ends in real SQL: a builder that returns a string without raising has proven
  nothing when the failure mode is FTS5 rejecting that string. The LIKE scans get
  the same treatment from the other side — `matched == (needle in haystack)` in
  both directions is the whole specification of "matches literally", and the
  reverse direction is the one that bites, since an unescaped `_` makes
  `get_session` match `getXsession` and reads as a hit rather than as a bug.
  Derandomized, so a red names the change under test rather than the dice.

- **Search can be used to enumerate, not just to find: `page=`, honest totals,
  and `match='substring'`.** A result was a cut with nothing naming the whole, so
  ten rows read identically whether they were all of them or ten of nine hundred
  — which made "list every thread that mentions X" unanswerable, and worse,
  unanswerable *silently*. Three changes close it.

  `page=N` walks the set, and every page is a slice of one ordering, so a walk
  neither repeats nor skips a row. Getting that right meant making *nothing* that
  shapes the order depend on which page was asked for — not the candidate pool's
  depth, not the cross-encoder's head. Sizing the pool from `page × limit` (the
  obvious move, since a deeper page needs deeper candidates) is precisely the bug:
  the coherence re-rank scores a thread's community against the pool's mass, so a
  pool that grew per page handed each page a differently-ordered list. Measured
  before the fix, a 721-thread walk served 122 rows twice and skipped as many.
  One pool per `(query, limit)` fixes it; what it costs is depth, which is why the
  ranked shapes report their totals as floors and `group='browse'` — unbounded,
  since the exact set supplies whatever the pool never reached — is the shape to
  enumerate with. The MCP layer stops widening the content-type scope past page 1
  for the same class of reason: the widen decides on the top hit, a different hit
  on every page, so a walk could widen at page 3 and narrow again at page 4,
  silently interleaving two corpora.

  Results now carry the size of the set they are a page of. Where the pool held
  every match, that is a real total; where it was cut, the header says `of ≥N ·
  truncated` rather than passing a reach off as a total. `group='browse'` is the
  shape that enumerates completely: its thread list is resolved from the whole
  match set (one capped `GROUP BY` over the same predicate and filters as the
  pool) instead of being cut from the ranked pool, so paging it to the end reaches
  every matched thread — including those ranked past the pool boundary, which were
  never ranked low, only absent. Ranked order still leads; the threads no ranking
  pass ever scored follow by recency.

  In that shape the match set is authoritative and the pool supplies only order,
  which cuts both ways: threads the pool never reached are appended, and threads
  the pool held that are *not* in the set are dropped. The pool is federated, so
  the vector arm contributes semantic neighbours that need not contain the query
  at all — a relevance aid, not set membership. Left in, they made the total climb
  as a caller paged through it (721 → 1280 across fifteen pages), which is worse
  than reporting no total. Whether the pool saturated is still tracked, on its
  *raw* reach rather than its length: dedup shrinks the list, so length would call
  a saturated pool short and report a cut as complete.

  `match='substring'` is the opt-in that reaches within-token matches no index can
  see. `p4` as a token finds `p4`; as a substring it also finds `mp4`, `p400`,
  `gcp4` — on this corpus 1937 threads against 894, more than double. The infix
  scan already existed but only as a *fallback*, gated to identifier-shaped
  queries and capped to the most recent 25k rows, which reached under a fifth of
  the real set. That cap is right for a scan nobody asked for and wrong as a
  ceiling on an explicit request, so an explicit `match='substring'` lifts it —
  the same rule the search blacklist and the `agents` filter already follow. It
  costs a full-table scan (~1.5s CPU plus IO over ~4M documents) and runs alone,
  with no fallback ladder to widen past what was asked for.

  Two measurements shaped the implementation. Exact totals are not affordable on
  every search — counting a 550k-match term exactly costs ~6.5s — so the set scan
  is capped at 20k rows and a total that hit the cap renders as a floor (`N+`).
  And the substring pool pass orders by rowid, not `occurred_at`: the latter is
  UNINDEXED, so sorting by it materialized and sorted the whole match list (~14s
  where the scan alone is ~1s).

- **Search quality is measured by how full the window is, not by where the first
  hit lands.** Agents do not read a ranking; they fire several searches carrying
  terms that surround what they want, dedupe by hand, and read around whatever
  looks worth opening. MRR scores the wrong thing for that, and it was hiding the
  actual problem: `evals/window_fill.py` scores the share of the window's relevant
  capacity that relevant threads occupy (ceiling-normalized, because raw recall@k
  on a multi-answer case scores gold-set size as much as ranking), plus **union
  coverage** — fire every query a topic carries, union the windows, and measure how
  much of the subject was assembled. The stack leads BM25 on every topic, but the
  absolute union coverage says a third of the relevant material comes back. That is
  a recall ceiling no reordering reaches, and it is invisible to MRR: files scoring
  0.9 MRR surface a quarter of what is relevant.

- **Scoring builds the corpus graph before its first case, on every path.** A
  scoring loop races the coherence re-rank's background graph build: it lands
  partway through, cases before it rank without coherence and cases after it with,
  and the boundary moves with wall-clock — so two runs of identical code over one
  frozen snapshot disagree by ~0.02 window fill on a file, concentrated in whatever
  ran first. The gold gate already built the graph inline; `retrieval_eval.py`, the
  shipped `thread_archive eval`, and anything else going through
  `_eval.evaluate` did not. That build is now `_eval.warm_for_scoring`, called by
  the scorer itself, with the gate's copy delegating to it so there is one
  implementation and one docstring explaining why.

- **The archive gold set covers 22 topics.** Mined against the same frozen snapshot
  the existing files bind to, so every case scores together: 317 cases across 25
  files (64 `querygen`, 21 `query`, 19 `rerank`, 213 `topic`). Topics were chosen
  for subject coverage and for sitting inside families of near-synonyms, where the
  hard negatives are real neighbouring conversations rather than synthesized ones —
  the two family picks (both project-name topics) produced the widest margins.
  Selecting instead by how lexically separable a topic's title is does not predict
  anything and should not be used: across 21 topics the correlation with the stack's
  margin is −0.33, not significant, and it changed sign between batches.

- **The code axis: `path=` and `commit=` scopes on search, and a files view on
  read.** The archive claims to be a richer record of what your agents did than git
  alone, but it could not answer the most ordinary question about that record —
  *which conversations edited this file* — except by text-searching for a path and
  hoping the session spelled it the same way. Every path was already there, in the
  tool calls: an `Edit`'s `file_path`, an `apply_patch` header, a `Read`'s
  `target_file`, a shell command's arguments. They are now folded into `event_paths`
  / `event_commits` — disposable projections of the event log, cursor-folded like
  the metrics rollup, so an existing archive backfills itself (~314k rows over a
  3.9M-event corpus, in under a minute) and a reindex rebuilds them. Provider
  spellings collapse at extraction: `Edit`, `search_replace`, and `edit_file` are
  one op, and paths are normalized against the session's working directory —
  including the `cd` inside a shell command, which is where a relative path in a
  `Bash` call usually actually resolves.

  It rides the tools that already exist rather than a third one, because the two
  questions it answers are the two shapes `thread_search` already had: an empty
  query lists conversations, a query searches inside them. `path=` (bare name,
  partial path, absolute file, directory subtree — which is how you ask about a repo
  — or glob, narrowed by `path_ops`) makes the empty-query browse the full "who
  worked on this file" answer: ordered changes-before-looks, each row carrying its
  op tally, the window of touches, and an `event_id` re-pointed at the strongest
  touch so opening it lands on the edit rather than the session's tail. `commit=`
  closes the loop from `git blame`, and resolves to every session the commit is
  **made of** rather than to one author: a commit carries work from several sittings,
  so the scope is the sessions whose edits fall inside its *authorship window* —
  after each of its files was last committed (one `git log --name-only` walk supplies
  the per-file floor), up to the commit itself. Without that floor a file edited
  across a year of sessions would credit every one of them to whichever commit
  happened to touch it. The session that ran `git commit` is flagged among the
  contributors rather than substituted for them — wherever a human commits out of
  band it is nobody, and where an agent commits it is usually just the session that
  typed the command. Each session's share of the commit, which of them ran it, and
  the fact that file overlap is evidence rather than proof ride as a note above the
  rows, since the rows themselves are ordinary thread rows. `thread_read(thread_id, summary='files')`
  is the same index backwards. `thread_archive status` reports the projection's
  counts and its trailing edge.

- **Search no longer reads stored summaries unless the caller names them.** The
  agent default scope searched user messages, thread titles, *and* stored thread
  summaries — but summaries are the librarian's derived text, not the record, so
  a hit could land on a paraphrase the conversation never said, and findability
  quietly depended on how much of the corpus the gardener had summarized. The
  default scope is now user+title, and the dry-scope auto-widen — which clears
  the content-type filter to everything — now excludes summary docs too, so no
  scope the caller didn't name reads them. ``content_type='summary'`` targets
  them and ``content_type='all'`` still searches truly everything. The warm
  search primes the new scope key.

- **A passing run says so again.** `addopts` carried `-q`, and pytest's verbosity
  is cumulative — so the `-q` every caller types (the README's own run line, both
  install guides, the two ci.toml pytest rows) stacked to `-qq`, where pytest stops
  printing the `N passed in Xs` counts line. A green suite emitted a field of dots
  and nothing else, and the standard verification — `pytest … | grep -E
  'passed|failed'` — came back *empty*, byte-identical to a command that never ran.
  Readers who couldn't tell green from broken re-ran the suite repeatedly, chasing a
  line the config had suppressed. Quiet is the caller's to pass, not the config's:
  `addopts` no longer sets it, and `tests/meta/test_output_summary.py` spawns a real
  `-q` run against the shipped config to prove the counts line survives.
- **Semantic hydration stopped joining `events` for a column it already had.**
  `hydrate_ms` was the biggest single stage of a settled warm search (137–177ms,
  ~80% of the warm vector arm), and roughly half of it was one join. Turning KNN
  candidates back into hits selected `e.occurred_at` through `events_fts f JOIN
  events e ON e.id = f.event_id` — a scattered rowid lookup into a 3.9M-row table
  per candidate, and the candidate list is three times the pool wide (594 lookups
  for a 198-hit pool) — while the shadow row carries its own `occurred_at` in step
  with `events`, and the lexical arm was already reading it there. Over eight
  first-touch 594-id pools the join costs a median 121.4ms against 67.2ms without
  it. The `since`/`until` bounds move to the shadow column with it, so the two arms
  now date and window a hit identically.
- **The lexical arm now says which pass spent the time.** `fts_ms` is the largest
  stage of a settled warm search (486–838ms against the vector arm's 406–529ms) and
  it covered the whole ladder as one number: an indexed FTS5 MATCH, the token-AND and
  token-OR passes behind it, a full-table substring LIKE, a duplicate-flood re-gather,
  plus hydrating every returned row into a hit. Those differ by orders of magnitude,
  so the total named the arm and nothing else — the same problem already fixed on the
  vector arm, sitting on the arm that now costs more. Searches record `match_ms`,
  `scan_ms`, `rescan_ms`, `build_ms` and a `fts_passes` count beside the total, so a
  slow arm distinguishes a slow index from a ladder that walked all the way down to
  the scan. The bench reports the same split.
- **Startup built the corpus graph twice, concurrently.** The build is the largest
  single cost in a warm pass — 15s on a 8,961-thread corpus, more than the embedding
  model and the cross-encoder together — and it had two entry points with only one
  guard between them. `_refresh_async` prevents a second background refresh, but
  `get(block=True)` builds on the calling thread and was covered by nothing, so the
  warm pass and the first search that arrived during it ran the identical
  corpus-wide Leiden partition side by side, doubling both CPU and peak memory at
  the moment the process is already loading two models. `build()` is now
  single-flight per engine: the loser waits on the winner and takes its result,
  which costs it nothing (it was going to wait out a build regardless) and makes the
  second build unnecessary rather than merely serialized. `graph_ms` in the `warm`
  ledger rows ranged 13.7s–37.4s across four restarts; the spread was this.
- **The embedder no longer renders a progress bar into the daemon log.** `encode`
  left `show_progress_bar` at its default, so every batch wrote a multi-KB line of
  carriage returns to `watcher-stderr.log` — 600-odd of them in the last 20k lines,
  read by nobody, since every caller here is a daemon or a library call. The re-rank
  path already passed `show_progress_bar=False`; embed now matches it.
- **`is_refreshing()` now reports the inline build too.** It backed the `refreshing`
  contention field but read the background-thread guard, so a search colliding with
  the warm pass's build — the worst-timed collision there is, and the one that
  actually happens on every startup — recorded no contention at all.

- **The thread-meta sync scanned the whole FTS shadow to find 1% of it.** The
  maintenance pass reads every thread-meta doc out of `events_fts` to diff titles and
  summaries, and `event_type` had no index — so finding ~11.9k rows meant scanning all
  1.2M, every pass, growing with the corpus rather than with what changed. Adding
  `idx_events_fts_event_type` (mirroring `idx_events_type` on `events`) takes that
  query from 1659ms to 23ms and the whole sync from 1854ms to 130ms — 14x. Plain
  rather than partial deliberately: the reader binds the event type as a parameter,
  and SQLite cannot match a partial index's predicate against a bound value, so a
  partial index would be built and then never used. `verify` reports it missing and
  `reindex` heals it, like every other declared index.

- **The embed cohost was the one drain that could fall behind silently.** `lag_s`
  covers lexical freshness; nothing covered the vector arm. If the cohost stalls,
  every other signal stays green — the poll loop is healthy, `lag_s` is low, searches
  return hits — and the only symptom is that the *right* hit is missing, because the
  conversation was never embedded. Each pass now records into `watch_embed_last`:
  docs embedded, wall time, docs still pending (with `capped` when the real backlog
  exceeds one batch — a stalled drain and a caught-up one both embed zero, and only
  that distinguishes them), chunks pending, and the newest embedded event's age.
  The drain already reported a `select` / `model_load` / `encode` / `write` split
  into a phase and the cohost was discarding it; `CollectingPhase` keeps those
  timings in memory without writing a ledger row, so the steady path gets the same
  breakdown a tracked load does.
- **`maintain()` is timed.** It is the interval-gated upkeep whose two halves — the
  manifest checkpoint and the shard rebalance — scale with the archive rather than
  with what just arrived, the shape that became a quadratic term once already.
  Gating bounds how often that is paid, not how much. `watch_maintain_last` records
  the total split across `checkpoint_ms` and `thread_meta_ms`, so a regression names
  its half instead of surfacing as the poll loop mysteriously slowing down.
- **Latency records carry what else was running.** Every timing so far was a bare
  duration with no way to tell a slow pipeline from a busy machine. Searches and
  reads now sample contention at the start of their work: `inflight` (concurrent
  calls in this process), `refreshing` (background matrix/graph rebuilds, which
  stream the pack off disk and run for seconds), and `wal_age_s` — seconds since
  anything last wrote the index, read off the SQLite WAL's mtime. That last one is
  the cross-process signal: reads never touch the WAL, so a fresh one means the
  watcher or an import is writing the database this search is reading, and it makes
  the retrieval ledger joinable to the ingest side with no coordination between them.
  Fields are omitted when they say nothing, so an idle-machine call records none.

- **An archive opened just after boot is registered.** The registry's per-process
  throttle read a missing entry as "last registered at monotonic 0.0". `time.monotonic`
  has no defined epoch and counts from boot on Linux, so on a machine up less than the
  five-minute interval every *first* registration was silently swallowed — an archive
  could be opened repeatedly and never become known. A missing entry now means never
  registered. Caught as two red tests on CI's fresh runners that no long-uptime dev box
  could reproduce; pinned by a test that fakes three seconds of uptime.
- **The browser suite mocks `/api/archives`.** The health view gained the registry
  fetch; the e2e API surface did not, so `/health` failed its own no-unhandled-request
  assertion — the check working exactly as designed.
- **The slowdown trend is measured in work, not items.** Items per second is only a
  proxy for cost, and it is a bad one wherever items differ in size and the phase
  orders them: the embed drain length-sorts on purpose, so its final window holds the
  longest documents in the corpus and its rate collapses for a reason that is not cost
  growth. A real 36-minute embed reported `slowing 122x` — true arithmetic, useless as
  a signal, and it would have fired on every embed. A phase can now name the unit its
  items are made of (`work_unit="chunks"` for the embed) and report it through
  `advance(n, work=...)`; the trend uses that unit and the snapshot says which one it
  used. Phases with no sub-item unit and no deliberate ordering — import — keep
  trending on item count, which is where the signal is real.
- **The haystack benchmarks are archives you can open.** LoCoMo and LongMemEval are
  scored per question — each question retrieves inside its own small history — so the
  harness built a throwaway home per question and the corpus existed only as hundreds
  of fingerprint-named micro homes: not openable, not searchable, not visible anywhere
  archives are listed. `evals/haystack_corpus.py` builds each dataset as one ordinary
  home beside `homes/cdr` and `homes/swe-chat` (locomo one thread per turn, ids
  namespaced by conversation since `dia_id` restarts at `D1:1` in each; longmemeval one
  thread per haystack session, deduped across the shared pool), tracked like any load
  and tagged `benchmark`. The per-question homes stay unregistered workspace.
- **Building a snapshot is a tracked load.** A snapshot of a real corpus runs for tens
  of minutes and only the index phase — `reindex`'s own run — was recorded, so the copy
  that opens it was a silent stretch with nothing to watch and no record afterward. The
  build now writes a `snapshot` run into the home it builds, phased `copy` / `index` /
  `verify`, with the copy's file and byte counts. `reindex` still keeps its finer-grained
  record inside the index phase; the ledger holds both.
- **The vector arm's latency is now attributable.** `semantic_ms` covered five
  unrelated costs — the query embedding, the scope-mask query, serving the KNN
  matrix, the matvec, and hydration — so a 28-second observation named an arm and
  nothing else, which is precisely useless in the tail where the value is. The probe
  now reports `embed_ms` / `scope_ms` / `matrix_ms` / `knn_ms` / `hydrate_ms` nested
  inside the arm total, plus `matrix_built` for the inline pack build a process's
  first query pays. The sub-stages ride the same probe the bench reads, so
  `retrieval_gold_gate.py --latency` and the production ledger report the same split.
- **The cold-model flag was pinned true and named nothing.** `cold` was one bit OR'd
  across both model arms and sampled at entry, so an installed-but-disabled
  cross-encoder — permanently "available and not loaded" — made every search read
  cold. It is now per-arm and sampled where a load would actually be paid:
  `embed_cold` at entry (the vector arm runs on every non-structural query),
  `rerank_cold` at the re-rank itself, so an arm that sits out contributes no signal.
- **Startup cost is recorded.** `warm_models` exists to move the tens-of-seconds model
  load off the request path, and moving a cost is not removing it. Each pass now
  writes a `warm` ledger row — total plus `embed_ms` / `rerank_ms` / `graph_ms` /
  `search_ms`, and the stages that failed — so "how long after a restart is this
  server useful" has an answer, and a load creeping toward an MCP client's timeout is
  visible before it crosses.
- **Searches that raise are recorded.** Reads already logged in a `finally`; searches
  did not, so a search that failed slowly left no trace — biasing every percentile
  computed off the ledger toward the calls that happened to succeed. Both now record
  `failed`, with the time burned. Rendering is measured too, as `render_ms` beside a
  `duration_ms` that still means retrieval alone, and reads record `chars` — a read's
  cost tracks how much conversation it materialized, so latency without size was a
  distribution missing its main explanatory variable.
- **The watcher timed itself.** The pass heartbeat recorded what ingest did and never
  how long it took: no pass wall time, no per-source cost, and no freshness number at
  all. It now carries `pass_ms` / `pass_ms_max`, a per-source cumulative `ms` beside
  each source's yield counters (a source polled 1.5M times for nothing and one polled
  twice expensively were indistinguishable), and `lag_s` — how far behind real time
  the newest ingested event is, sampled only on passes that actually imported.
- **The web viewer has request telemetry.** It is a real read surface running the same
  engine, and nothing had ever recorded its latency. Served requests now append to
  `web-requests.jsonl` (path, status, size, wall time; no query strings). Deliberately
  its own file — `retrieval-usage.jsonl` is the sampling frame evals are mined from,
  and folding a human clicking around into "queries an agent asked" would bias them.
- **A served search now records where its time went.** The viewer's worst measured
  latency is a search (a 65s `/api/search` sits in the ledger with no attribution
  whatsoever), and an endpoint total names the endpoint and nothing else — the same
  reason the arm totals were split into sub-stages. A probe now wraps the whole
  dispatch, and its breakdown is folded flat into the row, under the field names the
  retrieval ledger already uses so one analysis reads both surfaces. It rides along
  only when the probe reports work, so the hundreds of cheap rows a browsing session
  makes stay two fields wide instead of carrying five zeros that would read as
  *measured and instant* rather than *did not happen*. Rows also carry `concurrent`
  (requests being served at once — the server is threaded and the SPA opens several
  per page, so a slow row is routinely slow *beside* others) and the same
  `context` contention sample the MCP tools take. Both are self-limiting: a request
  served alone on a quiet machine records neither.

- **Bulk import was quadratic: each imported file paid for every file before it.**
  `import_path` runs the maintenance checkpoint per file, and two parts of that
  pass cost O(archive size) — the `import_state` snapshot rewrites every row (with
  two fsyncs), and the shard-rebalance sweep walks every thread file to count them.
  Per file that is invisible; across a cold catch-up or a corpus build it is a
  quadratic term, and it dominated: on a 9,146-conversation build the `import_state`
  table reached 9,146 rows and 3.4 MB, rewritten in full 9,146 times. Ingest
  throughput collapsed from ~89/s to ~8.5/s *within a single build*. Both are
  cadence work whose staleness is already documented as safe, so the maintenance
  form now runs them on a sweep interval (first call in a process always, then at
  most every 30s) while the full form — pre-backup, pre-reindex — never defers.
  A/B over 6,000 imports, one binary and one machine: 225s → 40s (5.6×), with
  throughput decay across the build falling from 6.8× to 2.2× — what remains is
  index growth, not a quadratic term. On the real CDR corpus, 3,000 conversations
  ingest in 27.0s against 71.4s. The live watcher benefits too: it was rewriting
  the whole snapshot every poll.
- **A phase now reports whether it is slowing down, not just its mean rate.** A mean
  is exactly the statistic that hides work whose per-item cost grows with what it
  has already written — the import collapse above averaged out to a healthy-looking
  15.8/s. Each phase keeps the throughput of its first and most recent 10s windows
  and reports `slowdown` (their ratio) into the live state and the ledger; the CLI
  prints it once it is material. Sampling rides the existing live-state refresh, so
  the progress path pays nothing, and a phase shorter than two windows reports no
  trend rather than a fabricated one. On the A/B above it read 6.23 against 1.56 —
  the pathology is now a number the ledger carries, not something to notice by eye.

- **Model colors in the viewer mean something now.** The per-model accent used to
  be a hash of the model's name, so `claude-opus-5` came up red while the other
  opuses were green and a thread's colors said nothing about what ran in it. Hue
  now comes from the model's family — every opus a green, every gpt/codex a blue,
  fable red, sonnet violet, haiku amber, and so on down a table of the families
  the archive actually holds — and the version picks a shade inside that family's
  band, so a thread header or a stats table reads as "two opuses and a gpt" at a
  glance. Models from unknown families keep a hashed hue, muted so they can't pass
  for a family color.

- **The drift quarantine is no longer eaten by the export-drop watcher.** Both
  live under `<home>/dumps/`, but only account exports are drops: the watcher
  scanned `dumps/drift/`, failed to classify it, and moved the whole tree into
  `dumps/failed/` as an unrecognized export — burying the preservation copy of a
  degraded source's raw store (often the only copy left once the harness prunes)
  under a name that means "this export needs a look", raising a capture error per
  sweep, and costing the snapshotter the prior generations it copies
  incrementally against, so each night re-copied the whole active window. The
  drift dir is now reserved alongside `failed/` and `imported/`.

- **A quiet archive no longer reads as a stalled watcher.** The health page ages
  the operational records against *now* — capture is stale past 15 minutes — but
  they rode the status survey's TTL cache, which nothing refreshes but a request.
  A page opened after twenty idle minutes therefore got a twenty-minute-old "last
  check" stamp and declared a perfectly live watcher stalled, red trust center and
  all. `api.operational_records` splits the freshness-bearing half of `status`
  (health records, pipeline verdict, watcher liveness, backup device check, load
  state) from the expensive counts; `/api/status` serves the counts cached and the
  records fresh. The health page re-reads on its idle cadence too, so ages stay
  true while it sits open instead of freezing at load time.

- **Registry entries can say what an archive is *for*.** Entries in
  `~/.thread/archives.json` carry an optional descriptive `role` (`live`,
  `benchmark`, `snapshot`) set via `thread_archive archives --set-role` /
  `--clear-role`, shown in the CLI listing and as a badge on the health page's
  archive cards. Purely descriptive — a role grants and gates nothing.
  `snapshot` stamps `role: snapshot` on its dest automatically. Scratch homes
  stay out of the registry entirely: a restore drill's temp home and a restore's
  staging directory no longer register (including via their smoke passes'
  re-entrant opens).

- **An abandoned CLI session no longer reads as a capture failure.** Capture
  coverage judged a source's ingest stale when its newest store *mtime* ran ahead
  of its newest archived event — so a session opened and never used (grok and
  codex write metadata at startup, before any turn) failed the nightly's coverage
  stage from the moment it was consumed until the next real conversation landed,
  and marked its source `degraded`, the verdict the MCP search notice and
  `fix-import` key on. Staleness now measures the store activity the archive has
  yet to *account for*: a file the importer consumed whole and settled as an empty
  session — one routine skip-ledger record, watermark covering its current bytes —
  is not evidence of missed capture. A session the archive keeps re-consuming to no
  effect is not settled and still fails, which is what keeps the drift catch the
  comparison exists for: a blind parser's sessions grow.
- **Loading an archive is now a tracked, phased, timed event.** Bringing an
  archive up to date — importing transcripts, rebuilding FTS, embedding vectors —
  was the longest thing the product does and the least visible: work happened
  inside one call that returned a count at the end, so "is it stuck or working",
  "how far along", and "which phase costs the hours" were unanswerable. The
  `_ops.load_runs` ledger records each load as a run of phases; a phase carries its
  wall time, a `done/total` progress counter with a live ETA, and a `detail`
  split of named sub-timings. The embed drain reports its `select`/`encode`/`write`
  split (measured: encode is ~96% of it), so where the time goes is a fact, not a
  guess. Live progress publishes to `<home>/load-state.json` for any process to
  read (`thread_archive loads`, `GET /api/loads`); a summary lands in
  `<home>/load-runs.jsonl`. A run that dies mid-phase reads as `stalled`, not
  `running`. `reindex`, `embed`, and the one-shot `watch --once` catch-up are
  tracked; the continuous daemon poll stays untracked (it keeps its pass
  heartbeat and writes no per-poll rows). `THREAD_ARCHIVE_LOAD_LOG=0` disables it.
- **Time-to-first-usable-search is import, not embed.** Lexical search is live the
  moment import finishes (FTS is trigger-maintained; the vector arm degrades in
  until vectors exist), so the number that gates a new user's first search is the
  import throughput (~16 MB/s on this hardware), not the hours-long cold embed that
  runs in the background behind it. The `watch --once` catch-up now shows a live
  per-file progress line with an ETA.
- **A registry of known archives** (`~/.thread/archives.json`): an archive becomes
  known by being opened, so `thread_archive archives` / `GET /api/archives` can
  list every home and its live load state — including archives the current process
  hasn't opened. `THREAD_ARCHIVE_REGISTRY=0` disables it.
- **The embedder loaded fp32 while the cross-encoder loaded fp16.** The dtype
  policy lived in `rerank.py` and `embed.py` never applied it, so the corpus embed
  ran at full precision — the difference between a cold embed in an hour and in
  several. The policy now lives once in `embed.py` and both model paths read it.
- **The embed drain now length-sorts within a recency window, ~halving encode.**
  The embedder pads every text in an encode batch to the longest one in it; draining
  docs in the store's natural (`event_id`) order put a 30-char user turn and a
  2048-char slice in the same batch, so most of the encode was padding (measured
  ~4/5 waste, and encode is the bulk of the embed). The drain now length-sorts the
  pending docs so each batch is length-homogeneous — measured 14.2 → 27.7 chunks/s,
  a ~2× speedup on the longest phase of a cold load. The sort is *windowed*
  (`_SORT_WINDOW` docs), not global: the drain still walks newest-window-first, so
  recent-thread semantic recall stays current and a large pass writes the newest
  docs durably before the oldest. `sort_window=0` restores the natural order.
- **The health view shows every archive's load state and history.** The registry
  and the load ledger had no surface: "which archives are loading, which are built,
  and what did past loads cost" was answerable only by reading files. The health
  page now lists every known archive with its live phase, progress, rate and ETA
  (polling while a load is in flight, backing off when idle), plus a cross-archive
  history table with each run's per-phase cost. It reads from each home's own state
  file, so a load running in another process — on an archive this one never opened —
  is visible as it happens. Each registry entry carries its own recent runs, so the
  view is one fetch rather than one per archive.
- **"Loaded" claimed something the product does not do.** Being indexed and being
  reachable are independent: `thread_search` / `thread_read` answer from the single
  archive their process was started against, so an archive can be fully built and
  answer no query. The state pill now names only the derived-data axis — `Indexed`,
  or `Untracked` when an index exists but no load was ever recorded for it, so
  completeness is unproven rather than asserted — and reachability is its own
  marker, `served` / `not served`, stated on every archive rather than inferred
  from a missing badge.
- **The embed model's cold load was billed to `encode`.** The model loads lazily
  inside the first `embed_documents` call, so the tens of seconds it takes landed
  inside the first batch's `encode` timing — on a short pass that was most of the
  reported encode, and it inflated encode's share of the embed. The drain now warms
  the model up front under its own `model_load` sub-timing.

- **Stats token and cost totals were over-counted.** Claude Code repeats one
  response's full usage object across every transcript row that response produced,
  so an event is not a request. The rollup deduplicated exactly one column —
  `cache_read_tokens`, via a `request_cache_metrics` ledger — while the fold
  directly above it summed the same duplicate rows straight into `requests`,
  `input_tokens`, `output_tokens`, `thinking_tokens`, `cost` and `cost_requests`.
  On this archive that was 31M phantom output tokens and 25k phantom requests
  (production totals on the rebuild: requests 461,397 → 436,407, input 92.55M →
  88.15M, output 306.54M → 275.71M). The ledger is now `request_metrics` and holds
  every usage figure, one canonical row per provider request, `MAX` per field so a
  duplicate arriving in a later watcher poll still collapses. `thread_metrics` is
  re-derived from it rather than accumulated into, which also makes re-folding an
  already-folded window a no-op instead of a doubling — verified bit-identical
  against a 200k-event re-fold on the live index.
- `refresh_metrics` rebuilds only the threads the folded window touched. It
  previously reran an unbounded full-table `UPDATE … SET cache_read_tokens = (
  correlated subquery)` over every `thread_metrics` row on every fold, which is the
  per-request full survey the incremental cursor exists to avoid.
- `metrics_cursor.cache_requests_ready` (a one-shot "backfilled once" bool) is now
  `projection_version`, an integer compared against `_metrics.PROJECTION_VERSION`.
  Any change to the fold that makes old sums incomparable is a bump, and the next
  refresh discards and rebuilds instead of adding to them.
- Fixed a crash opening any archive predating both metrics columns. `_ADDED_COLUMNS`
  iterates in dict order, and the `thread_metrics` fixup wrote
  `metrics_cursor.cache_requests_ready` — a column the *next* entry had not created
  yet — so `init_db` raised `no such column` and the open died. It self-healed on a
  second open (the ALTER autocommits, the fixup's DML rolls back), which disguised a
  deterministic ordering bug as a transient race. Schema provisioning no longer
  writes data at all: it provisions shape, and `refresh_metrics` owns staleness via
  `projection_version`, so the steps are order-independent by construction.
- `init_db` drops projections a newer shape superseded (`request_cache_metrics`, the
  `cache_requests_ready` column) rather than stranding them. They are disposable
  re-derivations of the event log, and a stale one left in place reads like a live one.

- New gold miner **`commit`** (`thread_archive mine commit`) and the
  `evals/swechat_corpus.py` harness that feeds it. Every existing miner
  establishes its labels by searching with the engine under test — the rerank
  judge grades a pool production search returned, the query/topic labelers sweep
  with their own reformulations through the same stack — so a systematic retrieval
  blind spot is invisible to labeler and ranker alike and can never score as a
  miss. `commit` takes its gold from **provenance** instead: a linkage file pairs
  each session with the commits it demonstrably authored, an agent reads only the
  commit (message + diff) and writes queries for it, and the linked session is the
  answer. No search runs during labeling, and since the agent never reads the
  target thread there is no vocabulary leakage either — the bias query-gen carries
  by construction. Same-repo siblings grade themselves structurally (overlapping
  files 1, disjoint 0), so the confound pool costs no tokens; only the grade-2 is
  grounded, 1/0 are proxies. Needs a corpus shipping session↔commit provenance, so
  it stays out of `mine all`.

  `swechat_corpus.py` builds that corpus from
  [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat) (public, ODC-BY,
  arXiv:2604.20779), whose transcripts are native Claude Code JSONL and so ingest
  through the shipped importer unchanged. Its value is being an **independent
  hold-out**: every other gold file is mined from one corpus by one author, and
  hold-out discipline within a corpus cannot see overfitting to it. Unlike the
  BEIR/CDR/haystack yardsticks it is domain-matched — agent session logs, not a
  third-party IR corpus. Roughly 2100 of 5851 sessions carry attributable commits,
  about half of which have retrievable commit content.

- **A gold file's number meant nothing on its own.** `evals/bm25_baseline.py` scores
  any case file with SQLite FTS5's `bm25()` alone — same cases, same metrics, same
  searchable scope, none of the ranking above it (no density/phrase/recency
  weighting, no RRF, no coherence pass, no rerank). The BEIR and haystack harnesses
  carry published BM25 references for *their* corpora; a corpus the golds were
  actually mined from had none, so an MRR could only be compared against itself.
  With `retrieval_eval.py --lexical-only` and `--rerank off` as the middle rungs,
  one case file now yields an ablation ladder from plain term matching to the
  shipped pipeline. Ranks a thread by its best-matching chunk; the FTS5 auxiliary
  function forces a MATERIALIZED CTE, since a plain subquery gets flattened into the
  aggregate and errors.

- **Mined gold is exportable as a benchmark somebody else can run.** Every case the
  miners write is keyed by a thread id this archive minted at ingest, so the gold
  under `swe-chat-data/gold/` scored only here — the queries and judgments were
  portable, the identifiers were not. `evals/swechat_bench.py export` rewrites them
  onto the dataset's own `session_id` and writes the four files a retrieval benchmark
  is made of: `queries.jsonl`, TREC `qrels.txt`, a `corpus.jsonl` pinning the exact
  documents in scope, and a manifest carrying the dataset revision, the selection
  rule, the harness bound and the scoring contract. Query ids are content-addressed
  over protocol, scope and text, so re-exporting is stable and runs stay comparable;
  scope is in the digest because the same question asked of two repositories is two
  questions. The corpus text is pinned by id and revision rather than copied — the
  upstream dataset is gated, and mirroring it would route around that.
  `swechat_bench.py run` drives a ranker over the exported queries into a TREC run
  file, so the runner contract is a file format rather than a call into `evaluate()`;
  FTS5's `bm25()` is lower-is-better and gets negated on the way out, since a
  conforming scorer sorts on the score column and would otherwise read that baseline
  as its own exact reverse. The published scorer agrees with `evaluate()` to
  floating point (max delta 2.2e-16) across every protocol and both baselines,
  including the queries a ranker answers with nothing — those stay in the
  denominator on both sides.

- **The SWE-chat corpus grows cross-repo topics, grouped by what sessions touched.**
  One topic per repository is confound-dense but trivially separable — each repo
  owns its file and module names, so nothing in one competes with a query aimed at
  another, and the topic miner's whole point is subjects where near-misses are real.
  The embedding communities `corpus_topics.py --propose` offers do not supply the
  missing case on this corpus: measured against the repo partition, five of twelve
  are 78–100% a single repo (the repo topic renamed) and three cluster on harness
  boilerplate — one is forty sessions sharing a Conductor system preamble, another
  sixteen sharing a persona header. `swechat_corpus.py` now also groups by the class
  of file a session touched (`files_touched`, a recorded fact, so no model is shared
  with the vector arm and boilerplate cannot form a cluster), keeping the groups big
  enough to survey and not dominated by one repo: CI config, dependency manifests,
  styling, agent instruction files, test suites — 31 to 145 sessions each, spread
  over 15 to 19 repositories. Membership is a proxy and does not need to be exact;
  it selects the subject a survey agent is pointed at, while the graded pool still
  comes from the labeler judging each thread against the query's intent.

- **SWE-chat's derived artifacts moved out of the private gold dir.** Mined cases
  live under `~/.thread/archive` because they quote the operator's real
  conversations — but nothing mined off a public corpus does, and a gold file is
  only meaningful beside the download it resolves against. `swechat_corpus.py` now
  writes the linkage (and points the miner's `--out`) at `gold/` beside the data
  dir, a sibling of the download so re-fetching the dataset never sees it.

- **A corpus home built from a fixed dataset is already the snapshot.** Mining and
  `retrieval_eval --cases` gate on a `snapshot.json` id, and the only way to get one
  was `thread_archive snapshot <dir>` — which copies truth, rebuilds an index beside
  it, and stamps the result. That pipeline exists to *make* a frozen home out of a
  live one; a home an eval harness builds from a fixed download is born frozen, so
  the copy bought nothing but a duplicate of a multi-GB home, and the SWE-chat
  harness's documented flow ended by telling you to make one. `stamp_snapshot()`
  writes the manifest in place instead — same contract, same content-derived id, no
  copy — and `swechat_corpus.py` stamps at the end of both its phases. It refuses
  the live archive (which grows, so a stamped id would go on blessing golds the
  corpus has already moved past) unless forced. Re-stamping is the cadence after a
  rebuild: the id follows the corpus, so the previous run's golds read as stale
  rather than silently scoring against a corpus that changed shape underneath them.

- Recent-conversation cards now include up to 200 characters from the first
  non-empty user message, so a title alone is no longer the only recognition cue.

- The ranker gained a **`bm25_weight` term** over `_lex` — the lexical arm's own
  placement of a hit (peak-normalized reciprocal rank, stamped in the pool half of
  `search`) — shipped at `100.0`. The arm's verdict previously reached the scorer
  through one channel only: FTS5 orders by bm25 but never surfaces the score, and
  `_rrf`, the feature carrying rank evidence, is computed only when the vector arm
  returns. So a lexical-only search (a `tool_name` or `types` scope, a structural
  query, an archive without embeddings) ranked on density alone — and density is
  IDF-blind and length-normalized, weighing a corpus-common term exactly like the
  rare one that discriminates and then dividing by length. Measured on BEIR scifact
  over a fixed pool, varying only the ordering: the pool's own bm25 order scores
  0.682 nDCG@10 (above the 0.665 published Anserini BM25 reference) while an
  unweighted density re-scoring of that same pool scores 0.302, pushing 54 of 332
  gold documents out of the top-200 entirely (recall@100 0.924 → 0.716) and
  dropping top-10 median document length 1496 → 835 chars.

  The shipped weight is set by the **gold files, not that benchmark**: 100 is
  where findability gains .019 MRR / .015 nDCG@10, judged .013 MRR and
  rerank-cases .052 success@10, against the recall it costs the confound-dense
  files (frustration −.048 recall@10, context-compaction −.033). A deliberate
  trade, taken on the in-domain delta. 200 buys findability another .015 nDCG@10
  for more of the same recall; past ~400 bm25's order overrides the density
  evidence the topic files lean on and they break their floors.
  `evals/experiments/no_bm25.py` races the ablation.

  The external suite, tuned against by nothing, agrees: BEIR scifact lexical
  0.302 → 0.445 (recall@100 0.716 → 0.900) and fused 0.650 → 0.658; CDR lexical
  0.101 → 0.230 (recall@100 0.250 → 0.569) and fused 0.458 → 0.492;
  LongMemEval-S 0.894 → 0.912 recall@10. The benchmark gap is deliberately left
  open — `bm25_weight` near 2000 reaches BEIR's BM25 reference and breaks five
  gold floors doing it. LoCoMo is flat (fused 0.649 → 0.653, re-rank forced on
  0.790 → 0.787) because the term is out of scale there, not inert: density is
  normalized to `density_norm_chars` but unbounded, so on a corpus whose every
  document is a sub-500-char turn (median 116) density runs several times larger
  than on archive-length text and a fixed-scale bm25 term cannot reach it. Read
  a flat number on a short-document corpus as scale, not as no effect.

- `pool_cache.FORMAT_VERSION` → 2, since cached pools now carry `_lex`. A pool
  cached by an older build would have scored the new term as zero and made a
  `--set bm25_weight=…` sweep read as having no effect.

- Search quality gained a **recall-shape tier** (`tests/test_search_recall_shape.py`)
  alongside the ordering floors. The existing tier-0 metrics (MRR, success@k, and a
  "recall@k" over golds that are mostly one thread) score which thread *wins*; they
  are satisfied by a ranker that returns one right answer, so two shapes the archive
  is actually asked for went unmeasured: "every thread that mentions X" and "the
  first / last time we discussed X". Both are now scored on two blocks added to
  `tests/quality_corpus.py`, built so their golds are true by construction rather
  than by judgment — a **nonce sentinel term** carried by exactly 24 threads and
  nothing else (past the default result window, so only `group='browse'` /
  `output='count'` can return the set, and corpus noise can never fuzz the answer),
  and a **dated series** of 12 mentions across a year whose earliest mention is
  deliberately the weakest lexical match, so a chronological scan that merely echoed
  relevance order fails. Adding 36 threads left the ordering metrics untouched
  (MRR 1.0, recall@5 1.0).

- Two tier-0 invariants were **measuring precision while reading as recall**, and are
  now two-sided. `test_focused_thread_beats_passing_mentions` asserted a decoy's rank
  only `if` it was present, so a ranker that dropped genuinely-matching threads passed
  — demotion and disappearance were indistinguishable; it now requires each decoy to
  come back. `test_quoted_phrase_excludes_scattered_words` pinned the entire result
  list to a single thread, encoding "a quoted phrase has one answer" and making any
  future fixture carrying the phrase a failure; it now names the two threads its
  mechanism is about.

- `search(sort=...)` **rejects** anything but `'oldest'` instead of silently ignoring
  it. `group` and `agents` already validated; `sort` did not, so `sort='newest'` — the
  plausible guess for "when was this last discussed" — returned relevance order, a
  wrong answer indistinguishable from a right one. There is no newest sort; the most
  recent mention is read off an enumerated result set.

- External calibration re-measured at the shipped configuration (`fusion_weight=400`,
  cross-encoder off), and the fused numbers moved a long way: BEIR scifact nDCG@10
  0.509 → 0.650, CDR 0.249 → 0.458, LoCoMo recall@10 0.621 → 0.649, and LoCoMo with
  the re-rank forced on 0.756 → 0.790. Every lexical-arm number is unchanged, the
  expected shape — `fusion_weight` moves only the fused ranking. Three findings came
  out of it:
  - The lift **generalizes**. `fusion_weight` was tuned solely against the mined gold
    files, and it lifted four third-party corpora nobody tuned against (+0.141 BEIR,
    +0.209 CDR). The external suite therefore works as an unplanned held-out set for
    gold-tuned ranking changes, and is worth scoring after a defaults change.
  - The cross-encoder is **domain-bound, not superseded**. Its lift over fusion on
    LoCoMo is +0.141 recall@10, essentially unchanged by the fusion increase, so on
    turn-level dialog the two arms are additive rather than overlapping. The "buys
    ~no MRR" verdict behind `rerank_auto=False` holds for the archive's own golds
    only. It costs 4207s against the +vectors pass's 157s over the same corpus.
  - The **lexical arm is the open problem**. At nDCG@10 0.302 it still trips
    `beir_eval`'s own `BELOW BM25 — investigate` verdict (−0.363 against the 0.665
    reference) while lexical recall@100 is 0.716 — the pool holds the right document
    and the re-scoring buries it. BM25 ranks candidate *selection* only; there is no
    bm25 term in `SearchParams`, and density/recency/content-type are inert on a
    corpus with no time axis and one content type. A `bm25_weight` ranker term is the
    experiment this points at; unmeasured so far.

- `docs/search-quality.md` rewritten to the current measurement regime. It had led
  with a click-label (`--from-log`) table as its headline metric; the measurement of
  record is the seven snapshot-bound gold files, so the doc now leads with their
  per-file MRR/success@10/recall@10/nDCG@10 and demotes the click protocol to the
  alarm it is. Also corrected: the gold gate is a deliberate run rather than a CI
  row, the cross-encoder ships off by default, coherence carries fresh `graph_eval`
  numbers at the shipped γ=0.005, the rejected PageRank-authority term is gone from
  the code rather than described as a live candidate, and a latency section covers
  the p50/p95 the speed axis now measures.

- `retrieval-gold-gate` dropped from the CI suite list (`ci.toml`). Its ~140 live
  searches with the embedding + rerank models loaded run at the edge of the 600s
  runner cap, so it timed the sweep out under load. The grounded regression floor
  is now a deliberate run — `scripts/retrieval_gold_gate.py`, alongside the tuning
  loop it already hosts — while the per-commit CI path keeps the `retrieval-gate`
  arm-liveness probes.

- The gold gate scores the speed axis too: `--latency [REPS]` measures warm
  latency over the same queries it scores for quality (pool cache OFF — the arms
  are the cost being measured) and prints the joint report, so a `--set` tuning
  decision reads on both axes at once. This is the seam the quality rebuild needs:
  the cross-encoder re-rank is both the top quality lever and the top latency, so
  re-enabling it means doing so within a budget. Speed has a fail-fast too: with
  `--fail-early`, a smoke test runs the queries that were *slowest at baseline*
  (the corpus's own pathological cases) against a p95 ceiling and bails in tens of
  seconds before the full pass. `--budget-ms` sets an absolute ceiling; the
  default is 1.5× the recorded latency baseline. `--latency-smoke` runs only that
  smoke test — a ~1-minute interactive speed check (the quick loop is
  `--cache --latency-smoke --set …`), with the full ~10-min pass kept for the
  confirm. `thread_archive._ops.speed` is
  the measurement core (warm reps, cache suspended, per-stage from the same probe
  the usage ledger records), with a `latency-runs.jsonl` timeseries and
  `latency-baseline.json` beside the quality ones. First measurement: warm search
  is FTS-dominated now that the re-rank ships off — p50 ~800ms, p95 ~1.5s, with
  code-identifier queries the slowest shape.

- Ranking-knob tuning moved onto the gold gate, which is now the interactive
  loop rather than only the CI floor. `scripts/retrieval_gold_gate.py` gained
  `--set field=value` (score a candidate `SearchParams`), `--cache` (persist the
  candidate *pools* across processes), `--fail-early` (stop once a floor is
  provably unreachable — sound, so it is safe on the CI path too),
  `--max-regressions N` (abort once N cases that used to rank stop ranking), and
  `--only`. A tuning run is flagged `overrides` in the run ledger and never
  overwrites the per-case baseline, so an experiment can't be read as the
  baseline moving. Over the full gold set a re-run at new weights is 132 s → 19 s.

- The caching that makes the above fast lives in the retrieval pipeline, not the
  eval harness: a search now splits into `retrieve_pool` (the FTS + vector +
  fusion half, which reads only the query, the structural scope, and the two
  pool-shaping knobs `rrf_k`/`pool_floor`) and the ranking half (every weight).
  `_retrieval.pool_cache` is an opt-in, contextvar-installed, fail-soft cache of
  the pool half, keyed on every pool-affecting input — production never installs
  one. `rank.score_features`/`score_from_features` split the scorer the same way,
  so a weight change re-reads one set of feature rows. `_eval.evaluate` grew an
  `early_stop` hook (and reports `scored`/`aborted`/`per_case`); the gate's
  `EvalProgress.best_possible` is what makes the fail-early bound exact.

- Fixed a wall-clock race in the gold gate's scores: the community-coherence
  re-rank reads a corpus graph built on a background thread, which lands partway
  through a scoring run, so cases before it were ranked without coherence and
  cases after it with — where the split fell depended on how fast the box was.
  Two runs of identical code could disagree, and the floors were calibrated under
  it. The gate now builds the graph inline before scoring any case, making a run
  a function of the code and the snapshot alone (verified: cached and uncached
  runs now agree on all seven gold files to the digit).

- Stats now separates cached input reads from token totals across the overview,
  provider, model, monthly, and heavy-session views. Codex's provider-native
  inclusive input count is normalized to uncached input before aggregation, so
  cache hits remain visible without inflating its comparable token total; token
  and cost amendments also rewind the derived metrics projection immediately.
  Anthropic's native `cache_read_input_tokens` spelling is normalized too, and
  Claude Code's repeated content-block rows are counted once by API message id
  through a request-level incremental projection.

- Retrieval `fusion_weight` raised 100 → 400, recovering the paraphrase recall the
  cross-encoder used to buy — at no latency cost. Term density is unbounded, so a
  short doc carrying a few of a long question's common words outscored the fusion
  term's ceiling several times over and sank the vocab-mismatch answers the vector
  arm had already ranked first: on the findability cases the semantic arm alone
  scored MRR 0.693 while the final production order scored 0.581, with 12 cases
  whose gold sat at semantic rank 1 and final rank 2–13. Weighting cross-arm
  agreement to density's working scale keeps them reachable. Every gold file
  improves on nDCG@10, six of seven on MRR: findability 0.566 → 0.666 (recall@10
  0.859 → 0.922 — past what the cross-encoder reached), topic-alpha 0.905 → 0.929,
  rerank-cases 0.595 → 0.632, context-compaction 0.950 → 1.000, needle 0.739 →
  0.762. Head order tightens rather than flattens (success@1 0.551 → 0.609), the
  risk the previous calibration had flagged. Past ~500 the vector arm starts
  overriding lexical evidence it should defer to; saturating density instead
  (`d/(d+k)`) buys the same paraphrase recall and costs far more elsewhere, so the
  linear term stays. `evals/experiments/fusion_light.py` (the previous 100) and
  `fusion_heavy.py` (800) keep both sides of the optimum measurable.

- The retrieval gold gate now gates **all seven** mined gold files.
  `topic-cases-398932b913e9` and `topic-cases-03769ec66804` were scored and printed
  on every run but carried no floor entry, so they could have regressed to zero
  without failing CI. Every floor is also recalibrated to the shipped ranking
  config on an explicit rule: scoring is deterministic — the same code over the same
  snapshot reproduces the same numbers to the digit — so headroom is a regression
  tolerance rather than a noise band, and its natural unit is one case. A floor sits
  `1/n` under its measured value, so a single case may regress and two fail the
  gate; small files therefore carry the widest absolute gaps (a 7-case topic file
  tolerates 0.143, the 64-case findability file 0.016). An existing floor is never
  lowered to accommodate a change. Several had gone stale when the `fusion_weight`
  change lifted their files at once — topic-alpha's MRR floor moves 0.58 → 0.78 and
  judged's recall@10 0.70 → 0.85.

- The web viewer now opens as a retrieval workspace instead of an empty reader:
  a real home page searches every provider, exposes source/date facets, and groups
  recent conversations by day; the same URL-synchronized search surface serves
  home, results, and the navigation rail. A contextual app header replaces the
  corpus-index telemetry strip, `/` or Command/Ctrl-K focuses search, and the
  sidebar's competing recent-title filter is gone.

- The retrieval-usage ledger now records a **per-stage latency breakdown** for
  every MCP search: `fts_ms`, `semantic_ms`, `rerank_ms`, `did_rerank`,
  `pool_size`, and `cold` (present when a model loaded inside the request — the
  cold-model tail). Total `duration_ms` alone couldn't see which stage a slow
  search spent its time in; the breakdown makes the ledger self-diagnosing and any
  latency change self-validating. A fail-soft, opt-in `_probe.SearchProbe`
  context-local carries the timings out of `search()` — no probe installed (evals,
  tests, direct callers) means every timing point is a cheap `is None` check, so an
  unmeasured search is never slowed. Still ids and timings only, never content.

- The retrieval gold gate now **records every run as a timeseries**, not just a
  pass/fail against fixed floors. Each run appends one row to
  `<home>/gold-runs.jsonl` (`thread_archive._ops.gold_runs`): per gold file's
  MRR/success@10/recall@10/nDCG@10 and p50 latency, the active `SearchParams`, the
  model-arm switches, the snapshot id, and the code commit. So the baseline is a
  recorded history — "baseline was 0.46 MRR on commit X under pool=24, 0.44 on Y
  under pool=12" is a lookup (`retrieval_gold_gate.py --history`), and the
  before/after of a defaults change is on disk under the config that produced it
  instead of needing the old configuration re-run. The gate's *verdict* stays a
  floor check (a displayed number is not a quality score — see the gate docstring);
  the ledger is the same telemetry the gate already prints, kept.

- **Search worst-case latency brought under ~1.5s** (from a 7s p50 / 57s p99 in the
  usage ledger). Three tail sources fixed:
  - *Cross-encoder re-rank off by default* (`SearchParams.rerank_auto=False`). It
    was the pipeline's dominant cost — measured 2–4s on a long conceptual query,
    with wide variance — for ~no gold-file MRR over the fused
    lexical+semantic+coherence stack. Auto-re-rank now sits out; `rerank=True` still
    forces it, and the community-coherence re-rank still orders the head. The
    quality-rebuild seam is the search lab (`rerank_pool8.py` = a budget-fitting
    re-rank candidate, `rerank_rich.py` = the old full budget to beat). Its
    `rerank_pool` (24→12) and the new `rerank_doc_chars` (1500→768) knobs stay, for
    when a re-rank re-earns its place within budget.
  - *Code-identifier queries no longer scan the whole corpus.* A `foo_bar` query
    whose exact-phrase MATCH came up short fell through to a `content LIKE '%…%'`
    full-table scan (~10s over ~3.9M rows). Now indexed token-MATCH fallbacks (the
    identifier's tokens, which ride the FTS index) fill the pool first, and the
    residual substring scan — the within-token catcher MATCH can't do — is bounded
    to the recent-id window (`_LIKE_SCAN_CAP`). Worst case ~9.8s → ~0.2s (0.6s for a
    genuinely all-rare-token query).
  - *Cold-model load no longer lands in a request.* A query arriving before the
    server's background warm finished used to block on the tens-of-seconds model
    load (the 56–134s ledger outliers). The server now defers construction to warm
    (`model_slot.set_defer_construction`): until the models are resident, a query
    serves lexical-only (fast) and the semantic/re-rank arms rejoin automatically
    once warm lands.

  The gold gate's `findability-cases` floor is recalibrated to the reranker-off
  baseline (MRR 0.72→0.54, nDCG@10 0.75→0.60): its paraphrase-match cases are the
  one dimension the cross-encoder uniquely lifted, so it dropped when auto-re-rank
  went off (most other gold files *improved* — the cross-encoder had been shuffling
  their good heads). Recovering paraphrase recall without the cross-encoder is the
  tracked quality-rebuild that re-raises the floor; both regimes are recorded in
  `gold-runs.jsonl`.

- `SearchParams` gained `rerank_doc_chars`, the per-passage character cap the
  cross-encoder scores each hit at — first-class so the search lab can race passage
  length as a plain `PARAMS` experiment.

- `evals/search_lab.py` gained `--sample FRAC`, a fast-iteration subset for the gold
  bench: it scores a deterministic, hash-selected slice of each gold file (the same
  cases every run, nested as `FRAC` grows) instead of the whole file. Paired with
  `--only <experiment>` it turns a tuning loop from the full bench's tens of minutes
  (baseline + every experiment × all ~124 gold cases, fused + reranked over the
  snapshot) into a couple. A subset reads a *direction* on grounded data, not the
  promotion delta — the CLI prints a SAMPLED banner and per-file `n/n_full`, and the
  full bench (drop `--sample`) stays the promotion bar.

- The shared in-process model (nomic embedder, cross-encoder reranker) is now safe
  under concurrent use. `ModelSlot` grew a `use()` guard that serializes access to
  the one process model, and `embed._encode` / `rerank.rerank_scores` drive their
  forward pass through it. A torch forward pass isn't reentrant: two threads
  encoding at once corrupted the model's length-sized buffers, surfacing as
  intermittent `embed: encode failed (size of tensor a (N) must match tensor b (M))`
  and a silent fall back to a lexical-only pool for that query. It bit anything that
  fans searches out over a shared embedder — `thread_archive mine rerank --jobs 5`
  (~9 in 400 query-embeds failed), and any daemon serving concurrent searches.

- Search's community-coherence re-rank now gates on embedding availability at the
  call site: it runs only when the embed arm is live (`embed.is_available()`), so a
  core lexical install — or `THREAD_ARCHIVE_EMBED=off` — skips it instead of kicking a
  graph build that probes an `event_vectors` table that never exists (previously a
  swallowed per-query exception — fail-soft, but noisy). The graph primitives stay
  embed-agnostic for tests and direct callers; only the search path gates.

- The eval lab gained two external benchmark instruments beside `beir_eval.py`:
  `evals/cdr_eval.py` (NVIDIA ChatRAG's CDR, a shared-corpus conversational-retrieval
  benchmark scored by nDCG@10) and `evals/haystack_eval.py` (`--dataset
  locomo|longmemeval`, per-question haystack retrieval scored by recall@k against the
  datasets' published baselines). The haystack harness caches each built+embedded
  corpus home by content under `~/.cache/thread-evals/homes/`, so a re-run — or the
  `--rerank` pass over an already-embedded `--vectors` corpus — reuses the embeddings
  instead of rebuilding (`--rebuild` forces a fresh build, `--fresh` uses throwaway
  homes). Measured numbers land in `docs/search-quality.md` (External calibration):
  the full stack reaches LoCoMo recall@10 0.756, above DRAGON's 0.662 at every cutoff.

- `evals/search_lab.py` now races the `experiments/` configurations over the **snapshot-bound
  gold files**, and a bare run scores **both benches** — gold and synthetic — where it used to
  score only the synthetic corpus. The gold bench runs the fused production pipeline natively
  over the frozen snapshot's vectors and prints one leaderboard per gold file: the
  challenger-vs-baseline ΔMRR on the graded pools the gold gate floors, which is what actually
  credits a ranking change (the synthetic corpus, lexically easy, only points a direction;
  `retrieval_eval.py --cases` scores a single production config, not a challenger). Running both
  by default is the point — the synthetic leaderboard lands in seconds while the gold pass
  (real models over the whole snapshot, minutes) is still going, so a gross regression shows
  immediately and the grounded verdict follows; `--gold` / `--synthetic` narrow to one. Snapshot
  home defaults to `~/.thread/archive-snap` (`$THREAD_ARCHIVE_SNAP`), gold dir to `~/.thread/archive`
  (`$THREAD_ARCHIVE_GOLD_DIR`), reusing the gold gate's file-discovery and snapshot-fingerprint skip
  so a moved corpus is never scored against stale golds. The synthetic bench runs first (into a
  throwaway home it deletes) and gold repoints the engine off it before reading, so the synthetic
  corpus can never leak into the snapshot. Both keep coherence off so the delta stays
  deterministic. On a box with no snapshot, a bare run still prints the synthetic leaderboard and
  notes the gold skip; `--synthetic` asks for that explicitly.

- Gold mining is now a first-class product subsystem: `thread_archive mine` (package
  `thread_archive._mine`), replacing the `evals/retrieval_mine_gold.py` and `evals/topic_mine_gold.py`
  scripts (deleted). A `Miner` contract + registry backs three shapes — `thread_archive mine` lists
  the miners, `thread_archive mine <miner> [args]` runs one, `thread_archive mine all [N]` sweeps the
  ones a count alone can drive. The two existing miners ported unchanged in behavior (`query` →
  `judged-cases.jsonl`, `topic` → `topic-cases-<slug>.jsonl`), and two new cheap rungs join the ladder:
  `rerank` grades a retrieved pool with one judge pass (precision/ordering within what search
  retrieved; blind to recall by construction, but reports a `none-of-pool` recall-failure rate) and
  `querygen` generates difficulty-laddered queries for a random thread to test findability (recall,
  corpus-representative → `findability-cases.jsonl`). The agent corpus seam moved to `python -m
  thread_archive._mine tool search|read`, so mining runs from an installed wheel, not only a dev
  checkout. Output still lands under `~/.thread/archive/` with `cases`-in-name basenames, so the
  `retrieval-gold-gate` discovery and the baseline sweep pick up the new files automatically (ungated
  until a floor is calibrated). `evals/retrieval_eval.py` (the scorer) and the experiment lab stay on
  the bench.

- `thread_archive mine` now rate-limits its agent fan-out. A process-global semaphore in
  `_mine/_agent.py` caps concurrency at 5 live `claude` sessions, enforced at the single choke point
  every miner passes through (`run_claude`), so the ceiling holds regardless of a miner's `--jobs` or
  how many miners a `mine all` sweep chains. A second cap bounds total spend: any one `mine` command
  launches at most 25 agent sessions — a single miner's `--target` (and the topic miner's labeler
  count) clamp to it, and a `mine all` sweep spends 25 *in total*, split as evenly as possible across
  its runnable miners (a modest per-miner target is honored in full; only a wide sweep is trimmed). So
  a fat-fingered `--target 500` or `mine all 100` runs bounded instead of running up a bill. `--jobs`
  clamps to the concurrency ceiling (more workers would only block on the semaphore), and the list
  view footer states both caps.

- Removed the cross-encoder net-lift figure ("~2 points of success@10") from the docs
  (`_retrieval/rerank.py`, `docs/search-quality.md`) — a log-mined/title-proxy number never
  re-established on the snapshot-bound gold files that are now the measurement of record, where a
  rerank on/off ablation shows no reliable net lift (mixed by file: helps one, hurts another, neutral
  on the rest). The docs now state only the ~5× latency cost and the gating that follows from it; the
  auto-gate and strong-head stand-down code are unchanged.

- Retrieval `fusion_weight` raised 50 → 100. The normalized cross-backend `_rrf` agreement term in
  the weighted ranker was tuned on the discredited title-proxy eval and left the semantic arm
  underweighted against term density: a vocab-mismatch answer the vector arm surfaces (density ~0,
  high `_rrf`) sank under any lexically dense confound (`density*100` dwarfing `rrf*50`). Doubling the
  term lets semantic agreement compete. Measured in production shape (rerank=auto) over the
  snapshot-bound gold files (snapshot `9519fc4518e13ee7`): aggregate success@10 0.909 → 0.945, true
  recall@10 0.708 → 0.746, nDCG@10 0.571 → 0.584, MRR 0.608 → 0.619, success@1 flat, no latency cost.
  Tuned on the query-mined `judged-cases`, confirmed on the held-out topic files (largest held-out
  lift `topic-cases-398932b913e9` S@10 0.900 → 1.000, R@10 +0.083; neutral on the other two topic files; one
  noise-level dip on context-compaction R@10 −0.014). The gains land in top-10 reachability, not
  success@1 — the rank-1 lexical confounds hold, but more real answers reach the window agents scan.

- The `evals/README.md` baseline runbook now leads with `scripts/retrieval_gold_gate.py` as the
  one-command read of the current gold-file baseline: it discovers every gold file, scores each over
  its bound snapshot with the production ranker at the canonical `limit=20`, and prints per-file
  MRR / success@10 / recall@10 / nDCG@10 (the CI gate's measured numbers print on every run, floored
  and ungated files alike). The per-file `retrieval_eval.py --cases` instrument stays the path for the
  fuller metric set and for scoring a challenger on both sides of a change.

- Retrieval evaluation now separates first-hit success@k from true recall@k (the fraction of every case's
  grade-2 gold set recovered) instead of calling success "recall." Reports, the operator CLI, the search lab,
  and graph eval expose both; the snapshot gold gate now protects MRR, success@10, true recall@10, and
  nDCG@10, so losing relevant siblings or degrading the full graded ordering can fail CI even when one answer remains.

- The eval bench sheds the instruments the snapshot-bound gold files supersede. `evals/retrieval_judge.py`
  (pointwise LLM grading of production results — the gold miners now produce graded, corpus-grounded labels
  directly) and `evals/search_arena.py` (blind pairwise LLM duels as the defaults-promotion bar — the promotion
  bar is now a gold-file delta scored on both sides of the change, tuned against one file and confirmed against
  a held-out one) are deleted, along with their guard tests. `tests/test_reality_mechanisms.py` is pruned from
  26 tests to 11: the 15 ranking-preference orderings on synthetic flood corpora go (they were minted from a
  brainstormed edge-case list on the theory that making them pass would improve real search; when the code was
  changed to pass them, measured recall didn't move, and each hard ordering assertion constrained future
  ranking changes) — the 11 deterministic mechanism contracts stay (content-type indexing, MCP default-scope
  widening, reindex durability/stability, semantic scope filtering, cross-encoder gate/window/boundary
  plumbing). Ranking *quality* is now measured in exactly one place: the gold case files. First baseline over
  snapshot `9519fc4518e13ee7`: judged-cases (21) MRR 0.441 / S@5 0.619 / S@10 0.857 / nDCG@10 0.510;
  topic-cases-alpha (7) MRR 0.683 / S@5 1.000 / nDCG@10 0.641. `beir_eval.py` stays as the external yardstick.

- Semantic search no longer rebuilds the corpus vector pack on the request thread. The KNN matrix cache is
  keyed on a whole-store validity token, so continuous background embedding invalidated it every few minutes;
  the next query then read the full ~GB blob table, `np.vstack`'d the matrix, and wrote the pack — inline — and,
  unguarded, a burst of concurrent queries all rebuilt the same pack at once, blowing past MCP client timeouts.
  `_load_matrix` now serves the cached matrix immediately (stale is fine — the lexical arm covers the freshest,
  not-yet-repacked vectors), probes staleness at most once per cooldown, and rebuilds only in a single-flight
  background thread. Redaction can't wait out the cooldown, so it drops the matrix cache outright
  (`reset_matrix_cache`) — dead rows are never served, and the content is scrubbed at the source regardless. The
  corpus-graph refresh (`embed_graph.get`) gets the same cooldown so ingest can't make every search re-probe;
  its authoritative `build()` reads the live matrix directly. The per-query non-emptiness check in the semantic
  arm is an O(1) existence probe instead of a full `count(*)` scan.

- The vector pack is now a **base + delta** so the (now background) rebuild is cheap too. Previously any token
  move rebuilt the whole ~GB base pack — the full blob scan, `np.vstack`, and 814MB write — so continuous
  embedding rewrote it every few minutes. Now a large base pack (mmap, shared across processes) is reused as
  long as it's a clean prefix of the store, and the vectors written since ride along as a small in-RAM delta
  read fresh each build; a `_SplitMatrix` presents the two halves as one matrix to the KNN and the corpus graph.
  A single new vector costs a delta read, not a base rebuild — the full base pack is repacked only when the
  delta grows past `_DELTA_MAX_ROWS` (folding it in) or a delete below the base watermark makes the prefix dirty.
  The token read, base scan, and delta read share one DB snapshot, so a concurrent insert can never land a row
  in both halves or neither. `index_vectors` drops the pack metas only on an actual in-place upsert (an existing
  key re-written, invisible to the clean-prefix check), not on pure inserts, so ingest keeps the base reusable.
  A new parallel stress test asserts that N concurrent `thread_search` calls all serve from the warmed pack —
  none rebuilds on its request thread — and finish well within a bound.

- The CI `retrieval-gate` row no longer runs a from-log metric sweep: it now runs `retrieval_eval.py
  --probes-only --require-semantic --require-rerank` — model-arm liveness checks only. Click-label MRR is
  incumbent-censored (the gold is what the live ranker surfaced and the agent picked), so a per-commit number
  wearing the shape of a quality score invited misreading it as one; quality measurement moves to the
  snapshot-bound gold case files, scored deliberately (`evals/README.md` → "Taking a baseline"). With the
  cadence gone, the nightly's retrieval-trend ledger watcher (staleness + sliding-median alerts) is removed;
  the ledger remains, fed by explicit `--trend-out` runs. `--from-log` stays available as a hand-run collapse
  alarm and as the sampling frame of real query shapes for the gold miner.

- New CI `retrieval-gold-gate` row puts the grounded baseline on the per-commit path — the piece the probes-only
  `retrieval-gate` row deliberately left out. `scripts/retrieval_gold_gate.py` scores every snapshot-bound gold
  case file over its frozen snapshot (`~/.thread/archive-snap`) and fails the row on a drop below a calibrated
  floor. It is a **one-way floor, not a displayed score**: the click-label protocols stay off the per-commit path
  because they are incumbent-censored, but the gold files — grounded and graded — can ride CI as a regression
  ratchet, answering only "did search break below the baseline," never "is search good" (that stays a deliberate
  gold-delta measurement). A stale or absent fixture (snapshot reclaimed, or a gold mid-re-mine) skips that file
  rather than failing, so a maintenance window can't wedge the commit gate red; a freshly minted file rides
  ungated until it gets a floor. Initial floors, a few points under the first baseline over snapshot
  `9519fc4518e13ee7`: judged-cases MRR/S@10/R@10/nDCG@10 floors 0.40/0.80/0.70/0.46 (measured
  0.441/0.857/0.762/0.511); topic-cases-alpha 0.58/0.85/0.78/0.58 (measured 0.683/1.000/0.836/0.642);
  topic-cases-781e23b9d3d5 0.50/0.70/0.50/0.45 (measured 0.600/0.857/0.562/0.511).

- New `thread_archive snapshot <dest>` verb freezes the corpus into a self-contained, immutable archive home:
  it copies the JSONL truth (drain-consistent, under the truth-write lock) and materializes `index.db` beside it,
  restoring the embeddings from the copied vector sidecar without a re-embed. The result is an ordinary
  `THREAD_ARCHIVE_HOME` that any tool — the shipped `eval`, the dev bench under `evals/` — resolves via the
  environment. Because the frozen corpus can't grow underneath a measurement, search over a snapshot is
  deterministic: a regression gate or experiment run scored against one moves only when the code moves, and a
  mined gold can't be outranked by a thread that landed after mining (the isolation the `until` date bound was
  standing in for). `api.snapshot()` exposes the same op; `--vectors` embeds any gap the sidecar lacks, `--force`
  overwrites a non-empty destination, `--no-verify` skips the truth==index check. Each snapshot carries a
  `snapshot_id` — a content fingerprint of its corpus (`_ops.snapshot.corpus_fingerprint`) that reproduces on a
  plain re-snapshot but changes whenever the corpus does.
- Agent-mined retrieval golds are now bound to a corpus snapshot instead of a per-case `until` date bound.
  `retrieval_mine_gold.py` requires `THREAD_ARCHIVE_HOME` to be a snapshot, searches/reads that frozen corpus
  (no more server-side date bound), and stamps each case with the snapshot's `snapshot_id`. `retrieval_eval.py
  --cases` runs over that same snapshot and refuses any case whose `snapshot_id` doesn't match the home — a
  corpus that has moved on invalidates its golds loudly rather than scoring them against drifted data. The
  `evaluate()` `strict`/`until` plumbing and the `--strict` flag are gone (the snapshot subsumes them). Existing
  mined case files (which carry `until`, not `snapshot_id`) are invalid under the new binding and must be
  re-mined against a snapshot.
- Topic-based gold mining is now a committed script (`evals/topic_mine_gold.py`) instead of an ad-hoc agent
  process. It mints golds from a curated topic dense with confounds in two agent stages: a survey `claude` agent
  searches the topic, decides how many *angles* it warrants (its own call — no target count), and authors one
  query per angle with the intent, the confound subjects, and the candidate threads its searches found; then one
  labeler agent per angle takes those candidates as a starting pool, verifies and expands them with its own
  searches to find everything relevant, and grades a comprehensive pool (2=intended, 1=partial, 0=confound). The
  labeler builds on the survey's findings rather than rediscovering blind — the goal is the most complete gold
  set, and the labeler isn't the search system under test, so nothing leaks. Snapshot-bound like the query miner
  (requires a snapshot home, stamps each case with `snapshot_id`), resolves a topic by id or unique name, and
  writes the eval's `--cases` format (`topic-cases-<slug>.jsonl`) plus a facet-map/intent detail sidecar. The
  reusable headless-agent runner is factored into `retrieval_mine_gold.run_claude`, shared by both miners.
- Retrieval closes the last nine reality-mechanism goldens (formerly expected failures). The cross-thread
  duplicate fold is now a *near*-duplicate fold — `rank._norm_content` folds runs of digits to one placeholder
  before comparing, so a flood of threads differing only by a counter or run index (routine ops, re-asked
  questions, pending-todo restatements, injected boilerplate carrying a `task N`, a swarm of agents on one
  templated prompt) collapses to a single representative instead of filling the ranked window and burying the one
  terse or old authoritative thread the query wants. And the MCP `thread_search` default scope (user/title/summary),
  when it comes up dry, widens once to the whole transcript rather than to assistant text alone — so an answer that
  lives only in a tool result, a tool's error, or the assistant's reasoning is reachable; the widen keeps its result
  when it surfaces a strong match anywhere, so a low-weight tool/thinking hit counts even below a weak user hit.
- Retrieval closes seven ranking failure shapes (the reality-mechanism goldens, formerly expected failures):
  a duplicate-flood rescan folds byte-identical bursts to one representative per `(thread, content)` from a bounded
  rank window, so the distinct answer a fleet-of-copies buried still reaches the pool (and with it, a literal
  `frobnicate_widget` no longer loses to split-token prose); ranking term-matching is word-aware (`auth` stops
  scoring inside `author`, `cache` still credits `caches`); the reranker window centres on the densest term cluster
  and a long doc also offers its head and tail (MaxP), so an answer far from an incidental term is scored; its head
  reaches at least `limit` deep so a strong-but-sparse hit at the pool boundary is reachable; a verbatim query echo
  no longer stands the cross-encoder down, though its verdict is kept only when it rescues a lexically-weak
  (vocab-mismatch) hit rather than reshuffling confident ones; and the MCP default scope widens to assistant text
  whenever the top hit is below the strong-match bar, not only when no term landed.
- Python floor drops from 3.14 to 3.12: nothing in the code needs 3.14, so the install now runs on the Python
  most machines already ship. Classifiers, the CI matrix (3.12 floor + 3.14), the install-test container, and the
  four install docs follow.
- Install docs close three friction gaps: the macOS path checks for the Xcode Command Line Tools the base
  C-extensions (igraph/leidenalg/cryptography) need on a source build — the Ubuntu and Docker paths already install
  build-essential; and the README pitch plus both agent install docs now state that the clone's location is
  load-bearing — `.mcp.json`, the service units, and self-update bake its absolute path, so relocating it means
  re-running the wiring, not a plain `mv`.
- Docs repositioned around preservation as the product: search framed as the access layer, eval metrics and
  methodology move to docs/search-quality.md, the related-projects survey to docs/related.md; the README stops
  claiming macOS-only (Linux/systemd is real and CI-tested), counts 8 shipped harnesses (cloth is an operator
  plugin), links the Ubuntu install path, and documents the embed / mirror / eval verbs.

## 0.0.6 — 2026-07-21

- The `archive` script is gone; `thread_archive` is the one front door (setup + every verb); reinstall agents to repoint.
- Linux (systemd `--user`) support: launchd gives way to a platform-neutral backend registry; a new OS is now a drop-in.
- The archive no longer knows thread-librarian exists; the subjects lens moves in-tree, curated analytics move out.
- The search stack is tunable end to end via a frozen `SearchParams`; an `evals/` lab races configs on mined queries.
- Ranking gains a corpus-native coherence signal, live when the reranker stands down; embed/rerank models are injectable.
- `thread_archive eval`: a read-only search-quality self-checkup over your own archive; nothing leaves the machine.
- Web viewer: a header line naming a thread's subagents and a Playwright gate over the prod bundle; topic pages retire.
- A first-run test finds every harness's store in its default spot on macOS and Linux; Docker and package lanes gate it.
- Security hardening: config fails closed, a bare MCP is read-only, the viewer sends CSP; SECURITY/CONTRIBUTING land.
- Truth-format boundaries fail closed with migrate-before-restart; the backup mirror now deletes renamed ULID twins.

## 0.0.5 — 2026-07-20

- Thread ids are ULIDs (truth format v2); legacy integer ids resolve forever as aliases; a one-shot migration ships.
- Curation moves out: thread-librarian is its own repo and plugin; archive keeps the knowledge data plane and graph.
- Providers are pluggable: a public provider API (discovery, render policies, session-id shapes) the built-ins share.
- Installs self-update from release tags: daily probe, 48h soak, smoke check, rollback — pushing a tag is shipping.
- Import drift self-repairs: raw source mirror, unmodeled-field preservation, degradation verdicts, `archive fix-import`.
- Retrieval is measured on real usage: the log-mined click eval gates CI; the re-rank pays only on vocab-mismatch heads.
- FTS drops to external-content (~4 GB smaller), the semantic matrix mmaps; search gains browse, grouping, dup folds.
- Agent-run threads leave default search; `thread_read` grows ends/last/topics modes; images and attachments are viewable.
- The knowledge graph projects over the whole corpus (evidence edges, thread nodes), not the librarian's links alone.
- Importer dropped-field sweep over every provider; the amendment seam and backfills enrich already-stored history.

## 0.0.4 — 2026-07-16

- Self-curation runs on a schedule: `archive curate librarian|gardener` spawns bounded headless drains, installed as
  hourly/daily LaunchAgents; the librarian writes stored summaries again and the gardener gets MCP diagnostics.
- `archive restore` does real recovery (staged rebuild, atomic publication, `--generation`); backups sync a
  `.recovery/` bundle (config, keyring, retained exports), enforce owner-only mirrors, and fail on a stale bundle.
- The graph reads back out (`topic_get`/`topic_members`, curated topic pages, `topic_id` search scope); the viewer
  gains topics/hierarchy views, an all-threads page, search-hit deep links, hook rendering, last-activity ordering.
- Retrieval usage is ledgered with latency (ids only); the topical arm and the usage-mined golden eval are deleted.
- Capture health stops over-alarming; export redrops merge grown conversations and originals are never deleted;
  ChatGPT branch structure and claude.ai attachments reach truth; repair dumps are purged from git history.
- Privacy/hardening: 0700 homes, loopback-only MCP, pinned embed revision, 300s bulk write timeout, coverage floors.

## 0.0.3 — 2026-07-15

- Distribution moves to clone-install (the brief PyPI/Homebrew run is retired): `pip install -e .` from a clone or
  `pip install git+<repo-url>@vX.Y.Z`; releases are annotated `vX.Y.Z` tags cut per `docs/releasing.md`.
- `archive redact`: crypto-shredding redaction (reversible / escrowed / forgotten) across every store without breaking
  the truth log's shape, verify, or dedup; setup schedules the nightly backup (`archive daemon install --backup`).
- One shared MCP server (`archive-mcp --http` behind a LaunchAgent) replaces a ~3 GB resident model per stdio client.
- Search ~5× faster at identical results, quality CI-gated (MRR/recall floors) over a usage-mined golden set; the
  topic graph demoted to a `subjects:` lens; assistant text indexed; Codex turns get the real serving model.
- Capture blind-spot detection (skip ledger, watch-pass heartbeat, `archive coverage` reconciliation); ChatGPT exports
  import and zero-yield imports are quarantined, not deleted; SMB destinations work; ingest rides out FTS rebuilds.
- Hardening: viewer loopback guard, suite isolation + `tests/meta/` ratchets, `_truth`/`_ops` split, mypy gate.

## 0.0.2 — 2026-07-11

- First release published to PyPI (since retired — see 0.0.3): first-run setup wizard (discovers stores, imports with
  narration, wires watcher + MCP), a status view, and `config.json` choices every ingest path honors.
- Zero-daemon path: `archive-mcp` cohosts lazy catch-up ingest under an ingest-owner flock; `archive daemon` verbs
  manage the launchd watcher from the package.
- Public API narrowed to the retrieval MCP tools + the versioned truth format (`docs/format.md`); retrieval CLI verbs
  removed, the Python API privatized, all ratchet-pinned; version single-sourced from `__version__`.
- Nightly publishes a tier-aware verdict; verify hardening (fresh-connection quick_check, cross-store parity, named
  failure evidence); a content-hash cursor rewind and write-mutex mirror copies close three silent-loss paths.
- Boundary test lanes: wheel into a clean venv driven over MCP stdio, SIGKILLed-writer durability, provider goldens,
  frontend component tests, per-package coverage floors.

## 0.0.1 — 2026-07-11

- Retrieval correctness pass: the semantic arm sits out scoped/count/oldest searches, exclusions narrow the KNN scope,
  chunk pooling precedes the top-k cut, a bm25 OR-fallback serves conversational queries, `sort='oldest'` is in order.
- Crash-safe truth appends: drain intent journal, all-or-nothing batches, torn-tail repair, exclusive cross-process
  write locking, dedup enforced as a DB constraint.
- Reindex fails closed; backup hardened (atomic publishes, shrink guard, restore drills); `archive nightly` runs
  backup → verify → drill under a monitored heartbeat; `verify` grew tiers; `archive repair` restores red to green.
- One-time duplicate repair: ~27k duplicated turns collapsed, ~237k dedup keys backfilled; rebuilds reproduce the
  repair instead of undoing it.
- Retrieval overhaul: titles/summaries indexed as docs, long-document chunking, identifier-recall fixes, auto-widening
  MCP scope, an MRR/recall eval harness; the suite pinned model-free (~2 min → ~8 s), coverage on every CI sweep.

## 0.0.0 — initial release

- Serverless local archive over `~/.thread/archive`: append-only JSONL truth plus a rebuildable SQLite index.
- Multi-provider importers: Claude Code, Cursor, OpenCode, ChatGPT/Anthropic exports, Grok, cloth, and friends.
- Watcher daemon (`archive watch`) tails harness stores and ingests continuously.
- Retrieval MCP server: `thread_search` (FTS + optional semantic/rerank arms) and `thread_read`.
- Librarian MCP: topics, citations, thread links, and a review queue for knowledge curation.
- Read-only web viewer cohosted at `:8787` (`archive watch --web`).
- CLI: import/export, reindex, checkpoint, backup, verify.
