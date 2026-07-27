# Fork Charter

**This is a patched fork of [headroomlabs-ai/headroom](https://github.com/headroomlabs-ai/headroom). Read this before you touch anything.**

If you are an agent picking this up cold, this file plus `git log` is the whole context you need. Everything below was verified by probe, not assumed.

---

## Why this fork exists

> "This fork is for me to be able to leverage PAI and Claude Code and dramatically reduce my token usage and not bump up against the limits of my Claude Code subscription."
> — M, 2026-07-26

Personal tooling for one operator, in service of PAI. That sentence decides more than it looks like it does:

- **The success metric is rate-limit relief**, measured in 429s and context headroom. Not compression ratio. A compression win that does not reduce limit pressure is not a win.
- **No redistribution.** Apache-2.0 section 4 attribution obligations stay dormant. If that ever changes, revisit.
- **Not production infrastructure.** The SanMarcSoft Nix Law and Scaleway registry law do not apply here. Nothing in this repo is built or shipped as a production image.
- **No upstream pull requests. Ever.** M directive, 2026-07-26. This is a private divergent asset, not a contribution staging area.
- **Divergence stays minimal.** Every local patch is permanent maintenance against roughly 5.5 upstream commits a day touching the same paths. Patch security, and only otherwise when the token objective genuinely requires it.

---

## Run it safely, in one command

```bash
/config/workspace/projects/bin/headroom-claude.sh
```

**It must be run from a real terminal.** Claude Code needs a TTY; launching it from inside another Claude Code session makes it fall back to `--print` mode and fail. The launcher checks for this and tells you.

What the launcher guarantees, all verified live on 2026-07-26:

| Guarantee | How it was verified |
|---|---|
| Binds `127.0.0.1` only, never `0.0.0.0` | `ss -ltn` shows `127.0.0.1:8787` |
| Unauthenticated `/v1/*` refused | `POST /v1/messages` without a token returns **401** |
| SSRF to cloud metadata refused | `x-headroom-base-url: http://169.254.169.254` returns **400** |
| SSRF to loopback refused | `x-headroom-base-url: http://127.0.0.1:6333` returns **400** |
| No prompt content leaves the box | `HEADROOM_API_KEY` never set, so cloud mode is off |
| No telemetry | no license key, `HEADROOM_UPDATE_CHECK=off` |
| No memory collision with PAI | Serena and headroom memory both off |

**Do not run bare `headroom proxy --host 0.0.0.0`.** The whole safety argument rests on the loopback bind.

---

## Gotchas that will cost you an hour each

**1. The system install is broken. Ignore it.**
`/opt/uv-tools/headroom-ai` fails with `ModuleNotFoundError: fastapi`. Do not repair it. Run the patched source directly:

```bash
cd <this repo> && uv run headroom --version   # 0.33.0-dev, Rust core present
```

**2. `--1m` silently downgrades your model.**
`headroom wrap claude --1m` appends the `[1m]` suffix that unlocks the 1M context window, but when `ANTHROPIC_MODEL` is unset it falls back to its own hardcoded default (`claude-opus-4-8`, `headroom/cli/wrap.py:243`). Always set `ANTHROPIC_MODEL` explicitly. The launcher does.

**3. `wrap claude` writes into your working tree.**
It creates `.claude/settings.local.json` in the current repo containing `env.ANTHROPIC_BASE_URL`. That is durable and directory-scoped: **every** later Claude Code session started in that directory routes through the proxy, with no further opt-in. Know it is there.

**4. The self-heal hook it installs is broken.** See issue #16.
The same file installs a `SessionStart` hook pointing a **claude** binary at a **headroom** subcommand, at a path that does not exist. It can never run. That hook is what clears a stale `ANTHROPIC_BASE_URL` when the proxy dies, so without it a hard proxy death bricks every later `claude` in that directory with `ConnectionRefused`.

**5. Remote Control dies.** Not fixable.
`/rc` mobile mirroring is deterministically disabled behind any custom `ANTHROPIC_BASE_URL` from Claude Code 2.1.196. The gate is inside the Claude Code binary and headroom explicitly cannot restore it (`headroom/providers/claude/runtime.py:25-32`). Run plain `claude` for sessions where you want your phone.

**6. Most of the apparent benefit may be self-inflicted repair.**
Pointing Claude Code at any custom base URL breaks tool-schema deferral (#746) and the 1M window (#1158). headroom then repairs both. So a naive before/after comparison measures headroom undoing damage its own presence caused. **Nothing here is measured yet.** Run the ablation below before believing any number.

---

## The measurement that decides whether this is worth running

Four arms, interleaved A/B/A/B at real subagent concurrency, minimum ten paired runs:

| Arm | Configuration |
|---|---|
| 0 | Direct API, no proxy, `/rc` intact |
| 1 | Proxy with **only** `ENABLE_TOOL_SEARCH=true`, everything else off |
| 2 | Arm 1 plus CacheAligner |
| 3 | Arm 2 plus live-zone compression |

**Run Arm 1 against Arm 0 first.** If Arm 1 captures most of the benefit, compression is not what is helping and the honest answer is a config change with no proxy at all.

Measure: 429 count, `retry-after` values, wall clock, `cache_read_input_tokens` vs `cache_creation_input_tokens`, context headroom at failure.

**Cheapest kill shot:** two runs back to back. If `cache_creation_input_tokens` stays nonzero on run two, cache writes are replacing reads, rate pressure gets *worse*, and the whole proposal dies. Stop there.

---

## What we changed, and why

Five commits on top of upstream. Every SHA256 and action SHA in them was resolved against the live GitHub API and the published release artifacts, never inferred.

| Commit | What |
|---|---|
| `05f91658` | RCE guard (`trust_remote_code` on request-supplied model strings), first SSRF pass, credential and memory files pre-created at `0600`, supply-chain pins, CI SHA-pinning, egress hygiene |
| `879242e2` | Installer binds the proxy to loopback across **all three** publish sites; OpenClaw unsafe-install gated; agent config mounts documented |
| `65f45155` | SSRF enforced at the **ASGI boundary** instead of one handler, after a review found three of four sinks unguarded |
| `51206b33` | Connect-time IP pinning, closing DNS rebinding |
| `b90c0d79` | Docker image publishing made opt-in and off by default |

### The two findings worth understanding before you change anything

**A guard on one door of four.** The first SSRF fix was applied inside `handlers/openai.py`. Three other sinks read the same header unvalidated, so this reached the cloud metadata service:

```
GET /latest/meta-data/iam/security-credentials/
x-headroom-base-url: http://169.254.169.254
```

Policy now lives in `headroom/proxy/ssrf.py` and is enforced once, in middleware, before routing. **If you add a new consumer of that header, route it through the shared guard.** A regression test in `tests/test_proxy/test_ssrf_boundary.py` tries to catch you; it is deliberately weak (see issue list) so do not rely on it alone.

**A passing test suite does not prove a module imports.** Agents were interrupted mid-write and left `binaries.py` syntactically invalid, a duplicate `_mirror_url` where the original shadowed the hardened one (making the fix inert while its tests passed), and a `NameError` waiting in `wrap.py`. A corrupt module makes its own tests *error*, not fail, which reads as green at a glance. **Always `py_compile` and marker-scan every changed file before trusting a test run.**

---

## Verification gate

Do not report anything as working on a green test run alone.

```bash
uv run pytest -q                    # expect ~9321 passed
uv run ruff check headroom/ tests/
uv run ruff format --check headroom/ tests/
```

**Six failures and 14 errors are pre-existing and also fail on a clean upstream checkout.** Do not chase them:

- `test_observability_tracing` (3)
- `test_proxy_healthchecks::test_readyz_reports_memory_backend_when_enabled`
- `test_release_workflows::test_no_native_tls_in_wheel_build_tree` (needs `cargo`, absent in the container)
- `test_startup_log_noise::test_huggingface_hub_logger_is_error_or_higher` (test-order dependent)
- `test_memory_bridge` (14 errors)

To prove zero regressions, measure the baseline yourself rather than trusting this list:

```bash
git worktree add /tmp/hr-baseline <upstream-sha> --detach
cd /tmp/hr-baseline && uv run pytest -q
```

---

## Operating rules

- **GitHub Actions are disabled on the SanMarcSoft mirror.** Publishing is additionally gated behind the repo variable `HEADROOM_PUBLISH_IMAGES`, repeated on every job so reordering `needs` cannot re-enable it. If you enable Actions anywhere, check that gate first: this fork previously published cosign-signed images of unreviewed upstream code under a first-party identity.
- **Pin to a reviewed commit. Never track upstream `HEAD`.** Triage upstream commits touching the patched paths within the week; batch everything else.
- **Do not force-push `main`.** There is real divergence here now.
- Open work, including the findings a security review raised and we have not closed, is tracked in epic **#15**.

---

*Last verified 2026-07-26 against upstream `a6d4921e`.*
