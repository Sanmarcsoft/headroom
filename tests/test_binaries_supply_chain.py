"""Supply-chain hardening tests for binaries.py (pins, mirrors, unpinned behavior)."""

from __future__ import annotations

import hashlib
import logging

import pytest

import headroom.binaries as binaries

# fake_urlopen is a pytest fixture reused from the sibling module. Ruff cannot
# see fixture injection, so it reads as an unused import and as a redefinition
# at each use site.
from .test_binaries import _make_tar_gz, _set_platform, fake_urlopen  # noqa: F401


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch, tmp_path):
    """Isolate every test from global state: cache dir, platform lru_cache, env."""
    binaries.detect_platform.cache_clear()
    binaries._registry.cache_clear()
    monkeypatch.setenv("HEADROOM_BINARIES_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("HEADROOM_BINARIES_MIRROR", raising=False)
    monkeypatch.delenv("HEADROOM_BINARIES_OFFLINE", raising=False)
    monkeypatch.delenv("HEADROOM_BINARIES_ALLOW_UNPINNED", raising=False)
    monkeypatch.delenv("HEADROOM_BINARIES_MIRROR_CONFIRM", raising=False)
    yield
    binaries.detect_platform.cache_clear()
    binaries._registry.cache_clear()


def test_unpinned_asset_raises_and_deletes(monkeypatch, fake_urlopen, tmp_path):  # noqa: F811
    _set_platform(monkeypatch, sys_plat="darwin", machine="arm64")
    monkeypatch.setattr(binaries.shutil, "which", lambda _name: None)

    reg = binaries._registry()
    asset = reg["tools"]["difft"]["assets"]["darwin-aarch64"]
    original = asset.get("sha256")
    asset["sha256"] = None
    archive = _make_tar_gz({"difft": b"unpinned"})
    fake_urlopen[asset["url"]] = archive

    try:
        with pytest.raises(binaries.Sha256Unpinned) as exc:
            binaries.resolve("difft")
        assert "no sha256 pin" in str(exc.value).lower()
    finally:
        asset["sha256"] = original


def test_allow_unpinned_downgrades_to_warning(monkeypatch, fake_urlopen, caplog):  # noqa: F811
    monkeypatch.setenv("HEADROOM_BINARIES_ALLOW_UNPINNED", "1")
    _set_platform(monkeypatch, sys_plat="darwin", machine="arm64")
    monkeypatch.setattr(binaries.shutil, "which", lambda _name: None)

    reg = binaries._registry()
    asset = reg["tools"]["difft"]["assets"]["darwin-aarch64"]
    original = asset.get("sha256")
    asset["sha256"] = None
    archive = _make_tar_gz({"difft": b"allowed"})
    fake_urlopen[asset["url"]] = archive

    try:
        caplog.set_level(logging.WARNING)
        path = binaries.resolve("difft")
        assert path.read_bytes() == b"allowed"
        assert any(
            "without sha256 pin" in rec.message and "supply-chain risk" in rec.message
            for rec in caplog.records
        )
    finally:
        asset["sha256"] = original


def test_mirror_without_confirm_raises(monkeypatch):
    monkeypatch.setenv("HEADROOM_BINARIES_MIRROR", "https://mirror.example.com")
    with pytest.raises(binaries.MirrorNotConfirmed):
        binaries._mirror_url(
            "https://github.com/Wilfred/difftastic/releases/download/0.64.0/x.tar.gz"
        )


def test_mirror_with_mismatched_confirm_raises(monkeypatch):
    monkeypatch.setenv("HEADROOM_BINARIES_MIRROR", "https://mirror.example.com")
    monkeypatch.setenv("HEADROOM_BINARIES_MIRROR_CONFIRM", "https://other.example.com")
    with pytest.raises(binaries.MirrorNotConfirmed):
        binaries._mirror_url(
            "https://github.com/Wilfred/difftastic/releases/download/0.64.0/x.tar.gz"
        )


def test_mirror_with_matching_confirm_rewrites_url(monkeypatch):
    mirror = "https://mirror.example.com"
    monkeypatch.setenv("HEADROOM_BINARIES_MIRROR", mirror)
    monkeypatch.setenv("HEADROOM_BINARIES_MIRROR_CONFIRM", mirror)
    out = binaries._mirror_url(
        "https://github.com/Wilfred/difftastic/releases/download/0.64.0/x.tar.gz"
    )
    assert out == f"{mirror}/Wilfred/difftastic/releases/download/0.64.0/x.tar.gz"


def test_http_mirror_rejected_even_with_matching_confirm(monkeypatch):
    mirror = "http://mirror.example.com"
    monkeypatch.setenv("HEADROOM_BINARIES_MIRROR", mirror)
    monkeypatch.setenv("HEADROOM_BINARIES_MIRROR_CONFIRM", mirror)
    with pytest.raises(binaries.MirrorNotConfirmed):
        binaries._mirror_url(
            "https://github.com/Wilfred/difftastic/releases/download/0.64.0/x.tar.gz"
        )


def test_all_shipped_assets_have_sha256_pins():
    reg = binaries._registry()
    for tool_name, tool in reg.get("tools", {}).items():
        for plat_key, asset in tool.get("assets", {}).items():
            sha = asset.get("sha256")
            assert sha is not None, f"{tool_name}/{plat_key} has no sha256"
            assert len(sha) == 64, f"{tool_name}/{plat_key} sha256 is not 64 chars"
            assert all(c in "0123456789abcdef" for c in sha), (
                f"{tool_name}/{plat_key} sha256 is not lowercase hex"
            )


def test_ensure_tools_degrades_gracefully_on_missing_pin(monkeypatch, fake_urlopen):  # noqa: F811
    _set_platform(monkeypatch, sys_plat="darwin", machine="arm64")
    monkeypatch.setattr(binaries.shutil, "which", lambda _name: None)

    reg = binaries._registry()
    asset = reg["tools"]["difft"]["assets"]["darwin-aarch64"]
    original = asset.get("sha256")
    asset["sha256"] = None
    archive = _make_tar_gz({"difft": b"bin"})
    fake_urlopen[asset["url"]] = archive
    # ensure_tools walks every registered tool, so the other tools' fetches have
    # to be served too or the fake urlopen raises on the first one it does not
    # recognise. Serve them with matching pins; only difft is unpinned here.
    for tool_name, tool in reg.get("tools", {}).items():
        if tool_name == "difft":
            continue
        other = tool.get("assets", {}).get("darwin-aarch64")
        if other is None:
            continue
        other_archive = _make_tar_gz({tool_name: b"bin"})
        other["sha256"] = hashlib.sha256(other_archive).hexdigest()
        fake_urlopen[other["url"]] = other_archive

    try:
        tools = binaries.ensure_tools(quiet=True)
        # difft is refused because it has no pin; the unpinned tool does not
        # take the whole proxy startup down with it.
        assert tools.get("difft") is None
    finally:
        asset["sha256"] = original
