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

run_lint()   { step "ruff check";        uv run --frozen ruff check .; }
run_format() { step "ruff format --check"; uv run --frozen ruff format --check .; }
run_types()  { step "mypy ${MYPY_VERSION}"; uvx "mypy==${MYPY_VERSION}" headroom --ignore-missing-imports; }

run_test() {
  step "pytest (quarantined tests deselected)"
  local args=()
  while IFS= read -r a; do args+=("$a"); done < <(quarantine_args)
  echo "quarantined: $(( ${#args[@]} / 2 )) test ids"
  uv run --frozen pytest "${args[@]}" --tb=short -q
}

# Non-blocking. Runs exactly what the gate skipped, so the debt stays visible
# instead of disappearing behind a green check.
run_quarantine() {
  step "pytest (quarantined tests only — reporting, never blocking)"
  local ids=()
  while IFS= read -r a; do ids+=("$a"); done < <(quarantine_ids)
  if [ ${#ids[@]} -eq 0 ]; then echo "quarantine is empty"; return 0; fi
  uv run --frozen pytest "${ids[@]}" --tb=short -q || true
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
