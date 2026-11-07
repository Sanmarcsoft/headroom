---
task: "headroom savings and /rc in one lane"
slug: 20260927-183000_headroom-forward-proxy
phase: climbing
progress: 15/16
started: 2026-09-27T18:30:00Z
updated: 2026-09-27T19:20:00Z
principal_stated_goal: "can you devise a way we can get all these features working together while still being able to leverage /rc"
principal_stated_goal_source: conversation
principal_stated_goal_signal: 1
principal_stated_goal_locked: 2026-09-27T18:24:00Z
---

## Goal

One launch lane gives a persona session (`be <persona>`) the LifeOS system prompt, claude-peers channels,
headroom token savings AND `/remote-control`. The lane never sets `ANTHROPIC_BASE_URL`; instead a per-session
loopback forward proxy (`hrfwd`) terminates TLS for `api.anthropic.com` only and sends `POST /v1/messages`
to the headroom sidecar. Every other request and every other host passes through untouched.

## Vision

M launches `be q` from a laptop, walks away, and continues the same session from the phone, while the
sidecar's `/stats` keeps climbing. He never has to pick between the savings lane and the remote lane again.

## Out of Scope

- Changing headroom's Python proxy. The compression core is reused as-is; upstream #1779 stays open.
- A long-lived daemon. `hrfwd` lives and dies with one Claude session.

## Constraints

- Bun + TypeScript. No new dependencies; certificates come from `openssl`.
- The headroom token is read only from `~/.config/headroom/proxy-token` under the existing hygiene checks.
- The CA key never leaves the box and is mode 0600.

## Claims

- [x] C1: `POST /v1/messages` for api.anthropic.com arrives at headroom with the proxy-token header and an intact body. — `hrfwd.test.ts` C1
- [x] C2: Any other api.anthropic.com request (e.g. `/v1/sessions`) goes to the real upstream, not headroom. — test C2
- [x] C3: Anti: the headroom token never reaches api.anthropic.com, including when the client itself sends one. — test C3
- [x] C4: CONNECT to any other host is a blind TCP tunnel; the client sees that host's own certificate. — test C4
- [x] C5: Anti: headroom down → the proxy answers 502 and never falls back to the direct upstream. — test C5
- [x] C6: SSE responses stream incrementally (first event reaches the client before the upstream finishes). — test C6
- [x] C7: The CA is name-constrained to `api.anthropic.com` (subdomains inherent) with all IPs excluded; leaves for other names or any IP fail verification. — tests C7, C7b
- [x] C8: Anti: `hrfwd` listens on 127.0.0.1 only. — test C8
- [x] C9: `ccs` launches with `HTTPS_PROXY` + `NODE_EXTRA_CA_CERTS` and without `ANTHROPIC_BASE_URL`, and kills `hrfwd` on exit. — `HRFWD_DRY_RUN=1` under a pty; no hrfwd left after exit
- [x] C10: Live: a real request to api.anthropic.com through `hrfwd` lands in the sidecar (sidecar request counter rises) and Anthropic answers. — sidecar `failed` 1→2 on no-key 401; Bun client rejects without the CA
- [x] C12: Anti: a client abort tears down the headroom request within 500 ms. — test C12
- [x] C13: Anti: CONNECT without the per-session secret gets 407 and never reaches headroom. — test C13; e2e wrong secret → 407
- [x] C14: The TLS terminator listens on a 0600 unix socket, not a TCP port. — test C14
- [x] C15: Intercept matching ignores case and a trailing dot, and needs port 443. — test C15
- [x] C16: Anti: `hrfwd` never outlives its launcher, even when the launcher is SIGKILLed; the CA and socket are per-session and removed on exit. — e2e SIGKILL probe; dry run left no run dir
- [ ] C11: Live: in a `be` session, `/rc` is offered and connects while sidecar stats rise. (Needs M at a real terminal.)

## Test Strategy

| claim | type | check | threshold | tool |
| C1–C6, C8 | bun-test | `bun test deploy/forward-proxy` | all pass | bun test |
| C7 | bun-test | openssl verify of in-name and out-of-name leaves | in ok, out fails | bun test |
| C9 | bash | `bash -n` + launch dry-run printing env | env as claimed | bash |
| C10 | bash | curl via proxy, `/stats` before/after | counter +1, Anthropic 4xx/2xx JSON | curl |
| C11 | manual | M runs `be q`, `/rc` from phone | connects | M |

## Decisions

- Max review (2026-09-27, verdict "concerns") adopted in full: abort propagation (C12), IP-excluded and
  per-session CA (C7, C16), secret-gated CONNECT and unix-socket terminator (C13, C14), host normalization (C15),
  parent watchdog (C16), test key cleanup, NODE_EXTRA_CA_CERTS chaining. Deferred: checking project-level
  settings for ANTHROPIC_BASE_URL (would fail loudly, not leak). Noted, not adopted: the internal
  `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL` flag, underscore-prefixed and free to vanish in any release.

- Route only `POST /v1/messages` to headroom. `/v1/sessions` and `/v1/environments` (the /rc control plane,
  found in the 2.1.283 binary) must reach Anthropic directly; the bridge itself is
  `wss://bridge.claudeusercontent.com`, a different host, so it is tunnelled blind.
- Per-session process instead of a daemon: no systemd in this container, and the session's MCP children
  (which inherit `HTTPS_PROXY`) die with it.
- Known limitation: non-Node clients inside the session (curl, Python) calling api.anthropic.com would not
  trust the local CA. Nothing in the estate does that from inside a session today.
