"""Transcript-derived CCR retrieval-tool reference integrity.

Once a conversation has called the CCR retrieval tool, that call stays in
its history forever. Every provider rejects a request whose transcript
references a tool name that is absent from the request's tool list::

    400 Tool reference 'headroom_retrieve' not found in available tools

Neither of the two existing injection paths can guarantee the invariant:

* Marker-driven injection (``CCRToolInjector.has_compressed_content``)
  describes *this* turn. A continued turn that compresses nothing has no
  markers, so it injects nothing — while the history still carries the
  call.
* Session-sticky injection (:class:`SessionCcrTracker`) lives in a
  bounded, process-local LRU. It does not survive a proxy restart and it
  evicts under load. The transcript outlives both.

So presence of the tool definition is derived from the transcript, which
is the only source as durable as the constraint it has to satisfy. This
is a stateless, self-healing guard: it re-derives the correct answer on
every request, including the first request after a restart.

The guard is deliberately the *last* word on the tools list. Cache
stability is what the deferral logic in ``ccr_marker_policy`` optimises
for, and that trade is right up until the point where the alternative is
a hard 400 — a rejected request costs the whole turn, not a cache
discount. Where the two conflict, correctness wins.

Prefixed names (``mcp__headroom__headroom_retrieve``) are handled too.
A client that registered the tool over MCP puts the prefixed name in the
transcript; if that client later defers the schema, the name goes missing
from ``tools`` and the same 400 fires. We re-declare whatever CCR-family
name the transcript actually references, so the definition matches the
call rather than merely resembling it.
"""

from __future__ import annotations

from typing import Any, Literal

from headroom.ccr.tool_injection import CCR_TOOL_NAME, create_ccr_tool_definition

Provider = Literal["anthropic", "openai", "openai_responses", "google"]

#: Suffix used by MCP-namespaced registrations of the retrieval tool,
#: e.g. ``mcp__headroom__headroom_retrieve``. Mirrors the Rust check in
#: ``crates/headroom-core/src/transforms/live_zone.rs``.
CCR_TOOL_NAME_SUFFIX = f"__{CCR_TOOL_NAME}"


def is_ccr_tool_name(name: object) -> bool:
    """Return True when ``name`` is the CCR retrieval tool, bare or namespaced."""

    if not isinstance(name, str) or not name:
        return False
    return name == CCR_TOOL_NAME or name.endswith(CCR_TOOL_NAME_SUFFIX)


def declared_tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    """Collect every tool name declared in a request's tool list.

    Handles both the Anthropic/Google flat shape (``{"name": ...}``) and
    the OpenAI nested shape (``{"function": {"name": ...}}``).
    """

    names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _scan_content_blocks(content: Any, found: list[str]) -> None:
    """Collect CCR tool names from Anthropic-shaped content blocks."""

    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        # Anthropic assistant turn: {"type": "tool_use", "name": ...}
        if block.get("type") in ("tool_use", "server_tool_use"):
            _record(block.get("name"), found)


def _scan_openai_tool_calls(message: dict[str, Any], found: list[str]) -> None:
    """Collect CCR tool names from OpenAI chat-completions ``tool_calls``."""

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        name = function.get("name") if isinstance(function, dict) else call.get("name")
        _record(name, found)


def _scan_google_parts(message: dict[str, Any], found: list[str]) -> None:
    """Collect CCR tool names from Google/Gemini ``parts[].functionCall``."""

    parts = message.get("parts")
    if not isinstance(parts, list):
        return
    for part in parts:
        if not isinstance(part, dict):
            continue
        call = part.get("functionCall")
        if isinstance(call, dict):
            _record(call.get("name"), found)


def _record(name: object, found: list[str]) -> None:
    if is_ccr_tool_name(name) and name not in found:
        found.append(name)  # type: ignore[arg-type]


def referenced_ccr_tool_names(messages: list[dict[str, Any]] | None) -> list[str]:
    """Return every CCR retrieval-tool name the transcript calls, in first-seen order.

    Provider-neutral by construction: a request only carries one of these
    shapes, and scanning for all of them costs a few dict lookups per
    message while removing a per-provider branch that could drift.

    Only *calls* count. A ``tool_result`` refers to its call by id, not by
    name, so it cannot independently create the reference that the 400
    complains about.
    """

    found: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        _scan_content_blocks(message.get("content"), found)
        _scan_openai_tool_calls(message, found)
        _scan_google_parts(message, found)
        # OpenAI Responses items are flat: {"type": "function_call", "name": ...}
        if message.get("type") == "function_call":
            _record(message.get("name"), found)
    return found


def missing_ccr_tool_names(
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
) -> list[str]:
    """CCR tool names the transcript calls but the tool list does not declare."""

    declared = declared_tool_names(tools)
    return [name for name in referenced_ccr_tool_names(messages) if name not in declared]


def ensure_ccr_tool_references(
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    *,
    provider: Provider = "anthropic",
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Guarantee every CCR tool call in ``messages`` has a declaration in ``tools``.

    Returns ``(tools_out, repaired_names)``. ``tools_out`` is the input
    list unchanged (same object) when nothing was missing, so a caller can
    cheaply detect the no-op case and leave the request bytes — and the
    prompt cache — untouched. ``repaired_names`` is empty in that case.

    ``provider`` selects the tool-definition shape only. The definition is
    re-declared under the referenced name, which may be MCP-namespaced.
    """

    missing = missing_ccr_tool_names(messages, tools)
    if not missing:
        return tools, []

    tools_out = list(tools or [])
    for name in missing:
        definition = create_ccr_tool_definition(
            "openai" if provider == "openai_responses" else provider
        )
        tools_out.append(_rename_tool_definition(definition, name))
    return tools_out, missing


def _rename_tool_definition(definition: dict[str, Any], name: str) -> dict[str, Any]:
    """Return ``definition`` re-declared under ``name`` without mutating the input."""

    if name == CCR_TOOL_NAME:
        return definition
    function = definition.get("function")
    if isinstance(function, dict):
        return {**definition, "function": {**function, "name": name}}
    return {**definition, "name": name}
