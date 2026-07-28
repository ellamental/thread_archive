# Releasing thread-archive

Distribution is the git clone (an editable install from a checkout, or
`pip install "git+<repo-url>@vX.Y.Z"`) and PyPI
(`pip install thread-archive`). A release is a pointer: compress the
changelog, bump the version, one release commit, and an annotated tag pushed
to GitHub. The tag is what a clone pins and fast-forwards to, what
`thread-archive status` / bug reports correlate against — and what triggers
the PyPI upload (`.github/workflows/publish.yml`, via Trusted Publishing).

**Pushing the tag is the point of no return.** A clone gets the release when it
runs `thread-archive self-update` (`--check` is how it sees one exists). That
is a delay, not a safety net: the release is offered to every install the
moment the tag lands, and the preflight below is the only gate between a bad
release and the first operator who reaches for it. This machine's clone runs
ahead of consumers, so a bad release should hurt here first.

The version's single source of truth is `__version__` in
`src/thread_archive/__init__.py`; pyproject declares `version` dynamic and
hatch reads it from there. Nothing else carries the number.

## 0. The repo is release infrastructure — keep it hardened

Release tags are executable software offered to every installed clone, and a
`self-update` fast-forwards to whatever the newest one contains. The GitHub
repo's protections are therefore part of the release mechanism, not optional
hygiene. The standing
requirements: two-factor auth on every account that can push, a tag protection
rule covering `v*` (nobody but the release path can create or move release
tags), and branch protection on `main`.

## 1. Preflight — the tree must already be releasable

- Full suite green: `.venv/bin/pytest tests/`.
- The package lane green: `.venv/bin/pytest -m package --no-cov
  tests/test_package_artifact.py` — builds the wheel + sdist with
  `python -m build`, proves their contents, installs the wheel into a clean
  venv, and runs the real entry points. Nothing is uploaded anywhere; this is
  the gate that proves a fresh-clone install actually works (files present,
  console scripts wired), rather than only the long-lived editable install.
- GitHub CI green on `main` (ruff, mypy, coverage floor, the pytest suite on
  the 3.12 floor and 3.14, frontend checks, and the same package lane).
- The search-quality gate green: `python -m search_lab gate --run`. The bench's
  numbers against the accepted ones in `search_lab/quality-baseline.json` —
  public benchmarks whose labels somebody else made, so a breach is real
  evidence the retrieval components got worse. `--run` measures whatever the
  code has invalidated first: rows unchanged since their last run are fresh and
  cost milliseconds, a ranking edit re-runs the set (a few hours cold). A row
  last measured at other code fails as **stale** rather than passing on old
  numbers — shipping an unmeasured ranking change under a green gate is the
  failure this exists to prevent. See `search_lab/README.md` for the tiers below
  it and what each one licenses.

  A breach is a decision, not a formality. Either it is a regression — fix or
  revert — or it is a deliberate trade, accepted with
  `python -m search_lab gate --update`, which puts the movement in the release
  diff where a reader can see what was given up.
- If `frontend/` changed since the last release, the committed
  `_web/static/` bundle must be current: `cd frontend && npm run build`,
  and the regenerated static assets committed with the change that caused
  them — a clone install ships whatever bundle is in the tree.

## 2. Compress the changelog

`CHANGELOG.md` accumulates verbose in-flight entries under `## Unreleased`
while work happens — each written for reviewers of that day's change, dates
and narration included. Releasing rewrites them for readers of the release:

- Retitle `## Unreleased` to `## X.Y.Z — YYYY-MM-DD`.
- Compress hard: **10 lines per version, hard maximum**, wrapped at **120
  characters** — every body line under the version heading counts (blank lines
  don't). An 11th line is not "about 10"; compress further, it always fits.
  One tight bullet per theme (group related in-flight entries), present tense,
  no per-bullet dates, no thread references. Drop pure-hygiene noise that no
  user or operator will ever act on. The full stories live in git history and
  the conversation archive; the changelog is the index, not the record.
- Open a fresh empty `## Unreleased` above it.

## 3. Version bump

Pick the number (semver; pre-0.1.0, all/breaking changes bump the minor). Edit
`__version__` in `src/thread_archive/__init__.py`. If the truth-directory
layout changed incompatibly, the format version in `docs/format.md` moves on
its own rules — that is a separate, deliberate decision, not part of the
package bump.

## 4. Release commit + tag

One commit containing exactly the changelog compression and the version bump:

```
Release X.Y.Z: compress changelog, bump version
```

Then an annotated tag on it, and push both. The push is the ship:

```bash
git tag -a vX.Y.Z -m "thread-archive X.Y.Z — <one-line theme of the release>"
git push origin main vX.Y.Z
```

The tag push also triggers the Publish workflow, which builds the wheel +
sdist on the runner and uploads them to PyPI via Trusted Publishing (no
tokens; PyPI trusts the repo/workflow/environment tuple configured under the
project's Publishing settings on pypi.org). Watch the run — a publish failure
means the tag exists but PyPI lags it, and the fix is a fixed vX.Y.(Z+1),
since PyPI refuses re-uploads of a once-seen version even after deletion.

## 5. Verify from the outside

Prove the release installs from the tag, not just from this checkout's
long-lived venv:

```bash
python3 -m venv /tmp/ta-verify
/tmp/ta-verify/bin/pip install "git+ssh://git@github.com/ellamental/thread_archive.git@vX.Y.Z"
/tmp/ta-verify/bin/thread-archive --help
```

And once the Publish run is green, from PyPI (the index can lag the upload by
a minute or two):

```bash
/tmp/ta-verify/bin/pip install --force-reinstall "thread-archive==X.Y.Z"
/tmp/ta-verify/bin/thread-archive --help
```

## 6. Roll the local deployment

The daemons on this machine run from the clone's editable install, so being
on the release commit *is* the deployment — with two follow-throughs:

- If dependencies or entry points changed, re-run `.venv/bin/pip install -e .`
  (editable installs pick up code automatically, not metadata).
- Restart whatever loaded the old code: `thread-archive service restart` for the
  watcher/backup agents; MCP clients pick up the new server on their next
  session.

## Yanking a bad release

Delete the bad tag as soon as possible:

```bash
git push origin :refs/tags/vX.Y.Z     # delete the remote tag
```

That removes the version from future self-update checks. It does not heal an
install whose operator already applied it, nor remove a tag a check already
fetched locally. On PyPI, yank the release (project → release → Options →
Yank): resolvers stop picking it for fresh installs, but a `==X.Y.Z` pin still
gets it, and the version number is burned — PyPI never accepts a re-upload of
it. Always follow with the real fix:

```bash
# fix, then release vX.Y.(Z+1) normally
```

The higher tag outranks the lingering bad one everywhere, fetched or not. A
truth-format bump still cannot be rolled back after the newer writer touches
the archive, which is why applying one requires an explicit flag.

## Who runs this

The agent, end to end — preflight, changelog compression, version bump,
release commit, tag, push, verification, and rolling the local deployment.
The release commit + tag are part of the release process the agent is
executing (an explicit exception to any standing no-commit convention in the
operator's environment). Asking for release means asking for all of it.
