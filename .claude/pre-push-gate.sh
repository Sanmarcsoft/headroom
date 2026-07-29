#!/usr/bin/env bash
# Pre-push gate. The PAI PrePushGate hook runs this before `git push` and
# blocks the push on a non-zero exit.
#
# It is a thin wrapper on purpose: the checks live in scripts/fork/run-gate.sh,
# which is the same script .github/workflows/fork-gate.yml calls. Duplicating
# the commands here is how a repo ends up with a local check that is looser
# than CI, which is the failure this file exists to prevent.
set -euo pipefail
exec "$(dirname "$0")/../scripts/fork/run-gate.sh" all
