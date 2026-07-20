# Security

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub security advisories](https://github.com/ellamental/thread_archive/security/advisories/new)
— please don't open a public issue for anything exploitable. Reports get a
response as fast as a one-maintainer project allows; fixes ship as a normal
release (see the update model below for how fast a fix reaches installs).

## Trust model

thread-archive is single-user, single-machine, local-only. There is no server
component, no account, no telemetry, and nothing listens beyond loopback. The
threat model is correspondingly narrow, and these are its load-bearing walls:

- **The web viewer is unauthenticated full read of the archive.** It binds
  `127.0.0.1` only, rejects non-loopback `Host` headers (DNS-rebinding
  defense), and refuses a non-loopback bind unless
  `THREAD_ARCHIVE_WEB_NONLOCAL=1` is set deliberately. If you tunnel or proxy
  it, you are the authentication layer.
- **The MCP surface is read-only.** The one server this package ships
  (`thread_search` / `thread_read`) cannot mutate the archive — a client wired
  to it can search and read, never write.
- **Archive content is untrusted input to whatever reads it.** Search results
  and thread reads return conversation text verbatim — text that originally
  came from models, tools, and web content. An agent consuming
  `thread_search` output should treat it like any other retrieved document:
  data, not instructions. The archive never executes archived content itself.
- **`archive fix-import` spawns a coding agent on your machine.** The spawn is
  permission-scoped, not sandboxed: file edits auto-approve only inside the
  patch scaffold, its shell allowlist is the repair protocol's commands
  (`python`/`pytest`/`archive`), and no MCP servers are injected — but running
  the scaffold's tests is still running code, and the samples it reads are
  transcript data. A patch can only go live through the deterministic
  activation gate (its test suite passing in a fresh subprocess). Don't run
  `fix-import` if you don't want an agent executing in that scope.
- **Redaction is crypto-shredding.** `archive redact` re-encrypts content
  under a fresh per-redaction AES-256-GCM key in `<home>/keyring.json`;
  destroy the key (or escrow it off-machine) for erasure. The provider's own
  store keeps its original copy — redaction covers the archive, not the
  source.

## The self-update mechanism

Installs cloned from git run `archive self-update` daily by default: it
fast-forwards the clone to the newest release tag once the tag is 48 hours
old, reinstalls, smoke-checks, and rolls back on failure. Its trust anchor is
transport security to the git remote you cloned from — the same trust the
install itself made. There is no signature layer, so repository compromise is
the supply-chain risk to weigh: a malicious tag would reach unattended
installs after the soak window. Mitigations: the 48-hour soak is the yank
window, the updater never crosses a truth-format bump unattended, never
touches a tree with local changes, and `{"update": {"enabled": false}}` in
`config.json` turns the mechanism off entirely (updates then happen only when
you run `archive self-update` yourself).
