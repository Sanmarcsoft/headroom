from __future__ import annotations

import pytest

from headroom.proxy.internal_header_policy import (
    STRIP_INTERNAL_HEADERS_ENV,
    resolve_strip_internal_headers_mode,
    strip_internal_headers,
)


def test_resolve_strip_internal_headers_mode_defaults_to_enabled() -> None:
    assert resolve_strip_internal_headers_mode(None) == "enabled"
    assert resolve_strip_internal_headers_mode("  ") == "enabled"


def test_resolve_strip_internal_headers_mode_accepts_known_values() -> None:
    assert resolve_strip_internal_headers_mode("ENABLED") == "enabled"
    assert resolve_strip_internal_headers_mode(" disabled ") == "disabled"


def test_resolve_strip_internal_headers_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match=STRIP_INTERNAL_HEADERS_ENV):
        resolve_strip_internal_headers_mode("maybe")


def test_strip_internal_headers_removes_headroom_headers_case_insensitively() -> None:
    headers = {
        "Authorization": "Bearer token",
        "x-headroom-bypass": "true",
        "X-Headroom-User-Id": "user-1",
        "content-type": "application/json",
    }

    stripped = strip_internal_headers(headers, mode="enabled")

    assert stripped == {
        "Authorization": "Bearer token",
        "content-type": "application/json",
    }
    assert "x-headroom-bypass" in headers


def test_strip_internal_headers_disabled_returns_copy_unchanged() -> None:
    headers = {"x-headroom-mode": "passthrough", "content-type": "application/json"}

    copied = strip_internal_headers(headers, mode="disabled")

    assert copied == headers
    assert copied is not headers


def test_strip_internal_headers_disabled_removes_proxy_token_case_insensitively() -> None:
    # Lowercase
    headers_lower = {
        "x-headroom-proxy-token": "secret-1",
        "x-headroom-mode": "passthrough",
        "content-type": "application/json",
    }
    stripped_lower = strip_internal_headers(headers_lower, mode="disabled")
    assert stripped_lower == {
        "x-headroom-mode": "passthrough",
        "content-type": "application/json",
    }

    # Mixed case
    headers_mixed = {
        "X-Headroom-Proxy-Token": "secret-2",
        "X-HEADROOM-PROXY-TOKEN": "secret-3",
        "x-Headroom-Proxy-Token": "secret-4",
        "x-headroom-bypass": "true",
        "authorization": "Bearer token",
    }
    stripped_mixed = strip_internal_headers(headers_mixed, mode="disabled")
    assert stripped_mixed == {
        "x-headroom-bypass": "true",
        "authorization": "Bearer token",
    }


def test_strip_internal_headers_disabled_preserves_order_and_other_headers() -> None:
    headers = {
        "header-1": "val1",
        "x-headroom-bypass": "true",
        "x-headroom-proxy-token": "secret",
        "x-headroom-user-id": "u1",
        "header-2": "val2",
    }
    stripped = strip_internal_headers(headers, mode="disabled")
    assert list(stripped.keys()) == ["header-1", "x-headroom-bypass", "x-headroom-user-id", "header-2"]
    assert stripped == {
        "header-1": "val1",
        "x-headroom-bypass": "true",
        "x-headroom-user-id": "u1",
        "header-2": "val2",
    }


def test_strip_internal_headers_enabled_removes_proxy_token_and_all_internal_headers() -> None:
    headers = {
        "x-headroom-proxy-token": "secret",
        "X-Headroom-Proxy-Token": "secret",
        "x-headroom-bypass": "true",
        "authorization": "Bearer token",
        "content-type": "application/json",
    }
    stripped = strip_internal_headers(headers, mode="enabled")
    assert stripped == {
        "authorization": "Bearer token",
        "content-type": "application/json",
    }


def test_drop_proxy_token_authorization() -> None:
    from headroom.proxy.internal_header_policy import drop_proxy_token_authorization

    # Drops matching proxy token in Bearer authorization
    headers = {
        "Authorization": "Bearer my-proxy-token",
        "x-api-key": "sk-ant-123",
    }
    result = drop_proxy_token_authorization(headers, "my-proxy-token")
    assert result == {"x-api-key": "sk-ant-123"}

    # Case-insensitive header name and 'bearer ' prefix
    headers_lower = {
        "authorization": "bearer my-proxy-token",
        "x-api-key": "sk-ant-123",
    }
    result_lower = drop_proxy_token_authorization(headers_lower, "my-proxy-token")
    assert result_lower == {"x-api-key": "sk-ant-123"}

    # All-uppercase case variants
    headers_upper = {
        "AUTHORIZATION": "BEARER my-proxy-token",
        "x-api-key": "sk-ant-123",
    }
    result_upper = drop_proxy_token_authorization(headers_upper, "my-proxy-token")
    assert result_upper == {"x-api-key": "sk-ant-123"}

    # Genuine OAuth token untouched
    genuine = {
        "Authorization": "Bearer sk-ant-oat01-genuine-token",
        "x-api-key": "sk-ant-123",
    }
    result_genuine = drop_proxy_token_authorization(genuine, "my-proxy-token")
    assert result_genuine == genuine

    # Non-Bearer authorization scheme untouched
    basic_auth = {
        "Authorization": "Basic dXNlcjpwYXNz",
        "x-api-key": "sk-ant-123",
    }
    assert drop_proxy_token_authorization(basic_auth, "my-proxy-token") == basic_auth

    # Header absent leaves headers untouched
    no_auth = {"x-api-key": "sk-ant-123"}
    assert drop_proxy_token_authorization(no_auth, "my-proxy-token") == no_auth

    # None, empty, or whitespace-only proxy token leaves headers untouched
    assert drop_proxy_token_authorization(headers, None) == headers
    assert drop_proxy_token_authorization(headers, "") == headers
    assert drop_proxy_token_authorization(headers, "   ") == headers

    # Preserves container order
    ordered = {
        "h1": "v1",
        "Authorization": "Bearer my-proxy-token",
        "h2": "v2",
        "h3": "v3",
    }
    assert list(drop_proxy_token_authorization(ordered, "my-proxy-token").keys()) == ["h1", "h2", "h3"]


