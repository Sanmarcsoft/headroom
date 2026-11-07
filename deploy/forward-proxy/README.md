# hrfwd: headroom savings without losing Remote Control

Claude Code turns off `/remote-control` whenever `ANTHROPIC_BASE_URL` names any host other than
`api.anthropic.com` (headroomlabs-ai/headroom#1779). The gate checks the string, not where the traffic
goes. So pointing a session at the headroom sidecar used to cost you `/rc`.

`hrfwd` routes around the gate. It is a per-session loopback forward proxy that Claude Code reaches
through `HTTPS_PROXY`, and `ANTHROPIC_BASE_URL` stays unset.

| Traffic | Where it goes |
|---|---|
| `CONNECT api.anthropic.com:443`, then `POST /v1/messages` | headroom sidecar, with `x-headroom-proxy-token` |
| `CONNECT api.anthropic.com:443`, any other request | the real `api.anthropic.com`, token stripped |
| `CONNECT` to any other host (the /rc bridge, claude.ai, MCP) | blind TCP tunnel, never decrypted |

If the sidecar is unreachable, model calls get a 502. The proxy never quietly falls back to the direct API.

## Trust

Each session gets its own run directory (0700, removed on exit) holding a fresh local CA, the TLS
terminator's unix socket and the bound port. The CA carries a critical `nameConstraints` extension:
DNS names are limited to `api.anthropic.com` (and, inherently, its subdomains) and every IPv4 and IPv6
address is excluded, so its key cannot impersonate any other site. It lives 90 days and dies with the session.

The CONNECT port is on 127.0.0.1 and demands a random per-session secret (`Proxy-Authorization`,
carried in the `HTTPS_PROXY` URL); anything without it gets 407. The TLS terminator listens only on the
unix socket, so no loopback process can reach the token-adding path without the secret.

If the client goes away mid-stream (Esc in Claude), the request to headroom is torn down at once, so
the discarded answer does not keep streaming. If the launcher dies, even by SIGKILL, `hrfwd` notices
its parent is gone and exits.

## Use

`ccs` (and therefore `be <persona>` and the zsh `claude` function) runs this lane by default. It starts
`hrfwd` under `setsid`, so Ctrl-C in Claude never reaches it, and kills it when the session ends.

```bash
HEADROOM_LANE=base ccs   # old ANTHROPIC_BASE_URL pairing (no /rc)
HRFWD_DRY_RUN=1 ccs      # print the launch environment (secret redacted) and exit
bun test                 # in this directory
```

Known limit: non-Node clients inside a session (curl, Python) that call `api.anthropic.com` do not read
`NODE_EXTRA_CA_CERTS` and will reject the local leaf. Nothing in the estate does that from inside a
session today.

State of record: [`ISA.md`](ISA.md).
