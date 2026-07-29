"""Transcript-derived CCR retrieval-tool reference integrity.

Regression cover for the 400 that marker-driven and session-sticky
injection both let through::

    400 Tool reference 'headroom_retrieve' not found in available tools

The load-bearing case is the *continued* turn: history carries a
``headroom_retrieve`` call, this turn compresses nothing, and the
process-local sticky tracker has been lost (restart or LRU eviction). Both
existing paths then decline to inject and the request is rejected — and
stays rejected, because the offending call never leaves the history.
"""

from __future__ import annotations

import pytest

from headroom.ccr.tool_injection import CCR_TOOL_NAME
from headroom.ccr.tool_reference import (
    declared_tool_names,
    ensure_ccr_tool_references,
    is_ccr_tool_name,
    missing_ccr_tool_names,
    referenced_ccr_tool_names,
)

MCP_NAME = "mcp__headroom__headroom_retrieve"


def _anthropic_call(name: str = CCR_TOOL_NAME) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Fetching the original."},
            {"type": "tool_use", "id": "toolu_1", "name": name, "input": {"hash": "a" * 24}},
        ],
    }


class TestIsCcrToolName:
    @pytest.mark.parametrize(
        "name",
        [CCR_TOOL_NAME, MCP_NAME, "vendor__headroom_retrieve"],
    )
    def test_accepts_bare_and_namespaced(self, name: str) -> None:
        assert is_ccr_tool_name(name)

    @pytest.mark.parametrize(
        "name",
        ["Read", "headroom_retrieve_v2", "retrieve", "", None, 42, "headroom_retrievex"],
    )
    def test_rejects_everything_else(self, name: object) -> None:
        assert not is_ccr_tool_name(name)


class TestDeclaredToolNames:
    def test_reads_anthropic_flat_shape(self) -> None:
        assert declared_tool_names([{"name": "Read"}]) == {"Read"}

    def test_reads_openai_nested_shape(self) -> None:
        tools = [{"type": "function", "function": {"name": CCR_TOOL_NAME}}]
        assert declared_tool_names(tools) == {CCR_TOOL_NAME}

    def test_tolerates_none_and_junk(self) -> None:
        assert declared_tool_names(None) == set()
        assert declared_tool_names([None, 7, {}, {"function": None}]) == set()  # type: ignore[list-item]


class TestReferencedCcrToolNames:
    def test_finds_anthropic_tool_use(self) -> None:
        assert referenced_ccr_tool_names([_anthropic_call()]) == [CCR_TOOL_NAME]

    def test_finds_mcp_namespaced_call(self) -> None:
        assert referenced_ccr_tool_names([_anthropic_call(MCP_NAME)]) == [MCP_NAME]

    def test_finds_openai_tool_calls(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "function": {"name": CCR_TOOL_NAME, "arguments": "{}"}}
                ],
            }
        ]
        assert referenced_ccr_tool_names(messages) == [CCR_TOOL_NAME]

    def test_finds_openai_responses_function_call_item(self) -> None:
        items = [{"type": "function_call", "name": CCR_TOOL_NAME, "arguments": "{}"}]
        assert referenced_ccr_tool_names(items) == [CCR_TOOL_NAME]

    def test_finds_google_function_call_part(self) -> None:
        messages = [{"parts": [{"functionCall": {"name": CCR_TOOL_NAME, "args": {}}}]}]
        assert referenced_ccr_tool_names(messages) == [CCR_TOOL_NAME]

    def test_ignores_unrelated_tools(self) -> None:
        messages = [_anthropic_call("Read"), {"role": "user", "content": "hi"}]
        assert referenced_ccr_tool_names(messages) == []

    def test_tool_result_alone_is_not_a_reference(self) -> None:
        # A tool_result names its call by id, not by tool name, so it cannot
        # independently create the reference the 400 complains about.
        messages = [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "orig"}],
            }
        ]
        assert referenced_ccr_tool_names(messages) == []

    def test_deduplicates_preserving_first_seen_order(self) -> None:
        messages = [
            _anthropic_call(MCP_NAME),
            _anthropic_call(CCR_TOOL_NAME),
            _anthropic_call(MCP_NAME),
        ]
        assert referenced_ccr_tool_names(messages) == [MCP_NAME, CCR_TOOL_NAME]

    def test_tolerates_none_and_junk(self) -> None:
        assert referenced_ccr_tool_names(None) == []
        assert referenced_ccr_tool_names([None, 7, {"content": "str"}]) == []  # type: ignore[list-item]


