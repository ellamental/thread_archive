#!/usr/bin/env python3
"""Audit the GitHub-side release hardening against docs/releasing.md §0.

The repo's protections are part of the release mechanism — the tag ruleset is
what keeps a `v*` tag meaning "released", the `main` ruleset is what makes the
merge the ship, and the required status checks are what make §4's greens
unmergeable-when-red rather than remembered. All of it lives in GitHub
settings, invisible from the repo and silent when it drifts. This script is
the only thing that looks: it reads the rulesets over `gh api` and holds them
against what §0 requires. Read-only — nothing here writes a setting.

Run from the release preflight (docs/releasing.md §2). Needs `gh`
authenticated as someone who can read the repo's rulesets.

What §0 requires that no API exposes is printed as MANUAL, not checked: 2FA
on every pushing account, and the PyPI Trusted Publishing tuple (repo +
workflow + environment) under the project's publishing settings on pypi.org.

Exit status: 0 when every check passes, 1 when any fails or the API is
unreachable.
"""

from __future__ import annotations

import json
import subprocess
import sys

REPO = "ellamental/thread_archive"

# The §4 greens as check-run names, which is how a ruleset's
# required_status_checks knows them. These must track the job names (and
# matrix renderings) in .github/workflows/ — a rename there that forgets this
# list turns the merge gate off for that job, and this audit red is the only
# thing that would say so.
REQUIRED_CHECKS = {
    "python (ubuntu-latest, 3.12)",
    "python (ubuntu-latest, 3.13)",
    "python (ubuntu-latest, 3.14)",
    "python (macos-latest, 3.14)",
    "python-serial",
    "systemd",
    "frontend",
    "devweb",
    "package (ubuntu-latest)",
    "package (macos-latest)",
    "install",
    "gate",  # bench.yml — the off-box search-quality gate
    "release-shape",  # release-pr.yml — the §4 shape contract
}

ADMIN_ROLE_ID = 5  # GitHub's fixed RepositoryRole id for "repository admin"

failures = 0


def report(ok: bool, label: str, detail: str = "") -> None:
    global failures
    mark = "ok  " if ok else "FAIL"
    if not ok:
        failures += 1
    print(f"{mark}  {label}" + (f" — {detail}" if detail and not ok else ""))


