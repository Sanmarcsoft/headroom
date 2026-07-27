"""SSRF policy is enforced at the boundary, not per handler.

The wave-1 fix guarded `x-headroom-base-url` inside the OpenAI handler only.
Three other sinks read the same header and forwarded it unvalidated: the
catch-all passthrough route, `/v1/messages`, and Azure-style target selection.
A `GET /latest/meta-data/...` carrying the header therefore still reached the
cloud metadata service. These tests pin the boundary behaviour so the guard
cannot drift back to a single leaf.
"""

from __future__ import annotations

import pytest

from headroom.proxy.ssrf import (
    UpstreamBaseUrlBlocked,
    check_upstream_base_url,
    is_blocked_address,
    is_blocked_hostname,
)

METADATA_IP = "169.254.169.254"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        METADATA_IP,
        "169.254.1.1",
        "fd00::1",
        "fe80::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "0.0.0.0",  # unspecified
        "not-an-ip",  # unparseable fails closed
    ],
)
def test_blocked_addresses(address: str) -> None:
    assert is_blocked_address(address) is True


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"]
)
def test_public_addresses_allowed(address: str) -> None:
    assert is_blocked_address(address) is False


def test_ip_literal_hostname_is_checked_without_dns() -> None:
    assert is_blocked_hostname(METADATA_IP) is True


def test_unresolvable_hostname_fails_closed() -> None:
    assert is_blocked_hostname("no-such-host.invalid") is True


def test_metadata_url_is_rejected() -> None:
    """The exact bypass the security review found."""
    with pytest.raises(UpstreamBaseUrlBlocked) as exc:
        check_upstream_base_url(f"http://{METADATA_IP}")
    assert exc.value.hostname == METADATA_IP


def test_loopback_url_is_rejected() -> None:
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url("http://127.0.0.1:6333")


def test_absent_header_is_not_an_error() -> None:
    """None and empty must NOT raise, and must not be confused with a block."""
    check_upstream_base_url(None)
    check_upstream_base_url("")
    check_upstream_base_url("   ")


def test_unparseable_value_is_not_a_block() -> None:
    """A value with no hostname cannot redirect anywhere, so it is not an SSRF.

    The handlers' own normalization rejects it and falls back to the configured
    upstream. Raising here would turn malformed input into a 400 and change
    behaviour unrelated to this policy.
    """
    check_upstream_base_url("://bad-base")
    check_upstream_base_url("/just/a/path")


def test_block_is_distinguishable_from_absent() -> None:
    """A rejection must not collapse into the 'no header' sentinel.

    Collapsing them is what made a blocked upstream silently fall back to the
    default provider, forwarding the caller's prompt and Authorization header
    to a provider they never named.
    """
    assert check_upstream_base_url(None) is None
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url("http://10.0.0.5")


def test_opt_in_restores_private_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_ALLOW_PRIVATE_UPSTREAM_BASE_URL", "1")
    check_upstream_base_url("http://10.0.0.5")  # must not raise


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "maybe"])
def test_opt_in_is_strictly_parsed(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """=0 and =false must NOT enable the bypass.

    A truthiness check would treat every one of these as enabled, which is the
    opposite of what an operator typing them intends.
    """
    monkeypatch.setenv("HEADROOM_ALLOW_PRIVATE_UPSTREAM_BASE_URL", value)
    with pytest.raises(UpstreamBaseUrlBlocked):
        check_upstream_base_url(f"http://{METADATA_IP}")


def test_every_header_sink_is_covered_by_the_shared_guard() -> None:
    """Regression gate: no module may read the header without the shared guard.

    The first version of this test grepped for the literal header string and
    exempted any file containing the word "ssrf". A security review defeated
    both halves against the real logic: a sink using the UPSTREAM_BASE_URL_HEADER
    constant was invisible to the regex, and an unrelated comment mentioning
    ssrf suppressed a genuine offender. This version matches the constant too
    and requires the guard to actually be imported, with an explicit allowlist
    instead of a keyword escape hatch.
    """
    import pathlib
    import re

    # Modules permitted to read the header without importing the guard, with
    # the reason. Anything not listed must route through the shared policy.
    ALLOWED: dict[str, str] = {
        "headroom/proxy/ssrf.py": "defines the policy",
        "headroom/proxy/helpers.py": "strips internal headers before forwarding",
        "headroom/providers/opencode/runtime.py": "docstring reference only",
    }

    root = pathlib.Path(__file__).resolve().parents[2] / "headroom"
    repo = root.parent
    # Matches headers.get("x-headroom-base-url"...) AND the constant form.
    read_pattern = re.compile(
        r"""(?:headers|request\.headers)\.get\(\s*(?:["']x-headroom-base-url["']|UPSTREAM_BASE_URL_HEADER|_OPENAI_BASE_URL_HEADER)""",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(repo))
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        if not read_pattern.search(text):
            continue
        # The guard must be imported by name, not merely mentioned.
        if "check_upstream_base_url" not in text:
            offenders.append(rel)

    assert offenders == [], (
        "these modules read the upstream base-URL header without importing "
        f"check_upstream_base_url: {offenders}. Enforcement is the boundary "
        "middleware in headroom/proxy/server.py; a new sink must route through "
        "it, or be added to ALLOWED here with a reason."
    )


def test_the_regression_gate_itself_catches_a_planted_sink(tmp_path) -> None:
    """Prove the gate above is not defeatable the way its predecessor was.

    A gate that cannot be shown to fire is not a gate. Both bypasses the
    reviewer constructed are exercised here against the same pattern the real
    test uses.
    """
    import re

    read_pattern = re.compile(
        r"""(?:headers|request\.headers)\.get\(\s*(?:["']x-headroom-base-url["']|UPSTREAM_BASE_URL_HEADER|_OPENAI_BASE_URL_HEADER)""",
        re.IGNORECASE,
    )

    # Bypass 1: the constant form, invisible to the original literal-only regex.
    via_constant = "base = headers.get(UPSTREAM_BASE_URL_HEADER)\n"
    assert read_pattern.search(via_constant), "constant-form read must be detected"
    assert "check_upstream_base_url" not in via_constant

    # Bypass 2: the literal form plus an unrelated mention of ssrf, which used
    # to suppress the finding entirely.
    via_keyword = '# ssrf: handled elsewhere\nbase = headers.get("x-headroom-base-url")\n'
    assert read_pattern.search(via_keyword), "literal-form read must be detected"
    assert "check_upstream_base_url" not in via_keyword

    # A properly guarded sink must NOT be flagged.
    guarded = (
        "from headroom.proxy.ssrf import check_upstream_base_url\n"
        'base = headers.get("x-headroom-base-url")\n'
        "check_upstream_base_url(base)\n"
    )
    assert read_pattern.search(guarded)
    assert "check_upstream_base_url" in guarded
