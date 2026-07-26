from __future__ import annotations

import pytest

import headroom.proxy.ssrf as ssrf_module
from headroom.providers.proxy_targets import (
    api_target,
    select_passthrough_base_url,
    vertex_target_for_location,
)
from headroom.providers.registry import DEFAULT_VERTEX_API_URL


@pytest.fixture(autouse=True)
def _resolve_test_hosts_as_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the .example hosts in this module resolve to a public address.

    The SSRF guard (headroom/proxy/ssrf.py) fails closed on a hostname that
    does not resolve, which is correct in production but would block every
    fake host used here. Stubbing resolution keeps these tests about target
    selection rather than about DNS.
    """
    import socket as _socket

    monkeypatch.setattr(
        ssrf_module.socket,
        "getaddrinfo",
        lambda *a, **k: [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )


def _proxy(**legacy_targets: str):
    class Runtime:
        @staticmethod
        def api_target(provider: str) -> str:
            return f"https://runtime.{provider}.test"

        @staticmethod
        def model_metadata_provider(headers) -> str:  # type: ignore[no-untyped-def]
            return "anthropic" if headers.get("x-api-key") else "openai"

    return type("Proxy", (), {**legacy_targets, "provider_runtime": Runtime()})()


def test_api_target_prefers_legacy_proxy_attrs() -> None:
    proxy = _proxy(ANTHROPIC_API_URL="https://legacy.anthropic.test")

    assert api_target(proxy, "anthropic") == "https://legacy.anthropic.test"
    assert api_target(proxy, "openai") == "https://runtime.openai.test"


def test_vertex_target_for_location_derives_region_when_default_configured() -> None:
    proxy = _proxy(VERTEX_API_URL=DEFAULT_VERTEX_API_URL)

    assert vertex_target_for_location(proxy, "europe-west1") == (
        "https://europe-west1-aiplatform.googleapis.com"
    )
    assert vertex_target_for_location(proxy, "global") == "https://aiplatform.googleapis.com"


def test_vertex_target_for_location_honors_explicit_gateway() -> None:
    proxy = _proxy(VERTEX_API_URL="https://vertex-gateway.example")

    assert vertex_target_for_location(proxy, "europe-west1") == "https://vertex-gateway.example"


def test_select_passthrough_base_url_handles_special_auth_modes() -> None:
    proxy = _proxy(
        ANTHROPIC_API_URL="https://legacy.anthropic.test",
        OPENAI_API_URL="https://legacy.openai.test",
        GEMINI_API_URL="https://legacy.gemini.test",
    )

    assert select_passthrough_base_url(proxy, {"chatgpt-account-id": "acct"}) == (
        "https://chatgpt.com"
    )
    assert select_passthrough_base_url(proxy, {"x-goog-api-key": "test"}) == (
        "https://legacy.gemini.test"
    )
    assert (
        select_passthrough_base_url(
            proxy,
            {"api-key": "azure", "x-headroom-base-url": "https://azure.example/base/"},
        )
        == "https://azure.example/base"
    )
    assert select_passthrough_base_url(proxy, {"x-api-key": "anthropic"}) == (
        "https://legacy.anthropic.test"
    )
    assert select_passthrough_base_url(proxy, {}) == "https://legacy.openai.test"
