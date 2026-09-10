"""Tests for proxy token enforcement on loopback connections.

When HEADROOM_PROXY_TOKEN is configured:
- Loopback callers must present the token by default across both HTTP and WebSocket.
- HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK can be set to "1", "true", or "yes" (case-insensitive,
  whitespace-trimmed) to opt out and restore loopback exemption.
- Health probes in _AUTH_EXEMPT_PATHS (/livez, /readyz, /health, /healthz) remain exempt.
- A startup warning is logged when loopback exemption opt-out is enabled.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from headroom.cache.compression_store import reset_compression_store
from headroom.proxy.server import (
    ProxyConfig,
    WebSocketAuthMiddleware,
    _loopback_token_exemption_enabled,
    create_app,
)

LOOPBACK = ("127.0.0.1", 12345)
NONLOOPBACK = ("203.0.113.5", 44444)


@pytest.mark.parametrize(
    "env_val,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("YES", True),
        (" 1 ", True),
        (" yes ", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("", False),
        ("  ", False),
        (None, False),
    ],
)
def test_loopback_token_exemption_helper(
    monkeypatch: pytest.MonkeyPatch, env_val: str | None, expected: bool
) -> None:
    if env_val is None:
        monkeypatch.delenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", raising=False)
    else:
        monkeypatch.setenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", env_val)
    assert _loopback_token_exemption_enabled() is expected


def _make_app(*, proxy_token: str | None = "s3cr3t-token") -> Any:
    reset_compression_store()
    config = ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        proxy_token=proxy_token,
    )
    return create_app(config)


class _SpyApp:
    """Downstream ASGI app that records whether it was ever reached."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True


def _ws_scope(*, client=LOOPBACK, headers=(), path="/v1/responses"):
    return {
        "type": "websocket",
        "path": path,
        "client": client,
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
    }


async def _drive(middleware, scope):
    """Run one connection through the middleware, returning (sent, downstream)."""
    inbox = [{"type": "websocket.connect"}]
    sent: list[dict] = []

    async def receive():
        return inbox.pop(0) if inbox else {"type": "websocket.disconnect"}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


def _closed_with_policy_violation(sent: list[dict]) -> bool:
    return any(m.get("type") == "websocket.close" and m.get("code") == 1008 for m in sent)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP tests
# ─────────────────────────────────────────────────────────────────────────────


def test_http_loopback_without_token_gets_401_on_v1_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", raising=False)
    app = _make_app(proxy_token="s3cr3t-token")
    with TestClient(app, base_url="http://127.0.0.1", client=LOOPBACK) as c:
        resp = c.post("/v1/messages", json={})
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}


def test_http_loopback_with_correct_token_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", raising=False)
    app = _make_app(proxy_token="s3cr3t-token")
    with TestClient(app, base_url="http://127.0.0.1", client=LOOPBACK) as c:
        # Custom header
        resp = c.get("/stats", headers={"X-Headroom-Proxy-Token": "s3cr3t-token"})
        assert resp.status_code != 401

        # Bearer header
        resp2 = c.get("/stats", headers={"Authorization": "Bearer s3cr3t-token"})
        assert resp2.status_code != 401


@pytest.mark.parametrize("truthy_val", ["1", "true", "True", "TRUE", "yes", "YES", " 1 ", " true "])
def test_http_loopback_passes_without_token_when_exempt_truthy(
    monkeypatch: pytest.MonkeyPatch, truthy_val: str
) -> None:
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", truthy_val)
    app = _make_app(proxy_token="s3cr3t-token")
    with TestClient(app, base_url="http://127.0.0.1", client=LOOPBACK) as c:
        resp = c.get("/stats")
        assert resp.status_code != 401


@pytest.mark.parametrize("falsy_val", ["0", "false", "no", "", "   ", "random"])
def test_http_loopback_rejected_without_token_when_exempt_falsy(
    monkeypatch: pytest.MonkeyPatch, falsy_val: str
) -> None:
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", falsy_val)
    app = _make_app(proxy_token="s3cr3t-token")
    with TestClient(app, base_url="http://127.0.0.1", client=LOOPBACK) as c:
        resp = c.get("/stats")
        assert resp.status_code == 401


@pytest.mark.parametrize("health_path", ["/health", "/healthz", "/livez", "/readyz"])
def test_health_paths_remain_exempt_without_token(
    monkeypatch: pytest.MonkeyPatch, health_path: str
) -> None:
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", raising=False)
    app = _make_app(proxy_token="s3cr3t-token")
    with TestClient(app, base_url="http://127.0.0.1", client=LOOPBACK) as c:
        resp = c.get(health_path)
        assert resp.status_code in {200, 503}  # Not 401


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_loopback_without_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", raising=False)
    downstream = _SpyApp()
    mw = WebSocketAuthMiddleware(downstream, proxy_token="s3cr3t-token")

    sent = await _drive(mw, _ws_scope(client=LOOPBACK))

    assert downstream.called is False
    assert _closed_with_policy_violation(sent)


@pytest.mark.asyncio
async def test_ws_loopback_with_token_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", raising=False)
    downstream = _SpyApp()
    mw = WebSocketAuthMiddleware(downstream, proxy_token="s3cr3t-token")

    sent = await _drive(
        mw,
        _ws_scope(client=LOOPBACK, headers=[("x-headroom-proxy-token", "s3cr3t-token")]),
    )

    assert downstream.called is True
    assert not _closed_with_policy_violation(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("truthy_val", ["1", "true", "True", "yes", "YES", " 1 ", " yes "])
async def test_ws_loopback_passes_without_token_when_exempt_truthy(
    monkeypatch: pytest.MonkeyPatch, truthy_val: str
) -> None:
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", truthy_val)
    downstream = _SpyApp()
    mw = WebSocketAuthMiddleware(downstream, proxy_token="s3cr3t-token")

    sent = await _drive(mw, _ws_scope(client=LOOPBACK))

    assert downstream.called is True
    assert not _closed_with_policy_violation(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("falsy_val", ["0", "false", "no", "", "   ", "invalid"])
async def test_ws_loopback_rejected_without_token_when_exempt_falsy(
    monkeypatch: pytest.MonkeyPatch, falsy_val: str
) -> None:
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", falsy_val)
    downstream = _SpyApp()
    mw = WebSocketAuthMiddleware(downstream, proxy_token="s3cr3t-token")

    sent = await _drive(mw, _ws_scope(client=LOOPBACK))

    assert downstream.called is False
    assert _closed_with_policy_violation(sent)


# ─────────────────────────────────────────────────────────────────────────────
# Startup warning logging test
# ─────────────────────────────────────────────────────────────────────────────


def test_startup_warning_logged_when_exempt_loopback_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", "1")
    target = logging.getLogger("headroom.proxy")
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="headroom.proxy"):
            _make_app(proxy_token="s3cr3t-token")
    finally:
        target.removeHandler(caplog.handler)

    warnings = [
        r.getMessage()
        for r in caplog.records
        if "HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK" in r.getMessage()
    ]
    assert len(warnings) >= 1
    assert any("loopback callers bypass token auth" in w for w in warnings)


def test_startup_warning_not_logged_when_exempt_loopback_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK", raising=False)
    target = logging.getLogger("headroom.proxy")
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="headroom.proxy"):
            _make_app(proxy_token="s3cr3t-token")
    finally:
        target.removeHandler(caplog.handler)

    warnings = [
        r.getMessage()
        for r in caplog.records
        if "HEADROOM_PROXY_TOKEN_EXEMPT_LOOPBACK" in r.getMessage()
    ]
    assert len(warnings) == 0
