"""Connect-time IP pinning for caller-steered upstream requests.

The SSRF guard in :mod:`headroom.proxy.ssrf` resolves a caller-supplied
hostname at header-parse time and refuses private targets. That check and the
actual connection are two separate resolutions: httpx resolves the name again
when it opens the socket, and nothing ties the two together. An attacker
serving a low-TTL record that answers public on the first lookup and private on
the second walks straight through the guard. That is DNS rebinding, and it is
the residual risk the guard's own docstring documents.

This module closes it by making the validated answer the answer that is used:
resolve once, check every address, then connect to that exact IP literal while
preserving the original ``Host`` header and TLS SNI so virtual hosting and
certificate validation still work.

Scope, deliberately narrow. Pinning every outbound request would defeat
connection reuse for ordinary provider traffic, which is the hot path and is
not caller-steerable in the first place. The transport therefore pins only
hosts that are NOT one of the proxy's configured upstream targets. A host
outside that set can only have come from a caller-supplied base URL, which is
precisely the traffic the SSRF policy governs.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, cast

import httpx

from headroom.proxy.ssrf import (
    UpstreamBaseUrlBlocked,
    allow_private_upstream_base_url,
    is_blocked_address,
)

logger = logging.getLogger(__name__)


def _literal_for_url(address: str) -> str:
    """Return ``address`` in the form httpx expects inside a URL host slot."""
    parsed = ipaddress.ip_address(address)
    return f"[{address}]" if parsed.version == 6 else address


def resolve_and_validate(hostname: str) -> str:
    """Resolve ``hostname`` and return the single address that will be used.

    Every returned address is checked, not just the first: an attacker who
    controls the DNS answer could otherwise order the records so a naive
    first-record check passes while another address is private. Resolution
    failure fails closed.

    The address returned is the one the caller must actually connect to. That
    is the whole point: validating one address and connecting to another is the
    bug this function exists to prevent.
    """
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if is_blocked_address(hostname):
            raise UpstreamBaseUrlBlocked(hostname)
        return hostname

    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UpstreamBaseUrlBlocked(hostname, reason="resolution_failed") from exc
    if not infos:
        raise UpstreamBaseUrlBlocked(hostname, reason="resolution_empty")

    # ``getaddrinfo`` returns sockaddr as ``(host, port)`` for AF_INET and
    # ``(host, port, flowinfo, scope_id)`` for AF_INET6. mypy unions those tuple
    # shapes, so ``info[4][0]`` widens to ``str | int``; at runtime the first
    # element is always the address string. The cast records that rather than
    # papering over it with ``str()``, which would silently stringify an int.
    addresses = [cast(str, info[4][0]) for info in infos]
    for address in addresses:
        if is_blocked_address(address):
            logger.warning(
                "event=upstream_pin_blocked hostname=%s address=%s reason=ssrf_guard",
                hostname,
                address,
            )
            raise UpstreamBaseUrlBlocked(hostname)
    return addresses[0]


class PinnedUpstreamTransport(httpx.AsyncBaseTransport):
    """Wraps a transport, pinning caller-steered hosts to a validated address.

    ``trusted_hosts`` are the proxy's own configured upstream targets. Requests
    to those pass through untouched, keeping connection reuse intact on the hot
    path. Anything else is resolved, validated, and rewritten to connect to the
    validated IP literal.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, trusted_hosts: set[str]) -> None:
        self._inner = inner
        self._trusted = {host.lower() for host in trusted_hosts if host}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host or host.lower() in self._trusted:
            return await self._inner.handle_async_request(request)

        # The operator opt-in that relaxes the SSRF policy relaxes pinning too:
        # someone deliberately fronting an internal gateway has accepted that
        # the target is private, and pinning would only add latency.
        if allow_private_upstream_base_url():
            return await self._inner.handle_async_request(request)

        address = resolve_and_validate(host)
        if address == host:
            return await self._inner.handle_async_request(request)

        # Connect to the validated literal, but keep the request looking to the
        # server exactly as it did before: Host for virtual hosting and routing,
        # sni_hostname so TLS still presents and validates the real name.
        original_host_header = request.headers.get("host") or request.url.netloc.decode("ascii")
        request.url = request.url.copy_with(host=_literal_for_url(address))
        request.headers["host"] = original_host_header
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_pinned_transport(
    trusted_hosts: set[str],
    **client_kwargs: Any,
) -> PinnedUpstreamTransport:
    """Build the pinning transport around a standard httpx async transport."""
    inner = httpx.AsyncHTTPTransport(
        verify=client_kwargs.get("verify", True),
        limits=client_kwargs.get("limits", httpx.Limits()),
        proxy=client_kwargs.get("proxy"),
        http2=client_kwargs.get("http2", False),
    )
    return PinnedUpstreamTransport(inner, trusted_hosts)
