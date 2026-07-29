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
| `fork-gate.yml` | lint, format, types, clippy, tests. The merge gate. |
| `fork-security.yml` | pip-audit, osv-scanner, opengrep, cargo-audit, gitleaks, zizmor. |
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

**The gate provisions every tool it runs; it never inherits one.** `ruff` runs
as `uvx ruff==<pin>` with the pin read from `scripts/verify-ruff-version.py`,
the repo's existing reader that upstream's `ci.yml` uses for the same purpose.
`pytest` runs as `uv run --frozen --extra dev`, because pytest, respx,
sqlite-vec and sentence-transformers all live in that extra. The first CI run
of this gate died on `Failed to spawn: ruff` and `Failed to spawn: pytest`: a
fresh checkout installs only the 58 base packages, and the machine the gate was
written on already had the rest. It had passed locally for a reason unrelated
to the code.

**The suite runs on the host's CPython. Do not pin it to a uv-managed one.**
`UV_PYTHON_PREFERENCE=system` is set deliberately. The managed build is
tempting because it carries its own `Python.h`, which the `dev` extra needs to
compile hnswlib. It also ships its own OpenSSL and starts with an *empty* trust
store, and `tests/test_ssl_context.py` asserts the opposite: that headroom
keeps the system CAs (`cert_store_stats()["x509_ca"] > 1`). Under the managed
interpreter two tests fail for reasons that have nothing to do with headroom.
A gate must not change the semantics of what it measures to make itself easier
to run. The consequence is that a host without the matching `python3-dev`
cannot build hnswlib and so cannot run the `test` target at all; that is a
property of the host, and CI is the authority.

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

**No fork-owned workflow holds any write permission.** Every one is
`contents: read`. This became true on 2026-07-29 when CodeQL was removed and
took the last `security-events: write` with it. Adding a write is a decision to
make deliberately, in the PR, not a line to slip into a job.

**SAST is OpenGrep, and it is measured against the CodeQL it replaced.** CodeQL
went when the repo went private, because code scanning needs GitHub Advanced
Security and the free plan does not have it. Its 50 alerts are snapshotted in
`ci/codeql-snapshot/`, and the replacement was checked against that snapshot
rather than declared equivalent. See "Source SAST" below for what it does and
does not reproduce.

**Enabling an inherited workflow requires changing its kind in
`ci/workflow-allowlist.txt` first**, or `fork-drift.yml` fails. That is the
point: the file records intent, the API check enforces it, and the two can only
diverge through a reviewed diff. Read the workflow before you promote it. Each
one is disabled for a reason recorded next to its entry.

## Dependency advisories

Two checks, deliberately different in scope.

**pip-audit** is zero-tolerance over one export: `uv export --no-dev --extra all`,
620 packages, the set that actually ships. It blocks on anything.

**osv-scanner** reads all six lockfiles instead (1839 packages: `uv.lock`,
`Cargo.lock`, and four `package-lock.json`), and compares against
`ci/vuln-baseline.txt`. Anything not in the baseline fails the job.

The reason for two is a reconciliation on 2026-07-29: pip-audit reported zero
vulnerabilities while GitHub held 22 open Dependabot alerts. Both numbers were
correct. Every one of the 22 sat outside the production export, in an opt-in
extra or in an npm lockfile nothing here was reading. A green pip-audit had
been quietly meaning less than it looked like it meant.

It also turned up gaps neither tool had: **Dependabot on this repo scans npm and
pip only**, so `Cargo.lock` had no coverage anywhere, and it holds three RUSTSEC
advisories.

**Fix it before you baseline it.** The same scan showed the `[tool.uv]`
constraint floor was `gitpython>=3.1.50`, which is the exact version all nine of
its High advisories are filed against. Raising it to `>=3.1.55` took one line
and removed nine findings. Baselining them would have been the easier and worse
answer. The counter-example is in the same commit: constraining `json-repair` to
its fixed version forced a 403-line re-resolution that downgraded `tomli` and
pulled in `textual` and `pendulum`, so it is baselined with that measurement
written down. Try the upgrade, measure the blast radius, then decide.

Every baseline entry needs a reason and `check_osv.py` errors without one. When
a dependency moves and an entry stops being reported the script prints a
`::notice::` rather than failing, because failing on good news is how a check
gets ignored.

## Source SAST

`opengrep` 1.26.0, with `opengrep/opengrep-rules` pinned at commit
`f1d2b562`, over `headroom/ sdk/ plugins/` (1106 files, about 90 seconds).

**Why the fork of the rules and not Semgrep's.** Semgrep relicensed:
`semgrep/semgrep-rules` is now under the proprietary "Semgrep Rules License
v1.0". `opengrep/opengrep-rules` was taken before that and stays LGPL-2.1 plus
the Commons Clause, which forbids *selling* a service whose value derives from
the rules. Scanning our own private repos is not that. OpenGrep also has no
telemetry to disable, which is one fewer thing for the egress rule to police.

**Three tiers, decided by each rule's own metadata**, in
`scripts/fork/check_opengrep.py`:

