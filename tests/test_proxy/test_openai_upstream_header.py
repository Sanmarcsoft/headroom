"""Tests for ``OpenAIHandlerMixin._resolve_openai_upstream``.

The dedicated OpenAI handlers (``/v1/chat/completions``,
``/v1/responses``) must honor the ``x-headroom-base-url`` request header
so OpenAI-compatible gateways (LiteLLM, CPA, self-hosted vLLM, Azure
OpenAI) route correctly — consistent with the generic passthrough route
that already honors it (see ``providers/proxy_routes.py``).

These tests pin the resolution contract:
- header present  → its value wins
- header absent   → configured ``OPENAI_API_URL`` fallback
- header empty or whitespace-only → fallback (no blanking)
- header resolves to a loopback/private/link-local/metadata address → SSRF
  guard blocks it and falls back (see ``TestSsrfGuard`` below)
"""

from __future__ import annotations

import socket

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from starlette.datastructures import Headers  # noqa: E402

from headroom.proxy.handlers import openai as openai_handler  # noqa: E402
from headroom.proxy.handlers.openai import OpenAIHandlerMixin  # noqa: E402

# A fixed, non-private address used to stub DNS resolution so the
# header-parsing tests below don't depend on real network access or on the
# test hostnames' actual DNS records (several use the reserved, deliberately
# non-resolving ``.example`` TLD -- RFC 6761).
_PUBLIC_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class _FakeRequest:
    """Minimal stand-in exposing ``headers`` like a real Starlette request.

    Uses ``starlette.datastructures.Headers`` so header lookup is
    case-insensitive, matching the production ``request.headers`` — a
    plain ``dict`` would let case-folding regressions pass silently.
    """

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = Headers(headers=headers)


def _stub_proxy(fallback_url: str) -> OpenAIHandlerMixin:
    """A bare mixin instance with only ``OPENAI_API_URL`` configured."""
    return type(  # type: ignore[return-value]
        "_S",
        (OpenAIHandlerMixin,),
        {"OPENAI_API_URL": fallback_url},
    )()


def test_header_overrides_configured_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_handler.socket, "getaddrinfo", lambda *a, **k: _PUBLIC_ADDRINFO)
    proxy = _stub_proxy("https://api.openai.test")
    # The transport sends the upstream origin (no /v1 path).
    request = _FakeRequest({"x-headroom-base-url": "https://gateway.example"})

    assert proxy._resolve_openai_upstream(request) == "https://gateway.example"


def test_missing_header_falls_back_to_configured_url() -> None:
    proxy = _stub_proxy("https://api.openai.test")
    request = _FakeRequest({})

    assert proxy._resolve_openai_upstream(request) == "https://api.openai.test"


def test_empty_header_falls_back_to_configured_url() -> None:
    """An explicitly empty or whitespace-only header must not blank the upstream."""
    proxy = _stub_proxy("https://api.openai.test")

    empty = _FakeRequest({"x-headroom-base-url": ""})
    assert proxy._resolve_openai_upstream(empty) == "https://api.openai.test"

    whitespace = _FakeRequest({"x-headroom-base-url": "   "})
    assert proxy._resolve_openai_upstream(whitespace) == "https://api.openai.test"


def test_header_lookup_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transports may send mixed-case header names; lookup must still resolve."""
    monkeypatch.setattr(openai_handler.socket, "getaddrinfo", lambda *a, **k: _PUBLIC_ADDRINFO)
    proxy = _stub_proxy("https://api.openai.test")
    # Real transports routinely send Title-Case header names.
    request = _FakeRequest({"X-Headroom-Base-Url": "https://gateway.example"})

    assert proxy._resolve_openai_upstream(request) == "https://gateway.example"


def test_header_with_subpath_preserves_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom upstream served from a sub-path (e.g. /api/v1) must keep the path,
    not be collapsed to the bare origin (#2047)."""
    monkeypatch.setattr(openai_handler.socket, "getaddrinfo", lambda *a, **k: _PUBLIC_ADDRINFO)
    proxy = _stub_proxy("https://api.openai.test")
    request = _FakeRequest({"x-headroom-base-url": "https://gateway.example/api/v1"})

    assert proxy._resolve_openai_upstream(request) == "https://gateway.example/api/v1"

    # Trailing slash is normalized away, not doubled.
    trailing = _FakeRequest({"x-headroom-base-url": "https://gateway.example/api/v1/"})
    assert proxy._resolve_openai_upstream(trailing) == "https://gateway.example/api/v1"


