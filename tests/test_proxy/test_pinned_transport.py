"""Connect-time IP pinning closes the DNS-rebinding gap in the SSRF guard.

The guard resolves at header-parse time; httpx resolved again at connect time.
A low-TTL record answering public then private walked through. These tests pin
the behaviour that closes it: the address that was validated is the address
that gets connected to.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from headroom.proxy.pinned_transport import (
    PinnedUpstreamTransport,
    resolve_and_validate,
)
from headroom.proxy.ssrf import UpstreamBaseUrlBlocked

PUBLIC = "93.184.216.34"
PRIVATE = "10.0.0.5"
METADATA = "169.254.169.254"


def _addrinfo(*addresses: str) -> list[tuple]:
    return [
        (
            socket.AF_INET6 if ":" in a else socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (a, 0),
        )
        for a in addresses
    ]


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Captures the request as it would go on the wire."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(200, request=request)

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        return None


@pytest.fixture(autouse=True)
def _no_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_ALLOW_PRIVATE_UPSTREAM_BASE_URL", raising=False)


# ---------- resolve_and_validate ----------------------------------------- #


def test_public_hostname_resolves_to_its_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo(PUBLIC))
    assert resolve_and_validate("gateway.example") == PUBLIC


def test_private_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo(PRIVATE))
    with pytest.raises(UpstreamBaseUrlBlocked):
        resolve_and_validate("internal.example")


def test_any_private_answer_refuses_even_when_a_public_one_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo(PUBLIC, METADATA))
    with pytest.raises(UpstreamBaseUrlBlocked):
        resolve_and_validate("mixed.example")


def test_resolution_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> None:
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(UpstreamBaseUrlBlocked):
        resolve_and_validate("unresolvable.invalid")


def test_ip_literal_is_checked_without_dns() -> None:
    assert resolve_and_validate(PUBLIC) == PUBLIC
    with pytest.raises(UpstreamBaseUrlBlocked):
        resolve_and_validate(METADATA)


# ---------- the transport ------------------------------------------------- #


@pytest.mark.asyncio
async def test_trusted_host_is_not_rewritten() -> None:
    """The hot path stays untouched so connection reuse is preserved."""
    inner = _RecordingTransport()
    transport = PinnedUpstreamTransport(inner, {"api.anthropic.com"})
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    await transport.handle_async_request(request)

    assert inner.seen[0].url.host == "api.anthropic.com"
    assert "sni_hostname" not in inner.seen[0].extensions


@pytest.mark.asyncio
async def test_untrusted_host_is_pinned_to_the_validated_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo(PUBLIC))
    inner = _RecordingTransport()
    transport = PinnedUpstreamTransport(inner, {"api.anthropic.com"})
    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")

    await transport.handle_async_request(request)

    sent = inner.seen[0]
    assert sent.url.host == PUBLIC, "must connect to the validated literal"
    assert sent.headers["host"] == "gateway.example", "Host must survive for virtual hosting"
    assert sent.extensions["sni_hostname"] == "gateway.example", "TLS SNI must survive"
    assert sent.url.path == "/v1/chat/completions"


@pytest.mark.asyncio
async def test_rebind_between_validation_and_connect_cannot_land_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a second resolution cannot change the target.

    getaddrinfo answers public once, then private on every later call, which is
    exactly the attacker-controlled low-TTL rebind. Because the transport pins
    the address it validated, the private answer is never used.
    """
    answers = iter([_addrinfo(PUBLIC)])

    def _rebinding(*a: object, **k: object) -> list[tuple]:
        try:
            return next(answers)
        except StopIteration:
            return _addrinfo(METADATA)

    monkeypatch.setattr(socket, "getaddrinfo", _rebinding)
    inner = _RecordingTransport()
    transport = PinnedUpstreamTransport(inner, set())
    request = httpx.Request("GET", "https://rebind.attacker.test/")

    await transport.handle_async_request(request)

    assert inner.seen[0].url.host == PUBLIC
    assert METADATA not in str(inner.seen[0].url)


@pytest.mark.asyncio
async def test_private_target_is_refused_at_connect_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo(METADATA))
    inner = _RecordingTransport()
    transport = PinnedUpstreamTransport(inner, set())
    request = httpx.Request("GET", "https://sneaky.example/latest/meta-data/")

    with pytest.raises(UpstreamBaseUrlBlocked):
        await transport.handle_async_request(request)
    assert inner.seen == [], "the request must never reach the wire"


@pytest.mark.asyncio
async def test_operator_opt_in_disables_pinning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_ALLOW_PRIVATE_UPSTREAM_BASE_URL", "1")
    inner = _RecordingTransport()
    transport = PinnedUpstreamTransport(inner, set())
    request = httpx.Request("GET", "https://internal-gateway.corp/v1/models")

    await transport.handle_async_request(request)

    assert inner.seen[0].url.host == "internal-gateway.corp"


@pytest.mark.asyncio
async def test_ipv6_literal_is_bracketed(monkeypatch: pytest.MonkeyPatch) -> None:
    v6 = "2606:2800:220:1:248:1893:25c8:1946"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo(v6))
    inner = _RecordingTransport()
    transport = PinnedUpstreamTransport(inner, set())
    request = httpx.Request("GET", "https://v6.example/")

    await transport.handle_async_request(request)

    assert inner.seen[0].url.host == v6
    assert inner.seen[0].headers["host"] == "v6.example"
