# Releasing thread-archive

Distribution is PyPI (`pip install thread-archive`) and nothing else — no
`git+<repo-url>` install, no distro package, no tap
([public/scope.md](public/scope.md)). A release is a stabilization branch:
cut `release/X.Y.Z` from `dev`, harden it in its own worktree while `dev`
keeps moving, and open a PR to `main`. The operator merging that PR is the
ship. A workflow on `main` turns the merge into the annotated tag — what this
machine's checkout fast-forwards to, what `thread-archive status` / bug reports
correlate against — and the PyPI upload
(`.github/workflows/release.yml` → `publish.yml`, via Trusted Publishing).

**Merging the release PR is the point of no return.** An install gets the
release when its operator runs `thread-archive self-update` (`--check` is
how they see one exists). That is a delay, not a safety net: the release is offered to every install
the moment the publish lands, and the preflight below is the only gate
between a bad release and the first operator who reaches for it. This
machine's clone runs `dev`, which carries everything a release carries, so a
*logic* bug should hurt here first — but a *delivery* bug (packaging, the
publish machinery) cannot hurt here at all, which is what the mandatory rc
lane below exists to catch.

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
always the latest release. Release tags point at `main`'s merge commits, so a
checkout parked on `main` fast-forwards cleanly from tag to tag.

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
publishes the wheel `self-update` installs. The
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
- Required status checks on that `main` ruleset: every CI job, the Bench
  `gate`, the `install` lane, and `release-shape` (`release-pr.yml`, which
  holds §4's shape contract — version bumped past every released tag,
  changelog compressed, PR titled, a green rc exactly one commit back) —
  plus CodeQL results via the code-scanning rule. This is what makes §4's
  greens unmergeable-when-red rather than remembered. Non-strict,
  deliberately: release branches never contain `main`'s tip (its merge
  commits exist only on `main`), so the "require branches to be up to date"
  flavor would fight the branch topology itself.
- A branch ruleset on `dev`: collaborators go through a PR with one
  approval; the repository admin bypasses, which is what lets the operator —
  and the agents pushing as the operator — land work directly.
