#!/usr/bin/env bash
# The gate. One definition, two callers: .github/workflows/fork-gate.yml and
# .claude/pre-push-gate.sh. If those two ever disagree about what "green"
# means, a local pass stops predicting a CI pass and the gate is decorative.
# Adding a check here is the only supported way to add a check.
#
# Usage: scripts/fork/run-gate.sh [lint|format|types|test|quarantine|all]
set -euo pipefail

cd "$(dirname "$0")/../.."
TARGET="${1:-all}"
QUARANTINE_FILE="ci/quarantine.txt"

# mypy runs through uvx at a pinned version, NOT `uv run --with mypy`. The
# project pins `python_version = "3.10"` in [tool.mypy]; resolving mypy inside
# the project environment drags in the numpy stubs bound to the *runtime*
# interpreter, and the version skew reports errors that do not exist. This cost
# a debugging session on 2026-07-28. Do not "simplify" it back.
MYPY_VERSION="1.20.2"

# ruff's version is NOT hardcoded here. pyproject pins it in the `dev` extra and
# scripts/verify-ruff-version.py is the repo's existing reader for that pin;
# upstream's ci.yml uses the same script. A second copy of the number here is a
# second thing to forget.
ruff_version() { python3 scripts/verify-ruff-version.py --print-version; }

# Every tool runs from a provisioned environment, never from whatever the
# working copy happens to have lying around.
#
#   lint/format/types -> uvx, pinned. They need no project dependencies, so the
#                        static job stays fast and independent of the ~600
#                        package install.
#   test              -> `uv run --extra dev`, because pytest, respx,
#                        sqlite-vec and sentence-transformers all live in the
#                        `dev` extra.
#
# This is not a style preference. The first CI run of this gate failed with
# `Failed to spawn: ruff` and `Failed to spawn: pytest`: a fresh checkout
# installs only the 58 base packages, while the machine it was written on
# already had the dev extra. The gate passed locally for a reason unrelated to
# the code. Provisioning explicitly is what makes a local pass mean anything.
UV_TEST_EXTRAS=(--extra dev)

# Use the host's CPython, NOT a uv-managed standalone build.
#
# The managed build was tried and reverted. It is attractive because it carries
# its own Python.h, which the `dev` extra needs to compile hnswlib. But
# python-build-standalone ships its own OpenSSL and starts with an EMPTY trust
# store, and tests/test_ssl_context.py asserts the opposite: that headroom's TLS
# path keeps the system CAs (`cert_store_stats()["x509_ca"] > 1`). Under the
# managed interpreter that count is 0, and two tests fail for a reason that has
# nothing to do with headroom's code.
#
# A gate must not change the semantics of what it measures to make itself
# easier to run. The distro interpreter is what headroom actually runs on, so
# that is what the suite runs on. Hosts without the matching python3-dev
# package cannot build hnswlib and therefore cannot run the `test` target
# locally; that is a property of the host, and CI is the authority.
export UV_PYTHON="${UV_PYTHON:-3.12}"
export UV_PYTHON_PREFERENCE="${UV_PYTHON_PREFERENCE:-system}"

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

# Reads ci/quarantine.txt and echoes one --deselect argument per live entry.
# Format is `<pytest node id>  # <reason>`; blanks and full-line comments skip.
quarantine_args() {
  [ -f "$QUARANTINE_FILE" ] || return 0
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | sed -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//')"
    [ -n "$line" ] || continue
    printf -- '--deselect\n%s\n' "$line"
  done < "$QUARANTINE_FILE"
}

quarantine_ids() {
  [ -f "$QUARANTINE_FILE" ] || return 0
  sed -e 's/#.*//' -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//' "$QUARANTINE_FILE" | grep -v '^$' || true
}

run_lint()   { local v; v="$(ruff_version)"; step "ruff check (${v})";        uvx "ruff==${v}" check .; }
run_format() { local v; v="$(ruff_version)"; step "ruff format --check (${v})"; uvx "ruff==${v}" format --check .; }
run_types()  { step "mypy ${MYPY_VERSION}"; uvx "mypy==${MYPY_VERSION}" headroom --ignore-missing-imports; }

run_test() {
  step "pytest (quarantined tests deselected)"
  local args=()
  while IFS= read -r a; do args+=("$a"); done < <(quarantine_args)
  echo "quarantined: $(( ${#args[@]} / 2 )) test ids"
  uv run --frozen "${UV_TEST_EXTRAS[@]}" pytest "${args[@]}" --tb=short -q
}

# Non-blocking. Runs exactly what the gate skipped, so the debt stays visible
# instead of disappearing behind a green check.
run_quarantine() {
  step "pytest (quarantined tests only — reporting, never blocking)"
  local ids=()
  while IFS= read -r a; do ids+=("$a"); done < <(quarantine_ids)
  if [ ${#ids[@]} -eq 0 ]; then echo "quarantine is empty"; return 0; fi
  uv run --frozen "${UV_TEST_EXTRAS[@]}" pytest "${ids[@]}" --tb=short -q || true
}

case "$TARGET" in
  lint)       run_lint ;;
  format)     run_format ;;
  types)      run_types ;;
  test)       run_test ;;
  quarantine) run_quarantine ;;
  all)        run_lint; run_format; run_types; run_test ;;
  *)          echo "unknown target: $TARGET" >&2; exit 2 ;;
esac
