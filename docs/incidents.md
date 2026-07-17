# Reality-integrity incident catalogues

`archive incidents <catalogue.jsonl>` replays recorded search failures as
permanent guards against the live archive — read-only, exit `0` when every
guard holds, `1` on any breach, `2` for a catalogue the harness refuses.

## The principle

When the operator is confident a conversation exists and search can't find it,
the correct defaults are (1) the phrasing is off — try variations — or (2) the
search is broken or not indexing that content; **not** "you're misremembering."
A false "not found" against a high-confidence memory is the archive telling its
operator that part of their own life didn't happen.

The catalogue turns that principle into regression tests: the day search fails
you, record the incident — the original failing query, the conversations it
should have surfaced — and the archive must pass it forever. The bar is
deliberately low ("the agent can see it at all", top-25 by default), because
each guarded case was once answered with "not found" against a conversation
that was right there.

## Why the cases aren't shipped

A catalogue quotes real queries, real thread ids, and how sure you were at the
time — it is exactly as personal as the archive itself. The product ships the
harness (tiers, pass conditions, runner) and this format; the cases are user
data, kept wherever you keep private things and pointed at by path. The
public test suite carries the *mechanisms* instead
(`tests/test_reality_mechanisms.py`): the failure shapes behind real
incidents, reproduced on synthetic corpora.

## Format

JSONL, one incident per line. Blank lines are allowed; anything else invalid
refuses the whole file (a typoed field must fail loud, not silently weaken a
guard). Slugs must be unique.

```jsonl
{"slug": "mill_pond", "tier": "recall", "query": "heron mill pond", "must_surface": [3704990, 3706568], "confidence": "certain — I know I had this conversation", "original_failure": "OR-split flood: 'mill' matched thousands of hits and the real thread sank"}
{"slug": "tool_calls_visible", "tier": "capability", "query": "stock_lookup", "content_type": "tool", "must_surface": [3180339], "original_failure": "tool-call events weren't indexed; search couldn't confirm the tool was ever used"}
{"slug": "distrust_conversation", "tier": "record", "query": "dump the database and grep by hand", "must_surface": [3189162], "original_failure": "accumulated misses drove the operator to bypass search entirely"}
{"slug": "unfound_moment", "tier": "unresolved", "query": "impossible to unlearn", "must_surface": [3705399], "notes": "the phrase was never located — open until found or ruled misremembered"}
```

### Fields

| field | required | meaning |
| --- | --- | --- |
| `slug` | yes | unique name for the incident |
| `tier` | yes | `recall` \| `capability` \| `record` \| `unresolved` (below) |
| `query` | yes | the original failing query (`recall`/`capability`) or a distinctive phrase from the record (`record`/`unresolved`) |
| `must_surface` | tier-dependent | thread ids that satisfy the guard: `recall` needs ≥ 1 (any-of); `record`/`unresolved` exactly 1 (the record itself); optional + informational for `capability` |
| `content_type` | no | narrows the search (e.g. `"tool"`) |
| `confidence`, `original_failure`, `notes`, `thread` | no | human-stakes annotations, echoed when a guard breaches (`thread`: the id of the conversation where the incident happened) |

### Tiers — what each can honestly assert

- **`recall`** — a lost-conversation bug that was fixed. The original failing
  query, run globally, must surface at least one `must_surface` thread in the
  top `--limit` (default 25) hits. Not "rank it first" — "see it at all."
- **`capability`** — a whole content-type was invisible. The query must return
  *any* hits; pinning one specific old thread's rank would be flaky under
  recency weighting. The real cliff is zero → many.
- **`record`** — the incident's own record. The phrase is searched
  *thread-scoped* against the one `must_surface` id: "is this still indexed",
  not "does it win global ranking". A reality-integrity system must never lose
  the record of its own failures.
- **`unresolved`** — a memory sought but never yet located. Guarded exactly
  like `record` (its record must stay findable) and reported as *open* so it
  can't be silently forgotten. It graduates to `recall` the day the memory is
  found, or leaves the catalogue if ruled never-said.

## Running

```bash
archive incidents ~/private/incidents.jsonl              # exit 0 green / 1 breach
archive incidents ~/private/incidents.jsonl --limit 40   # widen the recall window
```

Wire it into whatever sweeps your machine (cron, a CI row) the same way as
`archive verify` — it is read-only and cheap. For richer per-case assertions
(pytest messages, xfail tracking of an unresolved phrase), a private test
suite can import the same seams the verb uses: `load_catalogue`, `evaluate`,
and `archive_search` in `thread_archive._evals.incidents` — private module,
same stability caveats as the rest of the internals.