class TestMissingCcrToolNames:
    def test_declared_tool_is_not_missing(self) -> None:
        tools = [{"name": CCR_TOOL_NAME, "input_schema": {}}]
        assert missing_ccr_tool_names([_anthropic_call()], tools) == []

    def test_undeclared_tool_is_missing(self) -> None:
        assert missing_ccr_tool_names([_anthropic_call()], [{"name": "Read"}]) == [CCR_TOOL_NAME]

    def test_bare_declaration_does_not_cover_namespaced_call(self) -> None:
        # The 400 matches on the exact string. A bare declaration does not
        # satisfy a namespaced call.
        tools = [{"name": CCR_TOOL_NAME, "input_schema": {}}]
        assert missing_ccr_tool_names([_anthropic_call(MCP_NAME)], tools) == [MCP_NAME]


class TestEnsureCcrToolReferences:
    def test_noop_returns_input_object_untouched(self) -> None:
        # Identity matters: callers use it to leave request bytes, and the
        # prompt cache, alone.
        tools = [{"name": "Read"}]
        out, repaired = ensure_ccr_tool_references([{"role": "user", "content": "hi"}], tools)
        assert out is tools
        assert repaired == []

    def test_repairs_missing_anthropic_declaration(self) -> None:
        out, repaired = ensure_ccr_tool_references([_anthropic_call()], [{"name": "Read"}])
        assert repaired == [CCR_TOOL_NAME]
        assert declared_tool_names(out) == {"Read", CCR_TOOL_NAME}
        injected = [t for t in out or [] if t.get("name") == CCR_TOOL_NAME][0]
        assert "input_schema" in injected

    def test_repairs_under_the_referenced_namespaced_name(self) -> None:
        out, repaired = ensure_ccr_tool_references([_anthropic_call(MCP_NAME)], [])
        assert repaired == [MCP_NAME]
        assert declared_tool_names(out) == {MCP_NAME}

    def test_repairs_openai_nested_shape(self) -> None:
        messages = [{"role": "assistant", "tool_calls": [{"function": {"name": CCR_TOOL_NAME}}]}]
        out, repaired = ensure_ccr_tool_references(messages, None, provider="openai")
        assert repaired == [CCR_TOOL_NAME]
        assert (out or [])[0]["function"]["name"] == CCR_TOOL_NAME

    def test_does_not_mutate_caller_tool_list(self) -> None:
        tools = [{"name": "Read"}]
        ensure_ccr_tool_references([_anthropic_call()], tools)
        assert tools == [{"name": "Read"}]

    def test_is_idempotent(self) -> None:
        messages = [_anthropic_call()]
        once, _ = ensure_ccr_tool_references(messages, [])
        twice, repaired = ensure_ccr_tool_references(messages, once)
        assert twice is once
        assert repaired == []

    def test_self_heals_a_poisoned_continued_turn(self) -> None:
        # The real shape of the bug: history holds the call, this turn has no
        # compression markers, and no session state survives. Nothing else in
        # the request would trigger injection.
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "look at the log"}]},
            _anthropic_call(),
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "original"}
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "now what?"}]},
        ]
        out, repaired = ensure_ccr_tool_references(messages, [{"name": "Read"}])
        assert repaired == [CCR_TOOL_NAME]
        assert CCR_TOOL_NAME in declared_tool_names(out)
