# Headroom sidecar for code-server (OrbStack / Docker)

Runs the Headroom optimization proxy as its own long-lived container beside a
`code-server` container on the same Docker host, so an agent running inside
code-server gets token savings by default.

## The problem this solves

Headroom's native runtime can be started by a client hook (for example
`headroom init hook ensure` wired into a Claude Code `SessionStart` hook). That
process is owned by nothing: `headroom install status` reports
`Supervisor: none`, and the process is reparented to PID 1 when the launching
session exits. Between sessions, and after any container rebuild, nothing
listens on the port and every request fails with `ConnectionRefused`.

This deployment gives the proxy an owner: a container with a restart policy.

## Quick start

On the Docker host, with this repository checked out:

```bash
# 1. Build an image pinned to the current commit
./deploy/orbstack-sidecar/build.sh

# 2. Configure
cd deploy/orbstack-sidecar
cp .env.example .env
$EDITOR .env          # paste the HEADROOM_IMAGE printed by build.sh,
                      # and set CODE_SERVER_NETWORK

# 3. Run
docker compose up -d
docker compose ps     # wait for STATUS = healthy
```

Then point the client at it. Inside the code-server container:

```
ANTHROPIC_BASE_URL=http://headroom:8787
```

For Claude Code that goes in the `env` block of `~/.claude/settings.json`.
`ANTHROPIC_BASE_URL` is read at process start, so restart the client afterwards.

## Design decisions

**Not published to the host.** There is no `ports:` mapping. The proxy relays
requests carrying the caller's Anthropic credentials; anything able to reach it
can spend them. Only containers on the shared network can connect. The proxy
binds `0.0.0.0` *inside* the container, which is safe only because the port is
never published.

**Attaches to the existing network.** `code-server-net` is declared
`external: true`, so compose joins the network code-server already uses rather
than creating a second one. Container-to-container DNS resolves the service
name `headroom`. Find the network with:

```bash
docker inspect code-server \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}'
```

**No Sablier label.** Sablier scales labelled containers to zero when idle.
That is right for an interactive IDE and wrong for a proxy: the first request
after an idle period would get `ConnectionRefused`, which is the failure this
sidecar exists to remove. Adding `sablier.enable=true` here would reintroduce
it.

**Named volume, not a bind mount.** `headroom-state` holds cache, memory and
config across container recreation. Upstream's `headroom deploy` bind-mounts
the host's `~/.claude`, `~/.codex` and `~/.gemini` directories into the
container; this does not.

**Image pinned to a git SHA.** Never a rolling `:latest`. A rolling tag is how
a stale image silently lingers after a rebuild.

**Built from this fork, not `ghcr.io/chopratejas/headroom:latest`.** The
upstream image lags this fork's security remediation.

## Why not `headroom deploy --image`

It generates no compose file. It shells out to `docker run` directly, and its
defaults conflict with this design: it publishes the port to the host, sets no
`--network` (so the container lands on the default bridge and the DNS name does
not resolve), bind-mounts the operator's credential directories, runs
`docker rm -f` on the existing container, and rewrites detected clients'
configuration files. It does get `--restart unless-stopped` right.

## Behaviour under failure

The proxy **fails open** on the compression path: compression and tokenizer
work runs on an executor bounded by `COMPRESSION_TIMEOUT_SECONDS` (default 30s)
and, on timeout or error, the original request is forwarded verbatim rather
than failing. A slow or broken optimizer costs latency, not correctness.

Unreachable upstream returns `502` promptly. Neither path hangs, so a long
client-side timeout is not at risk of a stall.

## Verifying

```bash
# container is healthy and has an owner
docker inspect headroom --format '{{.State.Health.Status}} {{.HostConfig.RestartPolicy.Name}}'

# reachable by DNS name from the client container (not from the host)
docker exec code-server curl -sf http://headroom:8787/health

# survives a restart
docker restart headroom && sleep 20 && docker exec code-server \
  curl -sf http://headroom:8787/health

# traffic is actually flowing (api_requests should climb)
docker exec code-server curl -s http://headroom:8787/stats
```

`api_requests: 0` means the client is not routing through the proxy, however
healthy it looks.
