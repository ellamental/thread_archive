# Security

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub security advisories](https://github.com/ellamental/thread_archive/security/advisories/new)
— please don't open a public issue for anything exploitable. Fixes ship as a
normal release; the update model below is how fast one reaches an install.

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
  Host. So a write also requires a loopback `Origin` and an `X-Archive-Write`
  header — unsettable by a form, which forces a preflight this server never
  answers. What lands is bounded too: a `.zip` under a sanitized name, refused
  unless a registered provider's `detect` claims it, refused before spooling if
  it would not leave a gigabyte of disk free, and refused while being read if any
  member expands past a ceiling and a compression ratio — an upload is judged on
  what it decompresses to, not on what it weighs or on what its header claims.
- **The HTTP MCP transport rejects a non-loopback `Host`.** `archive-mcp --http`
  is the same unauthenticated full read as the viewer, over one loopback port, so
  it carries the same DNS-rebinding defense — built from the host actually being
  bound, and off only under the deliberate `THREAD_ARCHIVE_MCP_NONLOCAL` opt-in,
  where this server can no longer know the names it is legitimately reached by.
- **The MCP tools are read-only, and the process defaults to read-only.** The
  one server this package ships exposes only `thread_search` / `thread_read`,
  plus `thread_help` (their manual) — all three read-only.
  `THREAD_ARCHIVE_MCP_INGEST=1` is a separate, explicit process-level opt-in to
  local catch-up ingestion; setup-generated stdio entries set it when the
  always-on watcher is skipped. The shared MCP LaunchAgent pins it off unless
  installed with `thread-archive service install --mcp --mcp-ingest`; leave it off when
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
- **The code axis runs `git` in directories it did not choose.** Resolving a
  commit to the sessions that built it means reading the repository, so
  `thread_search`'s `commit` scope shells out to read-only `git` — in the
  directory a caller passed as `repo=`, or, without one, in the working
  directories archived sessions recorded. Both are caller- or content-named, so
  the tools are read-only with respect to *the archive*, not with respect to the
  machine: they will tell you whether a given path is a repository and what is in
  it. Git takes instructions from the config of whatever repository it is pointed
  at, so the keys that can name a command to run are pinned off on the command
  line, which outranks any config file, and a path that is not an existing
  directory is refused without spawning anything.
- **`thread-archive source fix` collects your transcripts into a scaffold.** It copies
  real drifted source files into `<home>/plugins/<provider>/samples/` so the
  fix can be diagnosed against them — private conversation content, sitting in
  a directory you will likely point an agent at. Archive itself runs no agent
  and executes nothing from those samples; `--activate` runs the scaffold's own
  test suite in a fresh subprocess, which is the only path a patch has to going
  live. If you hand the scaffold to an agent, the samples are untrusted input
  to it, and its blast radius is whatever scope you grant it.

## The update model

Nothing updates itself, in either install shape. No install probes for
releases on its own, none applies one, and no configuration turns unattended
apply on. When an update happens, you ran the command.

**A packaged install** (`pip install thread-archive`) updates with
`thread-archive self-update`, which is `pip install -U thread-archive` with
guardrails: `--check` resolves the newest release and reports it without
installing anything, and applying downloads that one wheel, gates it, installs
it, smoke-checks the result, and rolls back to the running version on failure.
Its trust anchor is PyPI plus transport security to it. The artifacts are built
and uploaded by this repo's own Publish workflow, triggered by the release tag,
authenticated to PyPI by Trusted Publishing (OIDC) — no long-lived API token
exists to leak or rotate. PyPI records a published attestation for each file,
binding it to that workflow and tag; that is a check available to you, not one
this updater performs — what self-update trusts is pip's transport to the index,
and nothing here verifies a signature. A compromise of the repo or the tag still
reaches you, which is the risk the attestation would not cover either — applying
a release is the exposure, and its timing is yours to choose. The updater never
crosses a truth-format bump without an explicit flag, and it reads that gate
out of the wheel it is about to install rather than off any other artifact.

**A tool-managed install** (`uv tool`, `pipx`) is its manager's to move:
self-update detects the receipt beside the environment and names that manager's
own upgrade command instead of driving pip inside it.

**A source clone** updates through git — fetch, check out the newer release
tag, reinstall, restart the agents. Its trust anchor is transport security to
the git remote you cloned from, the same trust the install itself made; there
is no signature layer on that path. Self-update does not touch a clone: its
code is the checkout, and what moves the checkout is you.
