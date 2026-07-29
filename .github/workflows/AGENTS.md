# CI on this fork

This directory holds 24 workflow files. Three of them run. The other 21 are
upstream's and are disabled, and keeping them that way is most of what the
tooling here does.

## Why it is arranged this way

`Sanmarcsoft/headroom` is a public fork of `headroomlabs-ai/headroom`, which
ships roughly 20 commits a day. The fork inherited every workflow upstream had:
108 jobs that publish to PyPI, npm, crates.io and ghcr, cut GitHub releases, open
release PRs, close stale issues, deploy docs to Vercel, spend LLM API keys, and
upload this repository's coverage to Codecov. None of that should happen here.

Actions were originally turned off wholesale (#11). That removed the hazard and
also removed every automated check, which is how the repository ended up with 22
open Dependabot alerts and no gate between a commit and a deployed image. #32
replaced the kill switch with an allowlist.

## The four layers

**Platform.** `enabled: true`, `allowed_actions: selected`,
**`sha_pinning_required: true`**, `default_workflow_permissions: read`,
`can_approve_pull_request_reviews: false`. Third-party actions are limited to
`astral-sh/setup-uv@*`; everything else must be GitHub-owned.

The pinning requirement is the load-bearing part. Upstream pins nothing (#7), so
any workflow arriving on a future sync is refused by the platform before it
runs. That is what makes fork-sync drift a preventive control rather than a
detective one.

**Files.** `ci/workflow-allowlist.txt` lists every workflow with a kind, `fork`
or `inherited`. `scripts/fork/check_workflow_allowlist.py` fails when the
directory holds a file the list does not. A sync that adds a workflow turns into
a red check on the sync PR instead of a silent addition.

**State.** The enabled/disabled flag lives on GitHub's side, not in git, so it
can be flipped in the Actions UI leaving no diff.
`scripts/fork/check_workflow_state.py` reads it back through the API and fails
when it disagrees with the allowlist. Without it, "the inherited automation is
disabled" is a claim about something that was true once.

**Distance.** `fork-drift.yml` measures `HEAD..upstream/main` daily and fails
above 200 commits, about ten days of upstream output. A failing scheduled run is
the alert; there is deliberately no `issues: write` anywhere in this directory.

## The three fork-owned workflows

| File | What it is for |
|---|---|
| `fork-gate.yml` | lint, format, types, tests. The merge gate. |
| `fork-security.yml` | pip-audit, CodeQL, gitleaks, zizmor. |
| `fork-drift.yml` | allowlist, workflow state, upstream distance. |

## Rules for editing anything in here

**The gate is defined once, in `scripts/fork/run-gate.sh`.** `fork-gate.yml`
calls it and `.claude/pre-push-gate.sh` calls it. Add a check there, never
directly in the YAML. Two definitions is how a repo gets a local check looser
than CI, and then a green local run stops predicting anything.

**mypy runs as `uvx mypy==1.20.2`, not `uv run --with mypy`.** `[tool.mypy]`
pins `python_version = "3.10"`; resolving mypy inside the project environment
pulls the numpy stubs bound to the runtime interpreter and reports errors that
do not exist. This cost a session on 2026-07-28.

**Pin every `uses:` to a full SHA with a trailing version comment**, and keep the
comment true. `sha_pinning_required` rejects an unpinned action outright; zizmor's
`ref-version-mismatch` audit catches a comment that has drifted from the commit
it names, but only in online mode, which is why the zizmor step passes
`GH_TOKEN`. Run the audit before pushing:

```bash
GH_TOKEN=$(pass show sanmarcsoft/github-pat) uvx zizmor==1.28.0 \
  --persona=auditor --min-severity=low .github/workflows/fork-*.yml
```

**No fork-owned workflow may publish anything, or send repository content to a
host outside github.com.** No Codecov, no Vercel, no runner-hardening services
that phone home. The EU data-sovereignty SOP covers CI egress too. The only
outbound fetches are PyPI, crates.io, and one unauthenticated read of a public
HuggingFace model id.

**`security-events: write` on the CodeQL job is the only write permission in
this directory.** It uploads SARIF to this repository's own security tab. If a
change needs another write, that is a decision to make deliberately, in the PR.

**Enabling an inherited workflow requires changing its kind in
`ci/workflow-allowlist.txt` first**, or `fork-drift.yml` fails. That is the
point: the file records intent, the API check enforces it, and the two can only
diverge through a reviewed diff. Read the workflow before you promote it. Each
one is disabled for a reason recorded next to its entry.

## Quarantine

`ci/quarantine.txt` lists tests `fork-gate.yml` deselects, with a reason each.
The inherited suite has failures this fork did not cause, and a gate that is red
on arrival gets ignored within a week.

`scripts/fork/check_quarantine.py` caps the list at a ceiling recorded in the
script. Raising it is a diff, in a PR, with a reason. A separate non-blocking job
runs the quarantined tests anyway, so an entry that starts passing shows up in
the run output instead of living there forever.

Measure on a runner, not locally, before adding an entry. This dev container
ships a 32-bit `sqlite_vec/vec0.so`, which makes `SQLITE_VEC_AVAILABLE` false and
errors out every memory-backed test. That is a property of one machine.

## Related

- #32 the allowlist design, and the per-workflow reasons
- #11 the original decision to disable Actions wholesale
- #13 sync cadence, drift alerting, branch protection
- #7 CI supply chain: SHA pinning, verified downloads, scoped tokens
- #9 egress and data-sovereignty audit
