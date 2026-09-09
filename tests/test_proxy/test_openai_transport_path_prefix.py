"""Tests for OpenAI transport path-prefix reconstruction from upstream hints."""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from headroom.proxy.server import ProxyConfig, create_app

_OPENAI_CHAT_PATH = "/v1/chat/completions"
_OPENAI_RESPONSES_PATH = "/v1/responses"


def _build_openai_client():
    config = ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        ccr_inject_tool=False,
        ccr_handle_responses=False,
        ccr_context_tracking=False,
        image_optimize=False,
    )

    app = create_app(config)
    proxy = app.state.proxy
    captured: dict[str, object] = {}

    async def _fake_retry(
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict,
        **_kwargs: object,
    ) -> httpx.Response:
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "object": "response",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "total_tokens": 12,
                },
            },
        )

    proxy._retry_request = _fake_retry

    async def _record_request_outcome(outcome: object) -> None:
        captured["outcome"] = outcome

    proxy._record_request_outcome = _record_request_outcome

    return TestClient(app), captured


def _assert_internal_header_absent(captured: dict[str, object], name: str) -> None:
    assert isinstance(captured.get("headers"), dict)
    headers = {k.lower() for k in captured["headers"].keys()}  # type: ignore[union-attr]
    assert name.lower() not in headers


def _assert_path(captured: dict[str, object], path: str) -> None:
    url = captured.get("url")
    assert isinstance(url, str)
    assert url.endswith(path)


def _assert_origin(captured: dict[str, object], origin: str) -> None:
    url = captured.get("url")
    assert isinstance(url, str)
    assert url.startswith(origin)


def _reconstruction_case(endpoint: str, body: dict, original_path: str) -> None:
    """Shared body for the two path-reconstruction cases.

    Upstream wrote this with ``base_fail = "://bad-base"`` expecting a 200: a
    value it cannot parse is ignored, and the request falls back to the
    configured default upstream. This fork rejects it with a 400 instead.

    That is deliberate, and it is a security property rather than a style
    difference. Silently falling back sends the caller's Authorization header
    and prompt to a provider they did not name, and returns that provider's
    answer as though the override had been honoured. The caller has no way to
    tell. See tests/test_proxy/test_ssrf_consolidated.py, which fixes the
    guard's four measured divergences from upstream, and CVE-2026-77775.

    The valid-host arm is upstream's and is kept verbatim in substance: it is
    the real regression coverage for path reconstruction, and it still passes.
    """
    base_fail = "://bad-base"

    # Rejected arm: 400, an accurate reason, and nothing sent upstream.
    client, captured = _build_openai_client()
    response = client.post(
        endpoint,
        headers={
            "Authorization": "Bearer sk-test",
            "x-headroom-base-url": base_fail,
            "x-headroom-original-path": original_path,
        },
        json=body,
    )
    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "scheme is not http or https" in message
    # The old hardcoded text claimed every rejection resolved to a reserved
    # address and printed a None hostname.
    assert "None" not in message
    assert captured == {}, "a rejected base URL must not reach any upstream"

    # Accepted arm: upstream's own assertions, unchanged.
    client, captured = _build_openai_client()
    response = client.post(
        endpoint,
        headers={
            "Authorization": "Bearer sk-test",
            "x-headroom-base-url": "https://api.deepseek.com",
            "x-headroom-original-path": original_path,
        },
        json=body,
    )
    assert response.status_code == 200, response.text
    assert captured["method"] == "POST"
    _assert_origin(captured, "https://api.deepseek.com")
    _assert_path(captured, original_path)


def test_chat_upstream_reconstruction_rejects_unparseable_base() -> None:
    _reconstruction_case(
        _OPENAI_CHAT_PATH,
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        "/chat/completions",
    )


def test_responses_upstream_reconstruction_rejects_unparseable_base() -> None:
    _reconstruction_case(
        _OPENAI_RESPONSES_PATH,
        {"model": "gpt-4o", "input": "hi"},
        "/responses",
    )