- The Trusted Publishing anchors: the `pypi` environment (publish.yml's, on
  PyPI's publisher tuple) and the `testpypi` environment (drill.yml's, on
  TestPyPI's — the weekly drill below rides it).
- The `RULESET_AUDIT_TOKEN` secret: a fine-grained PAT, this repository only,
  repository Administration **read** — what lets the weekly drill's audit job
  see bypass actors, which the rulesets API hides from anything below admin.
  Read-only by construction: a leak shows settings, it cannot change them.

`scripts/audit_release_settings.py` reads all of this over `gh api` and holds
it against this section. Settings drift is invisible from the repo, and the
audit — a §2 preflight step — is the only thing that looks; what no API
exposes (2FA, the PyPI Trusted Publishing tuple) it names as manual.

## 1. Cut the release branch

The primary checkout stays on `dev` — the daemons run from it and other work
continues there — so the release branch gets its own worktree:

```bash
scripts/release_cut.sh X.Y.Z
```

One command: the worktree at `~/dev/archive-rc` on a fresh `release/X.Y.Z`
cut from `dev`, its venv (editable install with the dev group), both
frontends' `npm ci`, the branch pushed, and the draft PR opened. Both `npm
ci`s are load-bearing: the local CI sweeper runs `ci.toml` against whatever
tree the commit landed in, so a worktree missing either `node_modules` reds
that app's typecheck/test/e2e rows for a setup reason and buries whatever the
sweep was supposed to tell you.

The push at the cut is what starts GitHub CI on the branch, and the draft PR
is the release's workbench: CI fills in, the diff is the whole release, the
body will become the changelog section.

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
- The worktree's thread-ci sweep green. GitHub CI mirrors most of the local
  bar on the branch, but the sweep is the whole of it — `retrieval-gate` runs
  only here, because only this machine has the live archive it reads.
- The package lane green: `.venv/bin/pytest -m package --no-cov
  tests/test_package_artifact.py` — builds the wheel + sdist with
  `python -m build`, proves their contents, installs the wheel into a clean
  venv, and runs the real entry points. Nothing is uploaded anywhere; this is
  the gate that proves a from-scratch install actually works (files present,
  console scripts wired), rather than only the long-lived editable install.
- GitHub CI green on `release/X.Y.Z` (ruff, mypy, coverage floor, the pytest
  suite across the interpreter matrix, frontend and devweb checks, and the
  same package lane).
- The GitHub-side hardening intact: `.venv/bin/python
  scripts/audit_release_settings.py` — the §0 requirements, read live over
  `gh api`. Seconds, and the only check that would notice a ruleset quietly
  loosened.
- The corpora unmoved: `python -m search_lab pins`. Content hashes of the files
  each harness reads, against the accepted ones in
  `search_lab/dataset-pins.json`. None of the upstreams offer an immutable
  handle, so this is what makes the gate's numbers comparable to the accepted
  ones at all — a drifted corpus turns a release check into a comparison
  between two different measurements. Milliseconds; the harnesses verify the
  same hashes before they build, so a drift found here has already stopped the
  bench.
- The search-quality gate green: `python -m search_lab gate --run --quick`. The
  bench's numbers against the accepted ones in
  `search_lab/quality-baseline.json` — public benchmarks whose labels somebody
  else made, so a breach is real evidence the retrieval components got worse.
  `--run` measures whatever the code has invalidated first: rows unchanged since
  their last run are fresh and cost milliseconds, and a ranking edit re-runs the
  quick tier in under 20 minutes. The ledger and corpus cache live in the lab's
  own state home, so a fresh worktree starts warm. A row last measured at other
  code fails as **stale** rather than passing on old numbers — shipping an
  unmeasured ranking change under a green gate is the failure this exists to
  prevent. See `search_lab/README.md` for the tiers below it and what each one
  licenses.

  **`--quick` is not optional here, and it is not a shortcut.** The quick tier
  is a depth in its own right — every query on the rows that fit, a
  deterministic sample on the three arms too large to score whole — and its rows
  carry their own accepted numbers under their own `~N` names. Dropping the flag
  gates the full tier instead, whose rows are hours of scoring and, on this box,
  have no accepted numbers to compare against.

  A breach is a decision, not a formality. Either it is a regression — fix or
  revert — or it is a deliberate trade, accepted with
  `python -m search_lab gate --quick --update`, which puts the movement in the
  release diff where a reader can see what was given up.
- If `frontend/` changed since the last release, the committed
  `_web/static/` bundle must be current: `cd frontend && npm run build`,
  and the regenerated static assets committed with the change that caused
  them — a checkout runs whatever bundle is in the tree. This gates the
  checkout only; the viewer is dev-only and no wheel carries it, so a stale
  bundle cannot reach an installed user.

## Release candidates — every release publishes at least one

The rc is the only step that exercises the release's *delivery* surface
before the final version number is at stake. Everything preflight checks
runs code; the failure classes that have actually burned releases live in
the machinery around it — the GitHub CI environment (ubuntu, the 3.12
floor, librarian-free, fresh pip), CodeQL on the PR, the tag ruleset, the
Trusted Publishing handshake, the wheel PyPI actually serves — and none of
those can fail on this machine, only on GitHub, only when something real
goes through them. The rc is that something. It is not optional: the final
PR is not marked ready until an rc has gone green end to end (§4).

PyPI has no release channels; PEP 440 pre-release versions are the
mechanism, and they are enough. An `X.Y.ZrcN` upload lands on the same PyPI
project as a final release, but pip and uv skip pre-releases unless asked:
a tester opts in with `pip install --pre thread-archive` or an exact
`thread-archive==X.Y.ZrcN` pin, and everyone else's `pip install` keeps
resolving the last final. `self-update` is stricter still — it only ever
offers a wheel naming a plain `X.Y.Z` — so an rc reaches nobody who did not
explicitly ask for it. PEP 440 orders `X.Y.ZrcN < X.Y.Z`, so when the final
ships, `--pre` installs converge onto it with a plain upgrade.

An rc is a stabilization-branch artifact. It never touches `main`, `dev`,
or the PR — it is a tagged commit on `release/X.Y.Z`, published by hand:

1. Preflight first: at minimum the full suite and the package lane green on
   the branch (an rc is still executable software offered to real installs).
2. On `release/X.Y.Z`, set `__version__ = "X.Y.ZrcN"` and commit:
   `Release candidate X.Y.ZrcN`. No changelog compression — that happens
   once, at the final.
3. Tag and push — the hand-pushed tag is publish.yml's manual path, and the
   `v*` tag ruleset means the repository admin pushes it:

   ```bash
   git tag -a "vX.Y.ZrcN" -m "thread-archive X.Y.ZrcN (release candidate)"
   git push origin "vX.Y.ZrcN"
   ```

4. Watch the Publish run. Its `verify` job installs the just-published rc
   from PyPI into a fresh interpreter, smoke-tests the real entry points, and
   runs a full ingest lifecycle on it — the same check §6 runs by hand for a
   final. A green `verify` on the rc is the point of cutting one: it proves
   the published artifact installs and *works* over the exact path the final
   will take, before the final's version number is at stake.

Fixes found during the rc land on the release branch as usual; the next
round is `rcN+1` — the §3 release commit (changelog compression + version
bump) is the only commit allowed between the last green rc and the final.
Anything else landing on the branch after the rc means the published rc no
longer proves the tree being shipped: cut `rcN+1`. The §3 release commit
replaces the rc string with the plain `X.Y.Z` — release.yml's version read
accepts nothing else, so an rc
string accidentally left in place fails the tag workflow on `main` loudly
instead of shipping. Like any version, a published rc's number is burned:
PyPI never accepts a re-upload, and a bad rc is yanked the same way a bad
release is (below), followed by the next rc rather than a re-tag.

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
layout changed incompatibly, the format version in `docs/public/format.md` moves on
its own rules — that is a separate, deliberate decision, not part of the
package bump.

One commit on `release/X.Y.Z` containing exactly the changelog compression
and the version bump:

```
Release X.Y.Z: compress changelog, bump version
```

## 4. The release PR — the operator ships it

Push, set the PR's title to `Release X.Y.Z` and its body to the version's
changelog section, and mark it ready for review. Ready-for-review asserts
four greens, all on the branch as it now stands: the preflight (§2), the
PR's own GitHub CI and CodeQL runs, the Bench lane (`bench.yml` — the
search-quality gate run off-box, against the packs and the checked-in
baseline; it runs on every PR to `main`, from the PR head's workflow file),
and an rc whose Publish `verify` job passed, with nothing but the §3
release commit on top of it. The diff is
everything since the last release.

Little of this rides on memory: `release-shape` (`release-pr.yml`) holds the
shape half mechanically — the version outranks every released tag, the
changelog section exists within its limits, the title matches, the rc sits
exactly one commit back with a green Publish — and the `main` ruleset's
required status checks refuse the merge while it, CI, CodeQL, Bench, or the
install lane is red. Ready-for-review is still the assertion; the machinery
is what makes a false assertion unmergeable.

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

Watch the runs as a command, not an intention — `gh run watch` on the
Release run, then on the Publish run its tag fires — so the release's
executor blocks until both conclude. A publish failure leaves the tag
without its upload, and which fix applies depends on what PyPI saw: a run
that died before anything uploaded (the OIDC handshake, a runner death)
burns nothing — re-run the failed Publish run and it ships the same
artifacts. Once any file of the version reached PyPI, the number is burned —
PyPI never accepts a re-upload of a once-seen file, even after deletion —
and the fix is a fixed vX.Y.(Z+1).

## 6. Verify from the outside

Prove the release installs from PyPI — the only shape an operator gets — and
not just from this checkout's long-lived venv.

That proof is automated: the Publish run's `verify` job installs the
published version from PyPI into a fresh interpreter (wheel only — the
self-update path), runs the entry points, checks the installed `__version__`
against the tag, and then drives a real lifecycle on it — a corpus for every
provider, imported through each provider's own importer, the index rebuilt
from the JSONL truth, every provider's marker searched back out
(`tests/install/e2e_check.py`, from the tagged tree; the package under test
stays the wheel, which the job asserts). That last step is the only place the
published artifact does real work against dependencies resolved fresh from the
index — the `package` and install lanes both prove a locally built wheel
against locally resolved ones. Watch it go green. To repeat it by hand:

```bash
python3 -m venv /tmp/ta-verify
/tmp/ta-verify/bin/pip install "thread-archive==X.Y.Z"
/tmp/ta-verify/bin/thread-archive --help
```

## 7. Back-merge, roll the local deployment, clean up

One command — it acts on the primary checkout, which sits on `dev`:

```bash
scripts/release_finish.sh X.Y.Z
```

It verifies the tag actually landed (finishing before the Release run
completes would tear down a branch a re-run might still need), back-merges
`release/X.Y.Z` into `dev` and pushes, fetches the new tag into this clone —
`status` output and bug reports correlate against tags, and a fetch is the
only way one arrives — refreshes the editable install's metadata, restarts
the service agents, and retires the worktree and the branch on both sides.

Back-merge conflicts arise only where `dev` diverged from a stabilization
fix while the release was in flight — resolve them here, once. The
back-merge is load-bearing (see the branch rules above): it is what keeps
the next release's merge to `main` clean.

The daemons on this machine run from the primary checkout's editable install
on `dev`, so the back-merge landing *is* the deployment — the script's
reinstall and `thread-archive service restart` are the follow-through
(editable installs pick up code automatically, but not dependency or
entry-point changes, and nothing reloads a daemon but a restart). MCP
clients pick up the new server on their next session. The script refuses to
remove a worktree that still carries uncommitted tracked changes — that is
unmerged work, not build residue.

## A red check on main is fixed through the rc flow — never by probing with finals

`main` is exactly the last shipped tree — nothing merges there but release
PRs — so when a check goes red on `main` itself (a scheduled CodeQL run
surfacing a new alert, a workflow rotting against a GitHub change), the
only way to clear it is to ship a release carrying the fix. That is fine;
the failure mode to refuse is using *final* versions as the probe: ship,
watch `main`, still red, ship again. A final reaches every install and its
number is burned; it must never be the experiment.

The rc flow is the experiment channel. Land the fix on `dev`, cut
`release/X.Y.Z` as usual, and iterate **on the branch**: every push runs
the same CI, the PR runs the same CodeQL queries that red-flagged `main`,
and an rc exercises the tag ruleset and the publish path — the entire
surface a GitHub-side fix could be wrong about, without touching `main` or
any install. Fix, push, read the checks, `rcN+1` if the publish surface is
implicated; repeat until the branch is green everywhere. Only then does the
final ship, once, and `main` goes green because the merge carries a fix
already proven on the exact machinery that was failing.

## The weekly drill — the machinery exercised between releases

Release machinery is the least-executed code in the repo: it runs when a
release ships, which is exactly when a quiet breakage hurts most. The weekly
drill runs the delivery path on throwaway versions so that breakage surfaces
on a schedule instead of mid-ship — the same property the backup restore
drill buys for backups. The clock is `.github/workflows/drill.yml`'s own
weekly cron; `scripts/release_drill.sh` runs the same thing by hand — after
fixing release machinery, ahead of a release — without waiting for the
schedule. (A cron fire executes `main`'s copy of the workflow file — GitHub
runs scheduled workflows from the default branch only — but every job checks
out `dev` explicitly, so the drill always builds and harnesses dev's tip. A
drill.yml change therefore reaches the *schedule* at the next release, while
a dispatch on `dev` runs it immediately.)

Two halves, jobs of the same run:

1. **The settings audit** (`scripts/audit_release_settings.py`) — the §0
   requirements read live. GitHub-side settings are load-bearing unversioned
   state, and the audit is the only thing that looks; the weekly fire is what
   turns "someone thinks to run it" into a clock. Its job authenticates with
   the `RULESET_AUDIT_TOKEN` secret (§0), because the rulesets API hides
   bypass actors from anything below admin — a `GITHUB_TOKEN` read shows the
   rules but not who may skip them, which is half of what §0 protects.
2. **The TestPyPI drill** — dev's tip built under drill version numbers
   (`999.run.N`, plain X.Y.Z, unmistakably not a release), an rc and a final
   published to TestPyPI over the same Trusted Publishing handshake shape the
   real publish uses, and then the operator surface driven against what the
   index actually serves: plain resolution lands the final and skips the rc,
   the rc installs by explicit opt-in, the fresh install runs the real ingest
   lifecycle (the same `tests/install/e2e_check.py` the Publish `verify` job
   runs on a real release), `self-update` carries an install from the base
   drill version to the target, refuses to offer the rc, and rolls back to
   the base when the post-install smoke fails
   (`tests/install/self_update_check.py`). Dependencies never resolve against
   TestPyPI — it is an open index anyone can upload to — so every leg pins
   `--no-deps` and takes only the artifact under judgement from it.

The whole workflow is gated on the `DRILL_ENABLED` repository variable: until
it is `true`, every trigger — cron and dispatch alike — skips all jobs, and
`release_drill.sh` refuses to dispatch rather than report an all-skipped run
as a pass. That is how the drill ships ahead of its standing requirements
(the TestPyPI publisher, the audit token) and how it pauses deliberately:
`gh variable set DRILL_ENABLED -b true` (or `-b false`) is the switch.

The drill mints no refs: no tag (a `v*` push would fire the real publish), no
branch, no commit — its only residue is throwaway versions on TestPyPI. That
boundary is also its limit: the tag ruleset + deploy-key push (release.yml)
and the real PyPI publisher tuple cannot be drilled without shipping, so they
stay covered by the mandatory per-release rc lane and the audit. A red drill
means the release machinery is broken *now*, on a quiet week — fix it on
`dev` and dispatch again; nothing is in flight, nothing is burned that
matters.

## Yanking a bad release

Yank the release on PyPI first (project → release → Options → Yank) — that is
where self-update resolves from, and a yanked version stops being a candidate
for it and for every fresh `pip install`. A `==X.Y.Z` pin still gets it, and the
version number is burned: PyPI never accepts a re-upload of it. Then delete the
bad tag — it is the release's identity, what `status` output and bug reports
correlate against, and what a checkout would otherwise move to:

```bash
git push origin :refs/tags/vX.Y.Z     # delete the remote tag
```

Neither heals an install whose operator already applied the release, nor
removes a tag already fetched into a checkout. Always follow with the real fix:

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
