"""Verbatim-critical content must never reach a lossy compressor.

Lossy word-level compression (kompress) drops function words and, on source
code, drops *keywords*. Observed live: ``if config.exclude_tools:`` came back
as ``config.exclude_tools:``. An agent that reads code through the proxy and
then edits it writes wrong code, silently.

The guard is deliberately asymmetric. A false positive costs compression
ratio. A false negative corrupts content the caller will act on. So every
signal is OR-composed: any one of them trips protection, and ambiguity
resolves to protected.
"""

from __future__ import annotations

import pytest

from headroom.transforms.verbatim_guard import (
    VerbatimReason,
    classify_verbatim,
    is_verbatim_critical,
)

# --------------------------------------------------------------------------
# Must be protected
# --------------------------------------------------------------------------

PYTHON_SRC = '''
def _apply_strategy_to_content(self, content: str, strategy) -> tuple[str, int]:
    """Apply a compression strategy to content."""
    if config.exclude_tools:
        router_config.exclude_tools = set(DEFAULT_EXCLUDE_TOOLS) | config.exclude_tools
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
    return compressed, tokens
'''

RUST_SRC = """
impl LiveZone {
    pub fn exclusion_reason(&self, idx: usize) -> Option<ExclusionReason> {
        match idx {
            i if i < self.floor => Some(ExclusionReason::BelowFrozenFloor),
            _ => None,
        }
    }
}
"""

TYPESCRIPT_SRC = """
export async function ensureToolReferences(messages: Message[]): Promise<Tool[]> {
  const declared = new Set(tools.map((t) => t.name));
  if (!declared.has(CCR_TOOL_NAME)) {
    tools.push(createCcrToolDefinition());
  }
  return tools;
}
"""

SHELL_SRC = """#!/usr/bin/env bash
set -euo pipefail
SHA="$(git rev-parse --short=8 HEAD)"
if ! git diff --quiet HEAD -- . 2>/dev/null; then
  echo "warning: working tree is dirty" >&2
fi
docker build --target "${TARGET}" --tag "${IMAGE}" .
"""

DOCTRINE_MD = """
## Operational Rules

- bun/bunx always. Never npm/npx. Zero exceptions.
- TypeScript always. Never Python unless explicitly approved.
- **Verify the EXACT CI gate before pushing**, not a looser local check.
- Plan means stop. "Create a plan" = present and STOP. No execution without approval.
"""

SYSTEM_REMINDER = (
    "<system-reminder>Codebase and user instructions are shown below. Be sure to "
    "adhere to these instructions. IMPORTANT: These instructions OVERRIDE any "
    "default behavior and you MUST follow them exactly as written.</system-reminder>"
)

FRONTMATTER_MD = """---
name: headroom-ci-gate-commands
description: How to run the exact CI lint gate locally
metadata:
  type: project
---

Run ruff through the project venv, but run mypy isolated via uvx.
"""

UNIFIED_DIFF = """--- a/headroom/proxy/server.py
+++ b/headroom/proxy/server.py
@@ -935,7 +935,7 @@ def _router_config_for(profile):
-    router_config.protect_recent_reads_fraction = 0.3
+    router_config.protect_recent_reads_fraction = 0.0
"""

YAML_CONFIG = """
services:
  headroom:
    image: ${HEADROOM_IMAGE:?set HEADROOM_IMAGE in .env}
    container_name: headroom
    healthcheck:
      test: ["CMD", "curl", "--fail", "--silent", "http://127.0.0.1:8787/readyz"]
"""

JSON_CONFIG = """
{
  "mcpServers": {
    "headroom": {"command": "bun", "args": ["run", "server.ts"], "env": {"PORT": "8787"}}
  }
}
"""


