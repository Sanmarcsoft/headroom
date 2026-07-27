#!/usr/bin/env bash
# Build the headroom sidecar image from THIS repository, pinned to the commit
# being built. Run on the Docker host that will run the container.
#
#   ./deploy/orbstack-sidecar/build.sh              # build, tag, print image ref
#   IMAGE_REPO=myorg/headroom ./build.sh            # override the repo name
#
# Why build here rather than pull ghcr.io/chopratejas/headroom:latest:
#   1. The upstream image lags this fork's security remediation.
#   2. A rolling :latest tag hides which code is actually running.
# The tag is derived from the git SHA so the running container is always
# traceable to a commit.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

IMAGE_REPO="${IMAGE_REPO:-sanmarcsoft/headroom}"
TARGET="${TARGET:-runtime}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "error: not a git repository: $REPO_ROOT" >&2
  exit 1
fi

SHA="$(git rev-parse --short=8 HEAD)"
TAG="git-${SHA}"
IMAGE="${IMAGE_REPO}:${TAG}"

if ! git diff --quiet HEAD -- . 2>/dev/null; then
  echo "warning: working tree is dirty; ${TAG} will not exactly match the commit" >&2
fi

echo "==> building ${IMAGE}"
echo "    repo:   ${REPO_ROOT}"
echo "    target: ${TARGET}"
echo "    arch:   $(uname -m)"

# Built for the host's native architecture. This is a local development
# sidecar on Apple Silicon, not a production image for the x86_64 fleet, so
# the cross-compile rule for Scaleway/NAS images does not apply here.
docker build \
  --target "${TARGET}" \
  --tag "${IMAGE}" \
  --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
  --label "org.opencontainers.image.source=$(git config --get remote.origin.url || echo unknown)" \
  --file Dockerfile \
  .

echo
echo "==> built ${IMAGE}"
echo
echo "Set this in deploy/orbstack-sidecar/.env:"
echo "  HEADROOM_IMAGE=${IMAGE}"
