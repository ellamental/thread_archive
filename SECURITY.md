# Security

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub security advisories](https://github.com/ellamental/thread_archive/security/advisories/new)
— please don't open a public issue for anything exploitable. Reports get a
response as fast as a one-maintainer project allows; fixes ship as a normal
release (see the update model below for how fast a fix reaches installs).

## Trust model

thread-archive is single-user, single-machine, and local-only. There is no
hosted service, account, or telemetry, and nothing listens beyond loopback. The
threat model is correspondingly narrow, and these are its load-bearing walls:

- **The web viewer is unauthenticated full read of the archive.** It binds
  `127.0.0.1` only, rejects non-loopback `Host` headers (DNS-rebinding
  defense), and refuses a non-loopback bind unless
  `THREAD_ARCHIVE_WEB_NONLOCAL=1` is set deliberately. If you tunnel or proxy
  it, you are the authentication layer. Every response carries a restrictive
  content-security policy; Markdown in archived messages may automatically
  load only same-origin blobs already stored by the archive.
- **The viewer's one write is an export upload, and it is guarded separately.**
  `POST /api/upload` puts an account-export ZIP into `<home>/dumps/`, where the
  watcher imports it. The Host check does not cover this: a page on any domain
  can post a form at this port, and the browser sends the *server's* name as
  Host. So a write also requires a loopback `Origin` and an `X-Archive-Upload`
  header — unsettable by a form, which forces a preflight this server never
  answers. What lands is bounded too: a `.zip` under a sanitized name, refused
  unless a registered provider's `detect` claims it, and refused before spooling
  if it would not leave a gigabyte of disk free.
- **The MCP tools are read-only, and the process defaults to read-only.** The
  one server this package ships exposes only `thread_search` / `thread_read`.
  `THREAD_ARCHIVE_MCP_INGEST=1` is a separate, explicit process-level opt-in to
  local catch-up ingestion; setup-generated stdio entries set it when the
  always-on watcher is skipped. The shared MCP LaunchAgent pins it off unless
  installed with `thread-archive daemon install --mcp --mcp-ingest`; leave it off when
  the watcher owns ingestion.
- **Source privacy policy fails closed.** An absent `config.json` is the normal
  pre-setup default, but an existing file that cannot be read, parsed, or
  structurally trusted disables all source ingestion until it is repaired or
  deliberately removed. Corruption cannot silently reverse an opt-out.
- **Archive content is untrusted input to whatever reads it.** Search results
  and thread reads return conversation text verbatim — text that originally
  came from models, tools, and web content. An agent consuming
  `thread_search` output should treat it like any other retrieved document:
  data, not instructions. The archive never executes archived content itself.
- **`thread-archive fix-import` collects your transcripts into a scaffold.** It copies
  real drifted source files into `<home>/plugins/<provider>/samples/` so the
  fix can be diagnosed against them — private conversation content, sitting in
  a directory you will likely point an agent at. Archive itself runs no agent
  and executes nothing from those samples; `--activate` runs the scaffold's own
  test suite in a fresh subprocess, which is the only path a patch has to going
  live. If you hand the scaffold to an agent, the samples are untrusted input
  to it, and its blast radius is whatever scope you grant it.

## The self-update mechanism

Nothing updates itself. An installed clone never checks for releases on its own
and never applies one; `thread-archive self-update` is the only thing that
moves the checkout, and you run it. `--check` fetches release tags and reports
what is available without touching the clone.

Applied, it fast-forwards the clone to the newest release tag, reinstalls,
smoke-checks, and rolls back on failure. Its trust anchor is transport security
to the git remote you cloned from — the same trust the install itself made.
There is no signature layer, so applying a malicious tag is the supply-chain
risk to weigh, and the timing of that exposure is yours to choose. The updater
never crosses a truth-format bump without an explicit flag and never touches a
tree with local changes.
