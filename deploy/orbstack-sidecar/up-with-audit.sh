#!/usr/bin/env bash
# (Re)launch the headroom sidecar with OTEL metric export to the estate's AI audit.
#
# Runs where `pass` lives (the dev container), not on the Docker host: the host
# has no pass and no gpg. The intake bearer token is read with `pass show` here,
# checked against the intake, and handed to `docker compose` on the host over ssh
# STDIN. It is never written to .env, never committed, and never in any argv
# (Sanmarcsoft/headroom#73; no-plaintext-secrets rule).
#
# What it cannot hide: compose passes the header into the container's environment,
# so `docker inspect headroom` on the host shows it, exactly as it already shows
# HEADROOM_PROXY_TOKEN. Anyone with the host's docker socket can read both.
#
#   ./up-with-audit.sh             # recreate with export ON
#   ./up-with-audit.sh --off       # recreate with export OFF (the rollback)
#   ./up-with-audit.sh --dry-run   # check token + intake, print the plan, change nothing
#
# `docker compose up -d` recreates the container only when its config changed, so a
# second run with the same settings is a no-op. A recreate drops in-flight requests
# through the proxy and takes up to the 90 s health start period; pick a quiet moment.
#
# Metrics are counts, token totals and timings with provider/model labels only.
# Langfuse stays off: this script sets nothing for it.

set -euo pipefail

DOCKER_HOST_SSH="${DOCKER_HOST_SSH:-m4mini2}"
STACK_DIR="${STACK_DIR:-/Users/matt/stacks/headroom/deploy/orbstack-sidecar}"
INTAKE="${AUDIT_OTLP_INTAKE:-http://10.0.0.12:31391}"
TOKEN_ENTRY="${AUDIT_OTLP_PASS_ENTRY:-sanmarcsoft/ai-audit/otlp-token}"
SERVICE_NAME="${HEADROOM_OTEL_SERVICE_NAME:-headroom-proxy}"
RESOURCE_ATTRS="${HEADROOM_OTEL_RESOURCE_ATTRIBUTES:-audit.host=m4mini2-sidecar,service.namespace=headroom}"
REMOTE_PATH='/usr/local/bin:/opt/homebrew/bin:$HOME/.orbstack/bin:$PATH'

mode=on
case "${1:-}" in
  "") ;;
  --off) mode=off ;;
  --dry-run) mode=dry ;;
  *) echo "usage: $0 [--off|--dry-run]" >&2; exit 2 ;;
esac

die() { printf 'up-with-audit: %s\n' "$1" >&2; exit 1; }

if [[ "$mode" == off ]]; then
  ssh -o BatchMode=yes "$DOCKER_HOST_SSH" \
    "export PATH=$REMOTE_PATH; cd '$STACK_DIR' && HEADROOM_OTEL_METRICS_ENABLED=0 docker compose up -d"
  echo "up-with-audit: export OFF, sidecar recreated if its config changed"
  exit 0
fi

token=$(pass show "$TOKEN_ENTRY" 2>/dev/null | head -n1 | tr -d '\r\n') || true
[[ -n "$token" ]] || die "pass show $TOKEN_ENTRY failed (cold gpg-agent?)"
# The headers variable is comma-separated key=value; a comma or space would split it.
[[ "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || die "token has characters the header list cannot carry"

# The intake must accept this token before we hand it to the proxy. Header via stdin.
code=$(printf 'Authorization: Bearer %s\n' "$token" \
  | curl -s -m 10 -o /dev/null -w '%{http_code}' -H @- -H 'Content-Type: application/json' \
      -X POST --data '{"resourceMetrics":[]}' "${INTAKE}/v1/metrics") || true
[[ "$code" == 200 ]] || die "intake ${INTAKE}/v1/metrics refused the token (HTTP ${code:-none})"

echo "up-with-audit: intake accepts the token; target ${DOCKER_HOST_SSH}:${STACK_DIR}"
if [[ "$mode" == dry ]]; then
  echo "up-with-audit: dry run, nothing changed"
  exit 0
fi

# Remote side reads the header from stdin, so it never appears in a process list.
printf 'Authorization=Bearer %s\n' "$token" | ssh -o BatchMode=yes "$DOCKER_HOST_SSH" \
  "export PATH=$REMOTE_PATH; IFS= read -r h; cd '$STACK_DIR' || exit 1; \
   HEADROOM_OTEL_METRICS_HEADERS=\"\$h\" \
   HEADROOM_OTEL_METRICS_ENABLED=1 \
   HEADROOM_OTEL_METRICS_ENDPOINT='${INTAKE}/v1/metrics' \
   HEADROOM_OTEL_SERVICE_NAME='${SERVICE_NAME}' \
   HEADROOM_OTEL_RESOURCE_ATTRIBUTES='${RESOURCE_ATTRS}' \
   docker compose up -d"
echo "up-with-audit: export ON, sidecar recreated if its config changed"
