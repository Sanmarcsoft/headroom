"""SSRF policy for the caller-supplied upstream base URL header.

The ``x-headroom-base-url`` header lets a client redirect this proxy's outbound
request to an arbitrary host. That is a deliberate feature for operators
fronting several OpenAI-compatible backends, and it is also a textbook SSRF
primitive: without a policy, any caller who can reach the proxy can reach the
cloud metadata service, loopback, and the whole internal network through it.

This module is the single definition of that policy. It exists as a module,
rather than as private helpers inside one handler, because the header is read
from four separate places:

  - ``headroom/proxy/handlers/openai.py``       (OpenAI-compatible routes)
  - ``headroom/providers/proxy_routes.py``      (``/v1/messages`` and the
                                                 catch-all passthrough)
  - ``headroom/providers/proxy_targets.py``     (Azure-style target selection)
  - ``headroom/providers/registry.py``          (provider runtime selection)

A guard installed at only one of those is not a guard. Enforcement therefore
happens once, at the ASGI boundary, in :func:`enforce_upstream_base_url_policy`,
which every inbound request passes through before routing. The per-handler
checks remain as defense in depth.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import cast
from urllib.parse import urlparse

from headroom.envflags import env_flag_enabled

logger = logging.getLogger(__name__)

UPSTREAM_BASE_URL_HEADER = "x-headroom-base-url"
ALLOW_PRIVATE_UPSTREAM_BASE_URL_ENV = "HEADROOM_ALLOW_PRIVATE_UPSTREAM_BASE_URL"


class UpstreamBaseUrlBlocked(ValueError):
    """Raised when a caller-supplied upstream base URL violates SSRF policy.

    Distinct from "no header supplied" on purpose. Collapsing the two lets a
    rejected upstream silently fall back to the default provider, which would
    forward the caller's prompt and Authorization header somewhere they did not
    ask for. A block must be visible to the caller, not papered over.
    """

    def __init__(self, hostname: str | None, reason: str = "ssrf_guard") -> None:
        self.hostname = hostname
        self.reason = reason
        super().__init__(
            f"upstream base URL rejected by SSRF policy (host={hostname!r}, reason={reason})"
        )


def allow_private_upstream_base_url() -> bool:
    """Operator opt-in restoring the pre-fix, unrestricted upstream behavior.

    Off by default. Some deployments legitimately proxy the header to an
    internal OpenAI-compatible gateway (self-hosted vLLM, LiteLLM) on an
    RFC1918 or loopback address. Those operators must explicitly accept that
    any caller who can reach this proxy can then also reach that internal
    target.
    """
    return env_flag_enabled(ALLOW_PRIVATE_UPSTREAM_BASE_URL_ENV)


def is_blocked_address(address: str) -> bool:
    """Return True if ``address`` must not be reachable via the header.

    Blocks loopback (127.0.0.0/8, ::1), RFC1918 private ranges, RFC4193 IPv6
    ULA (fd00::/8, a subset of the stdlib's fc00::/7 ``is_private``),
    link-local including the cloud metadata address 169.254.169.254
    (169.254.0.0/16, fe80::/10), and IPv4-mapped IPv6 forms of any of the above
    (``::ffff:127.0.0.1``). Unparseable input fails closed.
    """
    try:
        parsed: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return bool(
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_unspecified
        or parsed.is_multicast
    )


def is_blocked_hostname(hostname: str) -> bool:
    """Resolve ``hostname`` and return True if it must be blocked.

    An IP literal is checked directly with no DNS lookup. A name is resolved
    via ``socket.getaddrinfo`` and EVERY returned address is checked: an
    attacker controlling the DNS answer could otherwise order records so a
    first-record-only check passes while another returned address is private.
    Resolution failure fails closed.

    Residual risk, DNS rebinding: this resolves once, here. The outbound
    request is made later by the shared ``httpx.AsyncClient``, which resolves
    again, and there is no IP pinning between the two. A low-TTL record serving
    a public address now and a private address moments later still passes.
    Closing that needs connect-time pinning via a custom httpx transport, which
    is tracked separately and is not attempted here.
    """
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass  # Not an IP literal, resolve it below.
    else:
        return is_blocked_address(hostname)

    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return True  # Resolution failed, fail closed.
    if not infos:
        return True
    # ``getaddrinfo`` returns sockaddr as ``(host, port)`` for AF_INET and
    # ``(host, port, flowinfo, scope_id)`` for AF_INET6. mypy unions those tuple
    # shapes, so ``info[4][0]`` widens to ``str | int``; at runtime the first
    # element is always the address string. The cast records that rather than
    # papering over it with ``str()``, which would silently stringify an int.
    return any(is_blocked_address(cast(str, info[4][0])) for info in infos)


def check_upstream_base_url(raw_base_url: str | None) -> None:
    """Raise :class:`UpstreamBaseUrlBlocked` if the header value is not allowed.

    A missing or empty value is fine and returns cleanly. This is the one
    function every consumer of the header should call.

    A value with no parseable hostname (``"://bad-base"``, a bare path) also
    returns cleanly, deliberately. Such a value cannot redirect the outbound
    request anywhere: the handlers' own normalization rejects it and falls back
    to the configured upstream. Treating it as an SSRF block would turn
    malformed input into a 400 and change behaviour unrelated to this policy.
    The guard's job is to stop a *reachable* private target, not to validate
    URL syntax.
    """
    if raw_base_url is None:
        return
    candidate = raw_base_url.strip()
    if not candidate:
        return
    if allow_private_upstream_base_url():
        return

    hostname = urlparse(candidate).hostname
    if hostname is None:
        return
    if is_blocked_hostname(hostname):
        logger.warning(
            "event=upstream_base_url_blocked hostname=%s reason=ssrf_guard "
            "(set %s=1 to allow internal upstream targets)",
            hostname,
            ALLOW_PRIVATE_UPSTREAM_BASE_URL_ENV,
        )
        raise UpstreamBaseUrlBlocked(hostname)
