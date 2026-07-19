# Provider format quirks

No provider-specific quirk notes exist for this source yet. The general shape
of the archive's import contract still binds:

- **Line-stream providers** (one JSONL transcript per session): the importer is
  watermark-incremental per `(source, source_id)`; the source_id derivation
  from the file path is identity — never change it in a fix. A torn final line
  is the normal state of a live transcript; importers skip it and keep the
  complete lines.
- **DB-scan providers** (one live SQLite store): the scanner re-reads changed
  DBs; per-conversation watermarks make it incremental. Schema drift usually
  breaks these *hard* (zero rows), so the fix is typically query/schema
  mapping, verified against a snapshot copy of the DB.
- **Drift ledgers**: if the provider has a `ProviderConfig`, its
  `expected_*`/`known_*` sets are what the validators diff against. New block
  types, roles, line kinds, or fields are often fixed entirely by a
  `derive()` extension — prefer that over parser code.
- **Preservation over interpretation**: unknown line kinds should be preserved
  (unmodeled/`unknown_line`) rather than dropped; residual fields land under
  `annotations["unmodeled"]`. A fix that silently drops what it doesn't
  understand is worse than the drift it fixes.
- **Dedup identity is sacred**: event dedup keys are derived from provider ids
  and content; the scaffold's re-import test fails a fix that shifts them.

When you learn something durable about this provider's format, append it here —
this file rides with the scaffold and the next repair reads it first.