class TestSsrfGuard:
    """``x-headroom-base-url`` is caller-supplied: a hostname that resolves to
    loopback, RFC1918, RFC4193 (fd00::/8), link-local (incl. the cloud
    metadata address 169.254.169.254), or an IPv6 equivalent must never be
    honored as the outbound upstream -- that would let any caller pivot the
    proxy into an internal-network or cloud-metadata SSRF probe.
    """

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",  # loopback
            "10.0.0.5",  # RFC1918
            "172.16.0.5",  # RFC1918
            "192.168.1.1",  # RFC1918
            "169.254.169.254",  # link-local / cloud metadata (AWS/GCP/Azure)
            "::1",  # loopback (IPv6)
            "fd12:3456:789a::1",  # RFC4193 ULA
            "fe80::1",  # link-local (IPv6)
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
        ],
    )
    def test_private_and_metadata_addresses_are_blocked(
        self, monkeypatch: pytest.MonkeyPatch, address: str
    ) -> None:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        monkeypatch.setattr(
            openai_handler.socket,
            "getaddrinfo",
            lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (address, 0))],
        )
        proxy = _stub_proxy("https://api.openai.test")
        request = _FakeRequest({"x-headroom-base-url": "https://internal.attacker.test"})

        assert proxy._resolve_openai_upstream(request) == "https://api.openai.test"

    def test_ip_literal_loopback_header_is_blocked_without_dns(self) -> None:
        """A raw loopback IP literal in the header needs no DNS lookup to catch."""
        proxy = _stub_proxy("https://api.openai.test")
        request = _FakeRequest({"x-headroom-base-url": "http://127.0.0.1:9999"})

        assert proxy._resolve_openai_upstream(request) == "https://api.openai.test"

    def test_ip_literal_metadata_header_is_blocked_without_dns(self) -> None:
        proxy = _stub_proxy("https://api.openai.test")
        request = _FakeRequest({"x-headroom-base-url": "http://169.254.169.254"})

        assert proxy._resolve_openai_upstream(request) == "https://api.openai.test"

    def test_any_resolved_record_being_private_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One public + one private DNS answer must still block -- not just a
        first-record-only check."""
        monkeypatch.setattr(
            openai_handler.socket,
            "getaddrinfo",
            lambda *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 0)),
            ],
        )
        proxy = _stub_proxy("https://api.openai.test")
        request = _FakeRequest({"x-headroom-base-url": "https://mixed.example"})

        assert proxy._resolve_openai_upstream(request) == "https://api.openai.test"

    def test_resolution_failure_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*args: object, **kwargs: object) -> None:
            raise socket.gaierror("nodename nor servname provided, or not known")

        monkeypatch.setattr(openai_handler.socket, "getaddrinfo", _raise)
        proxy = _stub_proxy("https://api.openai.test")
        request = _FakeRequest({"x-headroom-base-url": "https://unresolvable.invalid"})

        assert proxy._resolve_openai_upstream(request) == "https://api.openai.test"

    def test_public_address_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(openai_handler.socket, "getaddrinfo", lambda *a, **k: _PUBLIC_ADDRINFO)
        proxy = _stub_proxy("https://api.openai.test")
        request = _FakeRequest({"x-headroom-base-url": "https://gateway.example"})

        assert proxy._resolve_openai_upstream(request) == "https://gateway.example"

    def test_operator_opt_in_restores_permissive_behavior(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HEADROOM_ALLOW_PRIVATE_UPSTREAM_BASE_URL=1 restores the pre-fix
        behavior for operators who legitimately proxy to an internal
        OpenAI-compatible gateway (LiteLLM, vLLM) on an RFC1918 address."""
        monkeypatch.setattr(
            openai_handler.socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))],
        )
        monkeypatch.setenv("HEADROOM_ALLOW_PRIVATE_UPSTREAM_BASE_URL", "1")
        proxy = _stub_proxy("https://api.openai.test")
        request = _FakeRequest({"x-headroom-base-url": "https://internal-gateway.corp"})

        assert proxy._resolve_openai_upstream(request) == "https://internal-gateway.corp"

    def test_opt_in_is_off_by_default(self) -> None:
        assert openai_handler._allow_private_upstream_base_url() is False
