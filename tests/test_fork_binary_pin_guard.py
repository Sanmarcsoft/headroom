"""The SHA-256 pin guard must survive an upstream sync.

`headroom.binaries.Sha256Unpinned` is fork-owned: it exists 5 times at fork
main f66544fc and zero times upstream. It makes a missing pin a hard failure
instead of a fall back to bare HTTPS trust, which is the fork's headline
supply-chain control for fetched tool binaries.

The 0.37.0 sync brought upstream commit 1f96dabc, which added an *autouse*
fixture `_null_binary_pins` to tests/conftest.py. It nulls every sha256 in
headroom/tools.json for the duration of every test. Upstream can do that
safely because upstream has no fail-closed guard, so a null pin there just
degrades to HTTPS trust. Here a null pin raises, so the three tests in
test_bundled_tools_savings.py that download the real published assets died
with `Sha256Unpinned`, and nothing in the merge produced a conflict marker.

That is the same silent-loss shape as the other regressions this sync
surfaced: an upstream change disarms a fork control without a syntax error, a
lint finding, or a conflict. So the exemption is asserted here rather than
left to whoever reads the next diff.

The contract these tests pin down:

  * a test marked `real_binary_pins` sees the real registry, not a nulled one;
  * every asset shipped in tools.json actually carries a pin on disk;
  * the real-download tests carry the marker, so a future sync that drops it
    fails here instead of at a confusing `Sha256Unpinned` three files away.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from headroom import binaries

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_JSON = REPO_ROOT / "headroom" / "tools.json"

# The tests that fetch the real published assets over the network, and so must
# be measured against the real pins. Keep in sync with the module named below.
REAL_DOWNLOAD_MODULE = REPO_ROOT / "tests" / "test_bundled_tools_savings.py"
REAL_DOWNLOAD_TESTS = {
    "test_ensure_tools_installs_every_tool",
    "test_difftastic_saves_tokens_vs_line_diff",
    "test_scc_repo_shape_card_is_tiny",
}


def test_tools_json_pins_every_fetched_asset() -> None:
    """No asset ships without a pin. Read from disk, not the live registry.

    Reading the file directly rather than `binaries._registry()` is the point:
    the registry object is mutated in-process by the autouse fixture, so
    asserting against it would prove nothing about what the wheel ships.
    """
    registry = json.loads(TOOLS_JSON.read_text(encoding="utf-8"))
    unpinned = [
        f"{tool}/{platform}"
        for tool, entry in registry["tools"].items()
        for platform, asset in entry.get("assets", {}).items()
        if not asset.get("sha256")
    ]
    assert not unpinned, f"tools.json ships assets with no sha256 pin: {unpinned}"


@pytest.mark.real_binary_pins
def test_marker_exempts_a_test_from_the_nulling_fixture() -> None:
    """Inside a marked test the registry still holds the published pins.

    Without the exemption every value here is None and any real download
    raises Sha256Unpinned before it can be verified.
    """
    nulled = [
        f"{tool}/{platform}"
        for tool, entry in binaries._registry()["tools"].items()
        for platform, asset in entry.get("assets", {}).items()
        if not asset.get("sha256")
    ]
    assert not nulled, (
        "the real_binary_pins marker did not exempt this test from "
        f"_null_binary_pins; these assets are still nulled: {nulled}"
    )


def test_nulling_fixture_still_applies_without_the_marker() -> None:
    """The exemption is opt-in, so the installer mechanics tests keep working.

    Those fetch small mock archives whose digests cannot match the published
    pins, which is why upstream nulls them in the first place.
    """
    difft = binaries._registry()["tools"]["difft"]["assets"]
    assert all(asset.get("sha256") is None for asset in difft.values()), (
        "unmarked tests should still see nulled pins; the exemption leaked"
    )


def test_real_download_tests_carry_the_marker() -> None:
    """A sync that drops the marker fails here, not three files away."""
    tree = ast.parse(REAL_DOWNLOAD_MODULE.read_text(encoding="utf-8"))
    marked = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        for deco in node.decorator_list
        if ast.unparse(deco) == "pytest.mark.real_binary_pins"
    }
    missing = REAL_DOWNLOAD_TESTS - marked
    assert not missing, (
        f"{REAL_DOWNLOAD_MODULE.name} downloads the real published assets, so "
        f"these must be marked `real_binary_pins`: {sorted(missing)}"
    )
