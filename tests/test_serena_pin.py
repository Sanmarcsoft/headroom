"""Tests for the immutable Serena pin (single source of truth)."""

from __future__ import annotations

import inspect
import re

from headroom.cli.wrap import _index_serena_project
from headroom.mcp_registry.install import SERENA_PACKAGE_SPEC, build_serena_spec


def test_serena_package_spec_is_immutable_commit_ref():
    """SERENA_PACKAGE_SPEC contains an immutable 40-hex-char commit ref (not a branch)."""
    assert re.match(
        r"^git\+https://github\.com/oraios/serena@[0-9a-f]{40}$", SERENA_PACKAGE_SPEC
    ), f"expected immutable commit ref, got: {SERENA_PACKAGE_SPEC}"


def test_build_serena_spec_and_wrap_index_use_same_constant():
    """build_serena_spec().args and the index command in wrap.py both reference
    the same constant (imported, never re-hardcoded).
    """
    spec = build_serena_spec("test-context")
    assert SERENA_PACKAGE_SPEC in spec.args

    source = inspect.getsource(_index_serena_project)
    assert "SERENA_PACKAGE_SPEC" in source
    assert "from headroom.mcp_registry.install import SERENA_PACKAGE_SPEC" in source
