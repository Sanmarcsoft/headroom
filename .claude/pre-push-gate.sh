#!/usr/bin/env bash
# Pre-push gate. The PAI PrePushGate hook runs this before `git push` and
# blocks the push on a non-zero exit.
#
# It is a thin wrapper on purpose: the checks live in scripts/fork/run-gate.sh,
# which is the same script .github/workflows/fork-gate.yml calls. Duplicating
# the commands here is how a repo ends up with a local check that is looser
# than CI, which is the failure this file exists to prevent.
set -euo pipefail

# Drop a VIRTUAL_ENV that points at an interpreter which is not there. The
# hook runs in a fresh shell that inherits the session's environment, and a
# devcontainer can export VIRTUAL_ENV for a venv it never created. pyo3's
# build script reads VIRTUAL_ENV before anything else, so `make clippy` then
# dies with:
#
#     error: failed to run the Python interpreter at /lsiopy/bin/python:
#     No such file or directory (os error 2)
#
# which reads like a Rust failure and is not one. Only a stale value is
# dropped: a VIRTUAL_ENV with a real interpreter is left exactly as it is, so
# this cannot quietly move the gate onto a different Python than the developer
# intended. Likewise, cargo living somewhere off the hook's PATH is a host
# problem, and run-gate.sh already fails closed on it rather than skipping the
# Rust checks.
if [ -n "${VIRTUAL_ENV:-}" ] && [ ! -x "${VIRTUAL_ENV}/bin/python" ]; then
  echo "pre-push-gate: VIRTUAL_ENV=${VIRTUAL_ENV} has no interpreter; ignoring it" >&2
  unset VIRTUAL_ENV
fi

exec "$(dirname "$0")/../scripts/fork/run-gate.sh" all