| tier | count here | behaviour |
|---|---|---|
| `security` + `vuln` | 48 | blocks, against `ci/sast-baseline.txt` |
| `security` + `audit` | 85 | reported in the step summary, never blocks |
| everything else | 297 | dropped |

That last row is the load-bearing one. The rule set finds 201 maintainability,
63 best-practice and 33 correctness issues on this tree, and ruff, mypy and
clippy already own that ground. Run the whole set unfiltered and the gate is red
on arrival with 430 findings, 130 of them at ERROR, of which 60 are
`useless-inner-function` firing on FastAPI handlers registered inside factories,
which is idiomatic. A gate nobody can act on is a gate nobody reads.

**Measured against CodeQL, not assumed equivalent.** It reproduces the classes
that mattered and finds three CodeQL did not (file permissions, path traversal,
cleartext transport). It does *not* reproduce CodeQL's 16x
`py/incomplete-url-substring-sanitization` or 19x `py/stack-trace-exposure`;
both land in the non-blocking audit tier. That is a real loss, written down
here rather than papered over.

**The baseline counts per rule, not per finding.** Upstream lands ~20 commits a
day and nearly every finding is in code this fork does not own, so a baseline
keyed on fingerprint or `file:line` would repaint itself on every sync. A
per-rule ceiling still fails on the twelfth md5 call and survives code moving
between files. The tradeoff, taken deliberately: deleting one finding and adding
another under the same rule nets to zero and passes.

**Scan errors are baselined too**, under `_scan-error:parse` and
`_scan-error:timeout`. A file OpenGrep cannot parse is a file OpenGrep is not
scanning. Two are known: `headroom/transforms/code_compressor.py` does not parse
at all, and the flask SSRF rule times out on `headroom/cli/proxy.py`. Both are
holes in coverage, and a third appearing has to be acknowledged in a diff
instead of scrolling past in a log.

**The rules repo is archived** (last commit 2025-01-26). Injection, crypto and
permission patterns do not rot quickly, so this is serviceable, but it will not
learn a new class on its own. Revisit the pin rather than trusting it.

## Rust

Nothing in this directory looked at the five crates until 2026-07-29. OpenGrep
ships 334 Python and 198 JS/TS rules and **zero** for Rust, so the ~74k lines of
Rust had no static analysis at all from either the gate or the security
workflow.

**clippy is the Rust SAST**, in `fork-gate.yml`, at `-D warnings`, blocking.
`cargo-audit` is in `fork-security.yml`.

**Neither job installs a toolchain and neither uses `dtolnay/rust-toolchain`**,
which upstream's `rust.yml` uses and this repo's third-party allowlist forbids.
`ubuntu-latest` ships rustup, `rust-toolchain.toml` pins channel 1.95.0 with
rustfmt and clippy, and rustup installs exactly that on the first `cargo` call.
The pin is why a clippy lint added in a later stable cannot turn CI red without
a visible diff.

**The commands are not written in the YAML.** The Makefile already defines
`fmt-check` and `clippy`, and upstream's `rust.yml` runs the same two, so
`run-gate.sh rust` calls those. A third copy is a third thing to drift.

**`cargo-audit` overlaps `osv-scanner` on purpose.** Both read `Cargo.lock`
against RustSec. `cargo audit` additionally fails on **yanked** crates, which
OSV does not model. If this job is ever cut, yanked-crate detection goes with
it. Its ignore list is read out of `ci/vuln-baseline.txt` rather than restated,
so removing an entry there turns this job red, which is what keeps that file the
actual record of what is accepted.

## Quarantine

`ci/quarantine.txt` lists tests `fork-gate.yml` deselects, with a reason each.
The inherited suite has failures this fork did not cause, and a gate that is red
on arrival gets ignored within a week.

`scripts/fork/check_quarantine.py` caps the list at a ceiling recorded in the
script. Raising it is a diff, in a PR, with a reason. A separate non-blocking job
runs the quarantined tests anyway, so an entry that starts passing shows up in
the run output instead of living there forever.

**The list is currently empty and the suite is green** (run 30455809107,
2026-07-29: 9886 passed, 576 skipped, 0 failed). The ceiling is 5, which is
headroom for a genuine flake, not a budget to spend.

**Measure on a runner, not locally, before adding an entry.** A local run the
same day showed 8 failures and every one of them passes in CI: three
opentelemetry tracing tests, four proxy-health tests that fail only on suite
ordering, and one that shells out to a `cargo` this container lacks.
Quarantining on that measurement would have skipped 8 healthy tests. This
container also ships a 32-bit `sqlite_vec/vec0.so` (an upstream packaging bug
in sqlite-vec 0.1.6's aarch64 wheel), which makes `SQLITE_VEC_AVAILABLE` false
and errors out every memory-backed test. All properties of one machine.

## Related

- #32 the allowlist design, and the per-workflow reasons
- #11 the original decision to disable Actions wholesale
- #13 sync cadence, drift alerting, branch protection
- #7 CI supply chain: SHA pinning, verified downloads, scoped tokens
- #9 egress and data-sovereignty audit
