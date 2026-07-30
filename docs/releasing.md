# Releasing thread-archive

Distribution is PyPI (`pip install thread-archive`) and the git clone (an
editable install from a checkout, or
`pip install "git+<repo-url>@vX.Y.Z"`). A release is a stabilization branch:
cut `release/X.Y.Z` from `dev`, harden it in its own worktree while `dev`
keeps moving, and open a PR to `main`. The operator merging that PR is the
ship. A workflow on `main` turns the merge into the annotated tag — what a
clone pins and fast-forwards to, what `thread-archive status` / bug reports
correlate against — and the PyPI upload
(`.github/workflows/release.yml` → `publish.yml`, via Trusted Publishing).

**Merging the release PR is the point of no return.** A packaged install gets
the release when its operator runs `thread-archive self-update` (`--check` is
how they see one exists); a clone gets it when someone checks the tag out.
That is a delay, not a safety net: the release is offered to every install
the moment the publish lands, and the preflight below is the only gate
between a bad release and the first operator who reaches for it. This
machine's clone runs ahead of consumers, so a bad release should hurt here
first.

The version's single source of truth is `__version__` in
`src/thread_archive/__init__.py`; pyproject declares `version` dynamic and
hatch reads it from there. Nothing else carries the number.

## Branches: dev develops, release/X.Y.Z stabilizes, main is the release history

Development lives on `dev` — the local clone sits there, every change lands
there, CI runs there. A release cuts `release/X.Y.Z` from `dev`'s tip;
preflight, the changelog compression, the version bump, and any fixes found
during stabilization all land on that branch, in a dedicated worktree, while
`dev` moves on underneath. `main` advances only by merging release PRs — one
merge commit per version, its tree identical to the release branch's tip —
so what a visitor sees on GitHub (README, CI badge, browsable code) is
always the latest release, and `pip install git+…@main` means something.
Release tags point at `main`'s merge commits, so a clone parked on `main`
fast-forwards cleanly from tag to tag.

Two rules keep the topology sound:

- **Release PRs merge with a merge commit — never squash, never rebase.**
  Long-lived branches pin their squash-merge base at the fork point, so
  repeated squash merges replay old diffs and eventually conflict; true
  merges advance the base every release.
- **The release branch merges back into `dev` right after the release**
  (§7). The back-merge carries the release commit and any stabilization
  fixes home and keeps everything ancestor-connected — it is what makes the
  *next* release's merge to `main` conflict-free. Skip it and the drift
  compounds.

## 0. The repo is release infrastructure — keep it hardened

A release tag is executable software offered to every install — it is what
publishes the wheel `self-update` installs, and what a clone checks out. The
GitHub repo's protections are therefore part of the release mechanism, not
optional hygiene. The standing requirements:

- Two-factor auth on every account that can push.
- A tag ruleset covering `v*` — creation, update, and deletion restricted to
  its bypass actors: the release deploy key (`RELEASE_TAG_DEPLOY_KEY`, the
  write deploy key `release.yml` pushes tags with — a personal repo can't
  put the Actions app itself on a bypass list) and the repository admin
  (the manual path, and deleting a yanked release's tag).
- A branch ruleset on `main`: PRs only, no direct pushes, allowed merge
  method `merge` alone — the merge-commit rule above is enforced, not
  remembered. Empty bypass list, deliberately: nothing skips the PR path,
  because the merge is the ship.
- A branch ruleset on `dev`: collaborators go through a PR with one
  approval; the repository admin bypasses, which is what lets the operator —
  and the agents pushing as the operator — land work directly.

## 1. Cut the release branch

The primary checkout stays on `dev` — the daemons run from it and other work
continues there — so the release branch gets its own worktree:

```bash
git worktree add "$HOME/dev/archive-rc" -b release/X.Y.Z dev
cd ~/dev/archive-rc
python3 -m venv .venv
.venv/bin/pip install --upgrade pip   # `--group` is PEP 735; needs pip >= 25.1
.venv/bin/pip install -e ".[embeddings,leiden]" --group dev
(cd frontend && npm ci)
git push -u origin release/X.Y.Z
```

Push at the cut so GitHub CI starts running the branch, and open the PR to
`main` immediately as a draft — it is the release's workbench: CI fills in,
the diff is the whole release, the body will become the changelog section.

Everything that follows happens in the worktree, on `release/X.Y.Z`. The cut
is frozen: `dev` landing more work does not move it, which is what buys the
benchmarks and preflight a calm tree. A fix discovered during stabilization
is committed on the release branch and flows back to `dev` in the §7
back-merge — not the other way around. (If `dev` has meanwhile landed a fix
the release genuinely needs, cherry-pick it in and say so in the PR; the
back-merge reconciles the duplicate.)

## 2. Preflight — the branch must be releasable

All in the worktree:

- Full suite green: `.venv/bin/pytest tests/`.
- The package lane green: `.venv/bin/pytest -m package --no-cov
  tests/test_package_artifact.py` — builds the wheel + sdist with
  `python -m build`, proves their contents, installs the wheel into a clean
  venv, and runs the real entry points. Nothing is uploaded anywhere; this is
  the gate that proves a fresh-clone install actually works (files present,
  console scripts wired), rather than only the long-lived editable install.
- GitHub CI green on `release/X.Y.Z` (ruff, mypy, coverage floor, the pytest
  suite on the 3.12 floor and 3.14, frontend checks, and the same package
  lane).
- The search-quality gate green: `python -m search_lab gate --run`. The bench's
  numbers against the accepted ones in `search_lab/quality-baseline.json` —
  public benchmarks whose labels somebody else made, so a breach is real
  evidence the retrieval components got worse. `--run` measures whatever the
  code has invalidated first: rows unchanged since their last run are fresh and
  cost milliseconds, a ranking edit re-runs the set (a few hours cold — the
  frozen branch is what makes that affordable). The ledger and corpus cache
  live in the lab's own state home, so a fresh worktree starts warm. A row
  last measured at other code fails as **stale** rather than passing on old
  numbers — shipping an unmeasured ranking change under a green gate is the
  failure this exists to prevent. See `search_lab/README.md` for the tiers
  below it and what each one licenses.

  A breach is a decision, not a formality. Either it is a regression — fix or
  revert — or it is a deliberate trade, accepted with
  `python -m search_lab gate --update`, which puts the movement in the release
  diff where a reader can see what was given up.
- If `frontend/` changed since the last release, the committed
  `_web/static/` bundle must be current: `cd frontend && npm run build`,
  and the regenerated static assets committed with the change that caused
  them — a clone runs whatever bundle is in the tree. This gates the clone
  only; the viewer is dev-only and no wheel carries it, so a stale bundle
  cannot reach an installed user.

## 3. Compress the changelog, bump the version

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

Pick the number (semver; pre-0.1.0, all/breaking changes bump the minor). Edit
`__version__` in `src/thread_archive/__init__.py`. If the truth-directory
layout changed incompatibly, the format version in `docs/format.md` moves on
its own rules — that is a separate, deliberate decision, not part of the
package bump.

One commit on `release/X.Y.Z` containing exactly the changelog compression
and the version bump:

```
Release X.Y.Z: compress changelog, bump version
```

## 4. The release PR — the operator ships it

Push, set the PR's title to `Release X.Y.Z` and its body to the version's
changelog section, and mark it ready for review. The diff is everything since
the last release; the preflight above is already green on exactly this tree.

The operator merges it (merge commit). That merge is the ship — everything
after this section is follow-through, not gate.

## 5. Tag and publish — automated on the merge

`.github/workflows/release.yml` runs on every push to `main`: it reads
`__version__`, and — if `vX.Y.Z` does not already exist — creates the
annotated tag on the merge commit, message carrying the version's changelog
section, and pushes it with the release deploy key. That push fires
`publish.yml` like any other tag push: wheel + sdist built on the runner,
uploaded to PyPI via Trusted Publishing (no tokens; PyPI trusts the
repo/workflow/environment tuple configured under the project's Publishing
settings on pypi.org). A tag pushed by hand takes the identical publish
path — that is the manual route, for a release shipped without the PR
machinery.

Watch the runs. A publish failure means the tag exists but PyPI lags it, and
the fix is a fixed vX.Y.(Z+1), since PyPI refuses re-uploads of a once-seen
version even after deletion.

## 6. Verify from the outside

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

## 7. Back-merge, roll the local deployment, clean up

Merge the release branch into `dev` — from the primary checkout, which sits
on `dev`:

```bash
git merge --no-ff release/X.Y.Z -m "Merge release/X.Y.Z back into dev"
git push origin dev
```

Conflicts arise only where `dev` diverged from a stabilization fix while the
release was in flight — resolve them here, once. This step is load-bearing
(see the branch rules above): it is what keeps the next release's merge to
`main` clean.

The daemons on this machine run from the primary checkout's editable install
on `dev`, so the back-merge landing *is* the deployment — with two
follow-throughs:

- If dependencies or entry points changed, re-run `.venv/bin/pip install -e .`
  (editable installs pick up code automatically, not metadata).
- Restart whatever loaded the old code: `thread-archive service restart` for
  the watcher/backup agents; MCP clients pick up the new server on their next
  session.

Then retire the branch:

```bash
git worktree remove ~/dev/archive-rc
git branch -d release/X.Y.Z
git push origin :release/X.Y.Z   # unless GitHub already deleted it on merge
```

## Yanking a bad release

Yank the release on PyPI first (project → release → Options → Yank) — that is
where self-update resolves from, and a yanked version stops being a candidate
for it and for every fresh `pip install`. A `==X.Y.Z` pin still gets it, and the
version number is burned: PyPI never accepts a re-upload of it. Then delete the
bad tag, which is the clone path and what a `git+…@vX.Y.Z` install resolves:

```bash
git push origin :refs/tags/vX.Y.Z     # delete the remote tag
```

Neither heals an install whose operator already applied the release, nor
removes a tag a clone already fetched locally. Always follow with the real fix:

```bash
# fix, then release vX.Y.(Z+1) normally
```

The higher tag outranks the lingering bad one everywhere, fetched or not. A
truth-format bump still cannot be rolled back after the newer writer touches
the archive, which is why applying one requires an explicit flag.

## Who runs this

The agent, end to end, except the merge button: preflight, changelog
compression, version bump, the release-branch commits and pushes, the PR,
watching the tag-and-publish run, verification, the back-merge into `dev`,
rolling the local deployment, and retiring the branch. The commits, branch
operations, and pushes here are part of the release process the agent is
executing (an explicit exception to any standing no-commit convention in the
operator's environment) — and the worktree keeps all of it out of the
primary checkout, whose branch never moves. Merging the release PR is the
operator's act alone; asking for a release means asking for everything on
either side of it.