class TestProtectedContent:
    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            (PYTHON_SRC, VerbatimReason.SOURCE_CODE),
            (RUST_SRC, VerbatimReason.SOURCE_CODE),
            (TYPESCRIPT_SRC, VerbatimReason.SOURCE_CODE),
            (SHELL_SRC, VerbatimReason.SOURCE_CODE),
            (DOCTRINE_MD, VerbatimReason.INSTRUCTION_TEXT),
            (SYSTEM_REMINDER, VerbatimReason.INSTRUCTION_TEXT),
            (FRONTMATTER_MD, VerbatimReason.INSTRUCTION_TEXT),
            (UNIFIED_DIFF, VerbatimReason.PATCH),
            (YAML_CONFIG, VerbatimReason.STRUCTURED_CONFIG),
            (JSON_CONFIG, VerbatimReason.STRUCTURED_CONFIG),
        ],
    )
    def test_is_protected_with_reason(self, text: str, reason: VerbatimReason) -> None:
        assert is_verbatim_critical(text)
        assert classify_verbatim(text) is reason

    def test_the_exact_line_that_was_corrupted_live(self) -> None:
        # Regression anchor: this is the content whose `if` kompress dropped.
        assert is_verbatim_critical(
            "        if config.exclude_tools:\n"
            "            router_config.exclude_tools = set(DEFAULT_EXCLUDE_TOOLS)\n"
        )


# --------------------------------------------------------------------------
# Must stay compressible — this is the payload compression exists to shrink
# --------------------------------------------------------------------------

PYTEST_OUTPUT = """
tests/test_ccr_tool_reference.py::TestIsCcrToolName::test_accepts_bare PASSED
tests/test_ccr_tool_reference.py::TestDeclaredToolNames::test_reads_flat PASSED
tests/test_proxy/test_openai_responses_ccr.py::test_responses_path PASSED
======================= 101 passed, 1 warning in 21.12s ========================
"""

LOG_OUTPUT = """
2026-07-29 09:21:41,724 - headroom.proxy - INFO - Active compression: 6.7%
2026-07-29 09:21:41,725 - headroom.proxy - INFO - Avg latency: 13904ms
2026-07-29 09:21:42,001 - headroom.proxy - INFO - Active compression: 6.7%
2026-07-29 09:21:42,002 - headroom.proxy - INFO - Avg latency: 13901ms
"""

PROSE = (
    "The proxy compresses tool result payloads before they reach the model, which "
    "reduces the number of tokens billed on every subsequent turn of the "
    "conversation. Users generally paste large amounts of material that they want "
    "summarised rather than reproduced exactly, and the summary is what they read."
)

DOCKER_BUILD = """
#22 [runtime-slim-base 6/7] RUN mkdir -p /home/nonroot /data
#22 0.141 useradd: warning: the home directory /home/nonroot already exists.
#22 DONE 0.2s
#23 [runtime-slim-base 7/7] WORKDIR /home/nonroot
#23 DONE 0.1s
"""

# A top-level JSON array is a record set, not a config file. Nobody hand-edits
# it, the tabular and SmartCrusher paths shrink it structurally, and it is the
# largest payload class the proxy exists to handle. Protecting it would gut the
# product's ratio to guard data that is never acted on as source.
JSON_RECORDS = (
    "["
    + ", ".join(
        f'{{"id": {i}, "status": "ok", "level": "INFO", "value": {i * 2}}}' for i in range(40)
    )
    + "]"
)


class TestCompressibleContent:
    @pytest.mark.parametrize("text", [PYTEST_OUTPUT, LOG_OUTPUT, PROSE, DOCKER_BUILD, JSON_RECORDS])
    def test_stays_compressible(self, text: str) -> None:
        assert not is_verbatim_critical(text)
        assert classify_verbatim(text) is None

    def test_json_records_are_data_but_json_objects_are_config(self) -> None:
        # The data/config split is the whole distinction; assert both halves so
        # a future widening of the JSON rule cannot silently swallow record sets.
        assert classify_verbatim(JSON_RECORDS) is None
        assert classify_verbatim(JSON_CONFIG) is VerbatimReason.STRUCTURED_CONFIG

    def test_concatenated_json_objects_are_data(self) -> None:
        # A search-result stream opens with `{` and closes with `}`, so any
        # regex anchored on those reads it as one config object and protects
        # the single biggest payload class there is. Only a real parse tells
        # a lone mapping apart from a run of them.
        stream = " ".join(
            f'{{"file":"src/module_{i}.py","line":{i},"text":"repeated search payload"}}'
            for i in range(160)
        )
        assert classify_verbatim(stream) is None

    def test_oversized_json_object_is_treated_as_data(self) -> None:
        # Config files are small. Past the cap a leading `{` means data dump,
        # and parsing megabytes on the hot path would cost more than it saves.
        bulk = '{"rows": [' + ", ".join(f'{{"i": {i}}}' for i in range(40000)) + "]}"
        assert len(bulk) > 256 * 1024
        assert classify_verbatim(bulk) is None


