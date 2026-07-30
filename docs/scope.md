# Not supported

The scope is deliberately narrow. These are design decisions, not gaps waiting
on a release:

- **More than one machine — and merging archives.** An archive belongs to one
  machine. There is no merge tool, no sync, and no federated search across
  archives — a deliberate single-machine scope, not a technical wall. Thread
  ids are globally-unique ULIDs, so two archives never collide there; what a
  merge would still have to reconcile is the locally-minted **event id** space
  wired through the truth layer — the append-only event log, causality links,
  amendment records — by remapping one archive's ids past the other's.
  Mechanical, but unbuilt. *Moving* an archive to another machine is supported
  — carry the directory, or `thread-archive backup restore <mirror> --to <home>`;
  running two and reconciling them later is not.
- **Anything but macOS and Linux.** The always-on pieces — watcher, scheduled
  backup, shared MCP server — are launchd LaunchAgents on macOS and systemd
  `--user` units on Linux. Public CI runs on Linux, including the systemd
  lifecycle against a real user manager. The launchd lifecycle is not something
  a hosted macOS runner can exercise — it drives the `gui/<uid>` domain, which
  needs a login session no hosted runner has — so it is verified on the
  maintainer's machine instead: macOS has the most mileage in daily use and the
  least in public CI. Windows is not supported, and no other platform exists
  here.
- **More than one user.** No accounts, no authentication, no per-user scoping.
  Everything that serves the archive binds loopback and assumes whoever reaches
  it owns all of it — the shared MCP server, and the web viewer where a clone
  runs one.
- **Live capture of web chats.** claude.ai, ChatGPT, and xAI arrive from account
  exports you download by hand — drop the ZIP into `<home>/dumps/`, or run
  `thread-archive source import-account` (from a clone, the viewer's `/upload`
  page is the same drop folder with a browser in front of it). The self-feeding
  path is the local agent harnesses.
- **Driving a conversation.** The archive preserves and retrieves. It never
  writes back to a harness store and never sends a message.