def gh_api(path: str) -> object:
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        sys.exit(f"gh api {path} failed: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout)


def rule_map(ruleset: dict) -> dict[str, dict]:
    return {r["type"]: (r.get("parameters") or {}) for r in ruleset.get("rules", [])}


def main() -> int:
    listing = gh_api(f"repos/{REPO}/rulesets")
    by_name = {r["name"]: r["id"] for r in listing}

    # ── main: PRs only, merge commits only, nothing bypasses, greens required ─
    if "main" not in by_name:
        report(False, "main ruleset exists", "no ruleset named 'main'")
    else:
        rs = gh_api(f"repos/{REPO}/rulesets/{by_name['main']}")
        rules = rule_map(rs)
        report(rs.get("enforcement") == "active", "main ruleset active", str(rs.get("enforcement")))
        report(rs.get("bypass_actors") == [], "main bypass list empty — nothing skips the PR path",
               json.dumps(rs.get("bypass_actors")))
        report("deletion" in rules and "non_fast_forward" in rules,
               "main protected against deletion and force-push", json.dumps(sorted(rules)))
        pr = rules.get("pull_request")
        report(pr is not None and pr.get("allowed_merge_methods") == ["merge"],
               "main merges by merge commit alone (release topology, §'Branches')",
               json.dumps(pr and pr.get("allowed_merge_methods")))
        rsc = rules.get("required_status_checks")
        if rsc is None:
            report(False, "main requires status checks", "no required_status_checks rule — §4's greens are unenforced")
        else:
            have = {c["context"] for c in rsc.get("required_status_checks", [])}
            missing = REQUIRED_CHECKS - have
            report(not missing, "main requires every §4 green",
                   "missing: " + ", ".join(sorted(missing)) if missing else "")
            # Strict up-to-date would demand release branches contain main's tip,
            # which the branch topology deliberately never gives them (main's
            # merge commits exist only on main): it must stay off.
            report(rsc.get("strict_required_status_checks_policy") is False,
                   "main required checks are not 'strict' (release branches never carry main's tip)",
                   str(rsc.get("strict_required_status_checks_policy")))
        cs = rules.get("code_scanning")
        tools = [t.get("tool") for t in (cs or {}).get("code_scanning_tools", [])]
        report(cs is not None and "CodeQL" in tools,
               "main requires CodeQL results (code_scanning rule)", json.dumps(tools))

    # ── dev: collaborators via reviewed PR; the operator lands directly ──────
    if "dev" not in by_name:
        report(False, "dev ruleset exists", "no ruleset named 'dev'")
    else:
        rs = gh_api(f"repos/{REPO}/rulesets/{by_name['dev']}")
        rules = rule_map(rs)
        report(rs.get("enforcement") == "active", "dev ruleset active", str(rs.get("enforcement")))
        pr = rules.get("pull_request")
        report(pr is not None and pr.get("required_approving_review_count", 0) >= 1,
               "dev requires an approved PR from collaborators",
               json.dumps(pr and pr.get("required_approving_review_count")))
        bypass = {(a.get("actor_type"), a.get("actor_id")) for a in rs.get("bypass_actors", [])}
        report(("RepositoryRole", ADMIN_ROLE_ID) in bypass,
               "dev bypass includes the repository admin (the operator's direct-land path)",
               json.dumps(sorted(map(str, bypass))))

    # ── v* tags: only the release machinery and the admin may mint or move ───
    tag_rs, tag_name = None, None
    for name, rid in by_name.items():
        rs = gh_api(f"repos/{REPO}/rulesets/{rid}")
        if rs.get("target") == "tag":
            tag_rs, tag_name = rs, name
            break
    if tag_rs is None:
        report(False, "v* tag ruleset exists", "no tag-targeted ruleset")
    else:
        includes = tag_rs.get("conditions", {}).get("ref_name", {}).get("include", [])
        report("refs/tags/v*" in includes, f"tag ruleset '{tag_name}' covers refs/tags/v*", json.dumps(includes))
        report(tag_rs.get("enforcement") == "active", f"tag ruleset '{tag_name}' active",
               str(tag_rs.get("enforcement")))
        types = set(rule_map(tag_rs))
        need = {"creation", "update", "deletion", "non_fast_forward"}
        report(need <= types,
               "v* creation, update, and deletion all restricted (a released tag can never move quietly)",
               "missing: " + ", ".join(sorted(need - types)))
        actors = {(a.get("actor_type"), a.get("actor_id")) for a in tag_rs.get("bypass_actors", [])}
        expected = {("DeployKey", None), ("RepositoryRole", ADMIN_ROLE_ID)}
        report(actors == expected,
               "v* bypass is exactly the release deploy key + the repository admin",
               json.dumps(sorted(map(str, actors))))

    # ── environments: each publish surface's repo-side half of its trust tuple ─
    # PyPI verifies repo + workflow + environment; the environment existing here
    # is the half this API can see. `pypi` anchors the real publish
    # (publish.yml), `testpypi` the weekly drill (drill.yml).
    envs = gh_api(f"repos/{REPO}/environments")
    have_envs = {e.get("name") for e in envs.get("environments", [])}
    for env, workflow in (("pypi", "publish.yml"), ("testpypi", "drill.yml")):
        report(env in have_envs,
               f"environment '{env}' exists ({workflow}'s Trusted Publishing tuple names it)",
               json.dumps(sorted(have_envs)))

    print()
    print("MANUAL  2FA on every account that can push (no API exposes another user's setting).")
    print("MANUAL  PyPI Trusted Publishing tuple: pypi.org → thread-archive → Publishing —")
    print("        repo ellamental/thread_archive, workflow publish.yml, environment pypi.")
    print("MANUAL  TestPyPI Trusted Publishing tuple (the weekly drill): test.pypi.org →")
    print("        thread-archive → Publishing — repo ellamental/thread_archive,")
    print("        workflow drill.yml, environment testpypi.")

    if failures:
        print(f"\n{failures} check(s) failed — §0 of docs/releasing.md says what each one protects.")
        return 1
    print("\nAll API-visible §0 requirements hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
