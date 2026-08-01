# Fix a drifted provider import

This directory is a patch scaffold for a thread-archive provider whose import
has drifted: the provider changed its on-disk transcript format, and the
archive's parser no longer fully understands it. The job is the parse logic;
everything else — where files go, how the fix is verified, how it goes live —
is already decided by the scaffold and the activation gate.

Work through it yourself or point an agent at it; the protocol below is
written to be handed to one.

## Ground rules

- **Work only inside this directory.** The one exception: running `thread-archive`
  commands named in this protocol. Never edit the thread-archive source tree —
  an edited core is lost the moment the install is updated, and it blocks a
  clone's own update; an override plugin here survives both.
- **Never weaken the pre-wired tests.** They are the exit bar. In particular
  the watermark-reset re-import test guards dedup identity — the one way a fix
  corrupts an archive instead of degrading it. Add tests freely.
- **Fixtures must be obfuscated.** Derive them from `samples/` with structure,
  keys, ids, timestamps, and block shapes intact, but replace all free text
  (user prose, assistant prose, tool output contents) with placeholder words.
  Samples are the user's private conversations; fixtures are lasting test
  artifacts. No personal content crosses that line.
- **Done means green, then activated.** Do not stop at a diagnosis or a
  plausible patch. If you cannot make the suite pass, say exactly what blocks
  you in your final summary — a red suite with an honest story beats a
  weakened test.

## Protocol

1. **Read the evidence.** `evidence.md` digests the drift ledgers, coverage
   verdict, provider versions first-seen, and quarantine snapshots. `quirks.md`
   carries the format knowledge previous work on this provider accumulated —
   read it before forming theories.
2. **Diagnose from samples.** Open the files in `samples/` (ledgered failures
   first) and find what actually changed: a new block type, a renamed field, a
   new line kind, a moved store. Compare against what the parser expects per
   `quirks.md`. State the drift precisely before writing any code.
3. **Derive fixtures.** Build the smallest obfuscated fixtures into `fixtures/`
   that reproduce the drift — plus the standing interesting shapes: a tool call
   with its result, a thinking block where the format has them, an unknown
   field, and (for line-stream formats) a torn final line.
4. **Implement the smallest fix** in the patch module (`patch_*.py`). The
   module's comments rank the shapes: a parser-config `derive()` for ledger
   drift beats a parser subclass; a parser subclass overriding one method beats
   rewriting; a watcher replacement is only for a moved store. Keep
   `PROVIDER.name` unchanged.
5. **Iterate until green:** `python -m pytest . -q`. The suite must pass with
   your fixtures importing events, no validation findings, and no dedup
   duplication on re-import.
6. **Activate:** run `thread-archive source fix <provider> --activate`. This re-runs
   the suite in a fresh subprocess, enables the override in config.json, and
   re-imports everything the broken parser consumed (ledgered files, plus
   quarantine snapshots whose originals were pruned). If activation refuses,
   fix what it names and repeat.
7. **Summarize** in a few lines: the drift, the fix shape, fixture coverage,
   and the re-import counts activation reported. If anything about the format
   surprised you, append it to `quirks.md` — the next repair starts from what
   you learned.
