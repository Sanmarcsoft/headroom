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

import asyncio
import logging
from urllib.parse import urlparse

from headroom.envflags import env_flag_enabled
from headroom.proxy.upstream_guard import (
    ALLOWED_BASE_URLS_ENV,
    REASON_BAD_SCHEME,
    REASON_INTERNAL_ADDRESS,
    REASON_NO_HOST,
    REASON_NOT_ALLOWLISTED,
    REASON_UNRESOLVABLE,
    classify_upstream_url,
    is_internal_address,
    resolve_host_addresses,
)

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

    Delegates to the shared classifier in
    :mod:`headroom.proxy.upstream_guard`, which is the fork's single definition
    of "internal". This module used to carry a second one; the two agreed on
    loopback, RFC1918, link-local, IPv4-mapped, NAT64, 6to4 and Teredo, and
    disagreed on RFC 6598 shared address space (100.64.0.0/10), which is not
    ``is_private`` and which this module therefore allowed. Two definitions of
    a security predicate is one too many.
    """
    return is_internal_address(address)


def is_blocked_hostname(hostname: str) -> bool:
    """Resolve ``hostname`` and return True if it must be blocked.

    An IP literal is checked directly with no DNS lookup. A name is resolved
    under a bounded wait and EVERY returned address is checked: an attacker
    controlling the DNS answer could otherwise order records so a
    first-record-only check passes while another returned address is private.
    Resolution failure, and resolution that overruns its budget, fail closed.

    Residual risk, DNS rebinding: this resolves once, here, and the outbound
    request resolves again later. That gap is closed separately by
    :mod:`headroom.proxy.pinned_transport`, which resolves once, checks every
    address, and then connects to the IP literal while preserving the Host
    header and TLS SNI.
    """
    addresses = resolve_host_addresses(hostname)
    if addresses is None:
        return True
    return any(is_internal_address(address) for address in addresses)


# One message per reason code, in one place. Both rejection sites (the boundary
# middleware in proxy/server.py and the per-route handler in
# providers/proxy_routes.py) render through this. They previously carried two
# copies of a single hardcoded sentence, written before classify_upstream_url
# grew reason codes, so every rejection claimed the value "resolves to a
# loopback, private, link-local or otherwise reserved address" -- including
# `://bad-base`, which has no host to resolve and interpolated its hostname as
# None. A caller debugging their own configuration was told something untrue.
# One template per reason code, in one place. Both rejection sites (the boundary
# middleware in proxy/server.py and the per-route handler in
# providers/proxy_routes.py) render through this.
#
# `{host}` is filled only where the reason actually produced a hostname. The
# three parse-level reasons never do: urlparse("://bad-base") yields an empty
# scheme and a None host, so a template that names a host would print None.
_REASON_TEMPLATES: dict[str, str] = {
    REASON_BAD_SCHEME: "the scheme is not http or https",
    REASON_NO_HOST: "the value has no host",
    REASON_NOT_ALLOWLISTED: "host {host} is not named in " + ALLOWED_BASE_URLS_ENV,
    REASON_UNRESOLVABLE: "host {host} could not be resolved within the timeout",
    REASON_INTERNAL_ADDRESS: (
        "host {host} resolves to a loopback, private, link-local or otherwise reserved address"
    ),
}


def describe_upstream_block(hostname: str | None, reason: str) -> str:
    """Render the caller-facing explanation for a blocked upstream base URL.

    Before this existed, both rejection sites carried the same hardcoded
    sentence, written before :func:`classify_upstream_url` grew reason codes.
    Every rejection therefore claimed the value resolved to a reserved address
    -- including one with no host to resolve, whose hostname interpolated as
    ``None``. A caller debugging their own configuration was told something
    untrue, in two places that could drift independently.
    """
    template = _REASON_TEMPLATES.get(reason, f"it violates SSRF policy ({reason})")
    detail = template.format(host=repr(hostname) if hostname else "the supplied host")
    return f"upstream base URL rejected by SSRF policy: {detail}"


def check_upstream_base_url(raw_base_url: str | None) -> None:
    """Raise :class:`UpstreamBaseUrlBlocked` if the header value is not allowed.

    A missing or empty value is fine and returns cleanly. This is the one
    function every consumer of the header should call.

    Anything else must earn its way through
    :func:`headroom.proxy.upstream_guard.classify_upstream_url`, which is the
    same code path behind :func:`~headroom.proxy.upstream_guard.is_safe_upstream_url`.
    That includes a non-HTTP scheme and a value with no parseable hostname,
    both of which this function used to wave through on the argument that
    handler normalization would reject them downstream. It does, today. Making
    a security guard's correctness depend on an invariant maintained in another
    module is how the original bypass happened, so both now fail closed.
    """
    if raw_base_url is None:
        return
    candidate = raw_base_url.strip()
    if not candidate:
        return
    if allow_private_upstream_base_url():
        return

    reason = classify_upstream_url(candidate)
    if reason is None:
        return

    hostname = urlparse(candidate).hostname
    logger.warning(
        "event=upstream_base_url_blocked hostname=%s reason=%s "
        "(set %s=1 to allow internal upstream targets, or name the host in %s)",
        hostname,
        reason,
        ALLOW_PRIVATE_UPSTREAM_BASE_URL_ENV,
        ALLOWED_BASE_URLS_ENV,
    )
    raise UpstreamBaseUrlBlocked(hostname, reason=reason)


async def check_upstream_base_url_async(raw_base_url: str | None) -> None:
    """Async form of :func:`check_upstream_base_url` for event-loop callers.

    Same policy and the same exception. The bounded resolution runs off the
    loop so a hostile or slow-resolving hostname cannot stall unrelated
    in-flight requests.
    """
    await asyncio.to_thread(check_upstream_base_url, raw_base_url)
