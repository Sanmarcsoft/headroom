"""Structural invariants for scripts/install.sh hardening (red-team wave 1, #5, #6).

These are grep-level assertions rather than behavioural tests because the file is
a bash installer that shells out to Docker. The point is to make a regression
loud: if someone reintroduces a bare port publish or an unconditional unsafe
install, a test fails instead of a laptop quietly appearing on the LAN.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def test_no_bare_port_publish(script: str) -> None:
    """A bare -p PORT:PORT binds 0.0.0.0 on the host.

    That puts the proxy, which fronts the operator's provider credentials and
    requires no token by default, on every interface the machine has.
    """
    bare = re.findall(r'-p "\$\{port\}:\$\{port\}"', script)
    assert bare == [], (
        f"{len(bare)} bare port publish(es) found; use publish_spec so the host "
        "binding stays on loopback unless HEADROOM_PUBLISH_HOST widens it"
    )


def test_publish_spec_defaults_to_loopback(script: str) -> None:
    assert "publish_spec()" in script, "publish_spec helper is missing"
    match = re.search(r'local host="\$\{HEADROOM_PUBLISH_HOST:-([^}"]+)\}"', script)
    assert match is not None, "publish_spec does not read HEADROOM_PUBLISH_HOST with a default"
    assert match.group(1) == "127.0.0.1", (
        f"publish_spec defaults to {match.group(1)}, expected 127.0.0.1"
    )


def test_every_docker_publish_uses_publish_spec(script: str) -> None:
    """Every port publish goes through the loopback-defaulting helper.

    Line-based rather than arg-based: the publish argument itself contains
    nested quotes, `-p "$(publish_spec "${port}")"`, which a naive quoted-string
    regex splits in the wrong place.
    """
    publish_lines = [
        line.strip()
        for line in script.splitlines()
        if '-p "' in line and "mkdir" not in line and "port" in line
    ]
    assert publish_lines, "expected at least one docker port publish in the installer"
    for line in publish_lines:
        assert "publish_spec" in line, f"publish bypasses publish_spec: {line}"


def test_openclaw_unsafe_install_is_opt_in(script: str) -> None:
    """OpenClaw's safety gate must not be bypassed without the operator asking."""
    unconditional = re.findall(
        r"openclaw plugins install --dangerously-force-unsafe-install", script
    )
    assert unconditional == [], (
        "install.sh passes --dangerously-force-unsafe-install unconditionally; "
        "gate it behind HEADROOM_OPENCLAW_UNSAFE_INSTALL"
    )
    assert "HEADROOM_OPENCLAW_UNSAFE_INSTALL" in script, "the opt-in env var is missing"


def test_agent_config_mounts_have_a_read_only_switch(script: str) -> None:
    """Read-write is the default because the wrapped agents write session state.

    The switch has to exist so an operator who accepts losing that state can
    close the mount.
    """
    assert "HEADROOM_AGENT_CONFIG_RO" in script, "no read-only switch for agent config mounts"


def test_image_default_documents_digest_pinning(script: str) -> None:
    """A mutable tag decides which image receives the mounted credentials."""
    idx = script.index('IMAGE_DEFAULT="')
    preamble = script[max(0, idx - 600) : idx]
    assert "@sha256:" in preamble, (
        "IMAGE_DEFAULT is a mutable tag with no digest-pinning guidance above it"
    )
