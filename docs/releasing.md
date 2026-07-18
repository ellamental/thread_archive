# Releasing thread-archive

Distribution is a git clone: the clone is the install (`pip install -e .`
into the clone's venv — see the README's Install section). There is no
package registry. A release is therefore a *pointer*, not an upload:
compress the changelog, bump the version, one release commit, an annotated
tag pushed to GitHub. The tag is what a consumer can pin and what
`archive status` / bug reports can be correlated against.

**Pushing the tag ships it.** Installs run auto-update by default (the
watcher's daily `archive self-update`): once a pushed tag is 48 hours old
(the soak window), every clean consumer clone fast-forwards to it, reinstalls,
and restarts its daemons — unattended. The preflight below is therefore the
release gate, not a formality, and the soak window is the yank window (see
"Yanking a bad release"). This machine's clone runs ahead of every consumer,
so a bad release should hurt here first.

The version's single source of truth is `__version__` in
`src/thread_archive/__init__.py`; pyproject declares `version` dynamic and
hatch reads it from there. Nothing else carries the number.

## 1. Preflight — the tree must already be releasable

- Full suite green: `.venv/bin/pytest tests/`.
- The package lane green: `.venv/bin/pytest -m package --no-cov
  tests/test_package_artifact.py` — builds the wheel + sdist with
  `python -m build`, proves their contents, installs the wheel into a clean
  venv, and runs the real entry points. Even without a registry this is the
  gate that proves a fresh-clone install actually works (files present,
  console scripts wired), rather than only the long-lived editable install.
- GitHub CI green on `main` (ruff, mypy, coverage floor, the pytest suite on
  3.14, frontend checks, and the same package lane).
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

Pick the number (semver; pre-1.0, breaking changes bump the minor). Edit
`__version__` in `src/thread_archive/__init__.py`. If the truth-directory
layout changed incompatibly, the format version in `docs/format.md` moves on
its own rules — that is a separate, deliberate decision, not part of the
package bump.

## 4. Release commit + tag

One commit containing exactly the changelog compression and the version bump:

```
Release X.Y.Z: compress changelog, bump version
```

Then an annotated tag on it, and push both:

```bash
git tag -a vX.Y.Z -m "thread-archive X.Y.Z — <one-line theme of the release>"
git push origin main vX.Y.Z
```

## 5. Verify from the outside

Prove the release installs from the repo, not just from this checkout's
long-lived venv:

```bash
python3 -m venv /tmp/ta-verify
/tmp/ta-verify/bin/pip install "git+https://github.com/ellamental/thread_archive.git@vX.Y.Z"
/tmp/ta-verify/bin/archive --help
```

## 6. Roll the local deployment

The daemons on this machine run from the clone's editable install, so being
on the release commit *is* the deployment — with two follow-throughs:

- If dependencies or entry points changed, re-run `.venv/bin/pip install -e .`
  (editable installs pick up code automatically, not metadata).
- Restart whatever loaded the old code: `archive daemon restart` for the
  watcher/backup agents; MCP clients pick up the new server on their next
  session.

## Yanking a bad release

Two moves, both within the 48h soak window if at all possible:

```bash
git push origin :refs/tags/vX.Y.Z     # delete the remote tag
```

That stops every install that has not fetched it yet — which, inside the soak
window, should be all of them. It does **not** heal a clone that already
fetched the tag (auto-update fetches without pruning, so a deleted remote tag
lingers locally). So always follow with the real fix:

```bash
# fix, then release vX.Y.(Z+1) normally
```

The higher tag outranks the lingering bad one everywhere, fetched or not.
A truth-format bump can never be yanked back across — auto-update refuses to
cross one unattended for exactly that reason.

## Who runs this

The agent, end to end — preflight, changelog compression, version bump,
release commit, tag, push, verification, and rolling the local deployment.
The monorepo's no-commit rule does not apply here: `archive/` is its own
repository, and the release commit + tag are part of the release process the
agent is executing, not tree-snapshot cadence. Asking for release means
asking for all of it.