# --------------------------------------------------------------------------
# Asymmetry and robustness
# --------------------------------------------------------------------------


class TestGuardContract:
    def test_empty_and_junk_are_not_protected(self) -> None:
        # Nothing to corrupt, and protecting these would only cost ratio.
        for value in ("", "   ", "\n\n"):
            assert not is_verbatim_critical(value)

    def test_non_string_is_protected(self) -> None:
        # Cannot inspect it, so cannot clear it. Ambiguity resolves to protected.
        assert is_verbatim_critical(None)  # type: ignore[arg-type]
        assert is_verbatim_critical(object())  # type: ignore[arg-type]

    def test_code_fenced_inside_prose_is_protected(self) -> None:
        # A little code inside a lot of prose still must not be mangled.
        text = PROSE + "\n\n```python\n" + PYTHON_SRC + "\n```\n" + PROSE
        assert is_verbatim_critical(text)

    def test_single_signal_is_enough(self) -> None:
        # OR-composition: one shebang line in otherwise plain text trips it.
        assert is_verbatim_critical("#!/usr/bin/env python3\n" + PROSE)

    def test_is_deterministic(self) -> None:
        for text in (PYTHON_SRC, PROSE, LOG_OUTPUT):
            assert classify_verbatim(text) == classify_verbatim(text)


# --------------------------------------------------------------------------
# Wiring: the guard must actually divert inside the router's dispatch
# --------------------------------------------------------------------------


class TestRouterWiring:
    """`_try_ml_compressor` is the single ML boundary. Every kompress entry
    point funnels through it — KOMPRESS-direct, TEXT, and the CODE_AWARE and
    SMART_CRUSHER fallbacks — so it is the only place a guard covers them all.
    Classifying correctly is worthless if the router still reaches the model.
    """

    def _router(self):
        from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

        return ContentRouter(ContentRouterConfig())

    def _stub_model(self, router, monkeypatch) -> list[str]:
        """Replace the ML compressor with a spy that mangles whatever it sees.

        The real model may not be downloaded in CI, in which case
        ``_try_ml_compressor`` passes through and every assertion below would
        hold vacuously. The spy makes reaching the model unmistakable.
        """

        seen: list[str] = []

        class _Spy:
            def is_ready(self) -> bool:
                return True

            def compress(self, text: str, **_kwargs):
                seen.append(text)
                from types import SimpleNamespace

                return SimpleNamespace(compressed="MANGLED", compressed_tokens=1)

        monkeypatch.setattr(router, "_get_kompress", lambda: _Spy())
        return seen

    def test_source_code_never_reaches_the_model(self, monkeypatch) -> None:
        router = self._router()
        seen = self._stub_model(router, monkeypatch)

        out, _tokens = router._try_ml_compressor(PYTHON_SRC, context="")

        assert seen == []
        assert out == PYTHON_SRC
        # The `if` that kompress dropped live must still be there.
        assert "if config.exclude_tools:" in out

    def test_instruction_text_never_reaches_the_model(self, monkeypatch) -> None:
        router = self._router()
        seen = self._stub_model(router, monkeypatch)

        out, _tokens = router._try_ml_compressor(DOCTRINE_MD, context="")

        assert seen == []
        assert out == DOCTRINE_MD
        assert "Never npm/npx" in out

    def test_ordinary_payloads_still_reach_the_model(self, monkeypatch) -> None:
        # The guard must not become a blanket "never compress" switch; that
        # would destroy the ratio the proxy exists to deliver.
        router = self._router()
        seen = self._stub_model(router, monkeypatch)

        out, _tokens = router._try_ml_compressor(LOG_OUTPUT, context="")

        assert seen, "log output must still be handed to the compressor"
        assert out == "MANGLED"

    def test_json_records_still_reach_the_model(self, monkeypatch) -> None:
        router = self._router()
        seen = self._stub_model(router, monkeypatch)

        router._try_ml_compressor(JSON_RECORDS, context="")

        assert seen, "record-set JSON must stay compressible"
