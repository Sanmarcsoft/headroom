"""The consolidated SSRF guard: one classifier, one resolution, one decision.

The upstream sync (#56) landed two guards that both read `x-headroom-base-url`:
this fork's `headroom/proxy/ssrf.py` (raises, enforced at the ASGI boundary,
backed by connect-time IP pinning) and upstream's
`headroom/proxy/upstream_guard.py` (returns bool, checked per call site).

Neither dominates. Measured on the merge commit, the two disagree on exactly
four inputs, and on every one of them THIS FORK is the weaker side:

    http://100.64.0.1/v1     ours ALLOW / upstream BLOCK   CGNAT, RFC 6598
    file:///etc/passwd       ours ALLOW / upstream BLOCK   no scheme check
    "://bad-base"            ours ALLOW / upstream BLOCK   no host, fails open
    "/just/a/path"           ours ALLOW / upstream BLOCK   no host, fails open

CGNAT is the one that matters: 100.64.0.0/10 is not `is_private`, so our
`is_blocked_address` waves it through, and it reaches ISP and cloud-internal
infrastructure. Upstream catches it with an `addr.is_global` test.

These tests define the consolidated target: upstream's address classifier and
scheme handling, this fork's raising API, boundary enforcement and pinning.
They are written to FAIL against the merge commit and to pass after Stage 3.
"""

from __future__ import annotations

import pathlib
import socket
from typing import Any

import pytest

from headroom.proxy.ssrf import (
    UpstreamBaseUrlBlocked,
    check_upstream_base_url,
    is_blocked_address,
)

METADATA_IP = "169.254.169.254"


# --------------------------------------------------------------------------- #
# Gap 1: RFC 6598 carrier-grade NAT shared address space.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.0",  # first address in the block
        "100.64.0.1",
        "100.100.100.100",
        "100.127.255.255",  # last address in the block
    ],
)
def test_cgnat_shared_address_space_is_blocked(address: str) -> None:
    """RFC 6598 100.64.0.0/10 is not `is_private`, so a truthiness-style check
    misses it. It routes to ISP and cloud-internal infrastructure, so it is a
    live SSRF target and must be classified internal.
    """
    assert is_blocked_address(address) is True


@pytest.mark.parametrize("address", ["100.63.255.255", "100.128.0.0"])
def test_addresses_adjacent_to_cgnat_stay_allowed(address: str) -> None:
    """The block must be exactly 100.64.0.0/10, not "anything starting 100."."""
    assert is_blocked_address(address) is False


def test_cgnat_url_is_rejected_by_the_header_guard() -> None:
    with pytest.raises(UpstreamBaseUrlBlocked) as exc:
        check_upstream_base_url("http://100.64.0.1/v1")
    assert exc.value.hostname == "100.64.0.1"


# --------------------------------------------------------------------------- #
# Gap 2: scheme. The header names a destination for an HTTP client, so only
# HTTP-family schemes may pass.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://127.0.0.1:11211/_",
        "ftp://internal.corp/",
        "data:text/plain,hi",
    ],
)
def test_non_http_schemes_are_rejected(url: str) -> None:
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url(url)


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://api.openai.com/v1"])
def test_http_family_schemes_still_pass(url: str) -> None:
    check_upstream_base_url(url)


# --------------------------------------------------------------------------- #
# Gap 3: a value with no parseable hostname must fail closed.
#
# The current docstring argues these are safe because handler normalization
# rejects them downstream. That makes the guard's correctness depend on an
# invariant held in another module. Fail closed here instead.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", ["://bad-base", "/just/a/path", "not a url at all"])
def test_value_without_a_hostname_fails_closed(url: str) -> None:
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url(url)


def test_absent_header_is_still_not_an_error() -> None:
    """Regression guard. "No header" must stay distinct from "blocked header"."""
    check_upstream_base_url(None)
    check_upstream_base_url("")
    check_upstream_base_url("   ")


# --------------------------------------------------------------------------- #
# Gap 4: resolution must be bounded. `socket.getaddrinfo` takes no timeout and
# runs on the caller's thread, which for the proxy is the event loop.
# --------------------------------------------------------------------------- #


def test_slow_resolution_is_bounded_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile hostname that resolves slowly must not stall the guard."""
    import headroom.proxy.upstream_guard as guard

    monkeypatch.setenv("HEADROOM_UPSTREAM_RESOLVE_TIMEOUT_S", "0.05")

    def _never_returns(*args: Any, **kwargs: Any) -> Any:
        import time

        # Deliberately short. A sleep long enough to be dramatic is also long
        # enough to stall interpreter shutdown, because ThreadPoolExecutor
        # joins its workers at exit. 2s against a 0.05s budget proves the
        # bound without leaving a 30s sleeper behind.
        time.sleep(2)
        raise AssertionError("resolution should have been abandoned")

    monkeypatch.setattr(guard.socket, "getaddrinfo", _never_returns)

    import time

    started = time.monotonic()
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url("http://slowloris.example/v1")
    assert time.monotonic() - started < 1.5, "guard did not bound the DNS wait"


# --------------------------------------------------------------------------- #
# Regression guards. These already pass and must keep passing through Stage 3.
# --------------------------------------------------------------------------- #


def test_every_resolved_address_is_checked_not_just_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attacker who controls the DNS answer can order the records.

    A first-record-only check passes while a later record points at loopback.
    """
    import headroom.proxy.upstream_guard as guard

    def _public_then_private(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(guard.socket, "getaddrinfo", _public_then_private)
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url("http://rebind.example/v1")


def test_resolution_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import headroom.proxy.upstream_guard as guard

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(guard.socket, "getaddrinfo", _fail)
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url("http://nope.invalid/v1")


@pytest.mark.parametrize("host", [METADATA_IP, f"[::ffff:{METADATA_IP}]"])
def test_metadata_service_is_blocked_by_literal(host: str) -> None:
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url(f"http://{host}/latest/meta-data/")


def test_metadata_service_is_blocked_by_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The literal is the obvious form; a name resolving to it is the real one."""
    import headroom.proxy.upstream_guard as guard

    monkeypatch.setattr(
        guard.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (METADATA_IP, 0))],
    )
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url("http://metadata.google.internal/")


# --------------------------------------------------------------------------- #
# Architecture guards. These pin the two properties the fork carries that
# upstream does not, so consolidation cannot quietly drop them.
# --------------------------------------------------------------------------- #


def test_outbound_client_does_not_follow_redirects() -> None:
    """A followed 3xx would bypass the guard entirely.

    The guard validates the URL the caller named, and `pinned_transport` pins
    the addresses for that host. Neither covers a Location header pointing at
    169.254.169.254. httpx defaults `follow_redirects=False`; this test exists
    so that enabling it becomes a deliberate, test-breaking decision rather
    than a one-line convenience change.
    """
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "headroom"
    offenders = [
        f"{path.relative_to(root.parent)}:{n}"
        for path in root.rglob("*.py")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"follow_redirects\s*=\s*True", line)
    ]
    assert not offenders, f"follow_redirects=True bypasses the SSRF guard: {offenders}"


def test_boundary_middleware_is_the_enforcement_point() -> None:
    """Enforcement lives at the ASGI boundary, not only at leaf call sites.

    The original CVE-2026-77775 bypass worked because the guard was installed
    in one handler while three other sinks read the same header.
    """
    from headroom.proxy import server

    source = pathlib.Path(server.__file__).read_text(encoding="utf-8")
    assert "check_upstream_base_url(request.headers.get(UPSTREAM_BASE_URL_HEADER))" in source
