#!/usr/bin/env python3
"""Fail when an inherited upstream workflow is enabled on this fork.

ci/workflow-allowlist.txt records the intent: `fork` files should run here,
`inherited` files should not. That intent lives in two places at once, though —
the file, and the per-workflow enabled/disabled state GitHub stores server-side.
Only the file is under review. The server-side state can be changed from the
Actions UI in two clicks, by anyone with write access, leaving no diff.

So this reads the state back through the API and asserts it matches the file.
Without it, "the inherited automation is disabled" is a claim about something
that was true once, which is exactly the kind of unverified assertion that turns
into an incident.

Requires `actions: read` and GH_TOKEN in the environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = REPO_ROOT / "ci" / "workflow-allowlist.txt"

# GitHub's own values. `active` means it runs. The two disabled forms differ in
# who turned it off: a person, or GitHub after 60 days of repository inactivity.
STATE_ACTIVE = "active"
STATES_OFF = {"disabled_manually", "disabled_inactivity", "disabled_fork"}


def parse_allowlist() -> dict[str, str]:
    entries: dict[str, str] = {}
    for raw in ALLOWLIST.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        kind, name = line.split()
        entries[name] = kind
    return entries


def fetch_workflows(repo: str) -> list[dict[str, str]]:
    out = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/actions/workflows", "--jq", ".workflows[]"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def main() -> int:
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        sys.exit("set REPO (or GITHUB_REPOSITORY) to owner/name")

    allowed = parse_allowlist()
    problems: list[str] = []
    checked = 0

    for wf in fetch_workflows(repo):
        name = Path(wf["path"]).name
        kind = allowed.get(name)
        if kind is None:
            # check_workflow_allowlist.py owns this failure mode; reporting it
            # twice would just make one root cause look like two.
            continue
        checked += 1
        state = wf["state"]

        if kind == "inherited" and state == STATE_ACTIVE:
            problems.append(
                f"::error::{name} is inherited upstream automation and it is ACTIVE. "
                f"Disable it: gh api -X PUT repos/{repo}/actions/workflows/{name}/disable"
            )
        elif kind == "fork" and state in STATES_OFF:
            problems.append(
                f"::error::{name} is a fork-owned gate and it is {state}. "
                f"Enable it: gh api -X PUT repos/{repo}/actions/workflows/{name}/enable"
            )
        else:
            print(f"ok  {kind:<9} {name:<28} {state}")

    if checked == 0:
        # Reaching the API and matching nothing means the allowlist and the
        # repository have diverged completely. Passing here would be vacuous.
        sys.exit("::error::no allowlisted workflow matched the API response")

    for p in problems:
        print(p)

    if problems:
        print(f"\n{len(problems)} workflow(s) in the wrong state")
        return 1

    print(f"\nall {checked} allowlisted workflows are in their intended state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
