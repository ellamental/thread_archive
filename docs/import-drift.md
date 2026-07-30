# When an import drifts

Providers change their on-disk formats without notice, and no maintainer runs a
harness that exercises every variation at every provider release. Archive's
answer is a support tier plus a repair loop, not a promise nobody can keep:

- **Claude Code is first-class.** Its parser carries the full drift ledger
  (validators, residual preservation, the version tripwire), so its worst
  failure mode is *soft*: content is preserved — unmodeled fields ride along
  under `annotations`, skipped files land on the audit ledgers — but partially
  modeled until fixed. Everything else is best-effort: same machinery where it
  reaches, community-maintainable via the plugin API.
- **Drift is loud.** The skip and validation ledgers plus the nightly coverage
  check produce per-source *degradation verdicts* (`thread-archive source coverage` prints
  them; the MCP search notice prepends a one-liner naming the remedy the next
  time you search, which is the moment you care).
- **Preservation doesn't wait for the fix.** A degraded source's recently
  active raw files are snapshotted into `dumps/drift/<source>/` — bounded,
  incremental, never auto-deleted — so a fix that comes months later can still
  recover everything the provider has since pruned.
- **`thread-archive source recheck <provider>` is the first move.** It re-reads
  exactly the files the ledgers named, through the parser as it stands now.
  Content the old parser missed lands, and records that come back clean are
  *closed* — which is what retires the verdict, since a ledger is append-only and
  a repair must never erase the drift it repaired. Closing is by re-read, not by
  assertion: the stamp is taken before the re-parse, so findings the re-parse
  itself records stay open and the source stays degraded. Run it after any
  upgrade that claims a parser fix — that is a repair with no patch involved, and
  otherwise nothing retires a verdict your upgrade already fixed. If the findings
  come back, the drift is live and the patch loop below is next.
- **The user's own agent writes the fix.** `thread-archive source fix <provider>`
  scaffolds an override patch under `<home>/plugins/` (module, tests, collected
  samples, drift evidence, per-provider quirk notes, and a `PROTOCOL.md`
  written to be handed to an agent), leaving one job open: the parse logic.
  Archive runs no agent itself — you work the scaffold, or point yours at it
  under whatever scope you choose, remembering that the samples are transcript
  data an agent should treat as untrusted input (see
  [SECURITY.md](../SECURITY.md)).
  Activation is deterministic — the scaffold's tests must pass in a fresh
  subprocess (including a dedup re-import guard) before the override is enabled
  and the ledger-driven re-import recovers the gap.
- **Patches are temporary by default.** The next `thread-archive self-update`
  retires them (a core release is the proper fix's vehicle; if drift persists,
  the notice re-fires and the fix re-runs against the new core). `thread-archive source fix
  <provider> --pin` keeps yours forever. Every lifecycle step is audited in
  `patch-log.jsonl`, and `thread-archive source list` shows `patched` / `patched
  (pinned)` state.
