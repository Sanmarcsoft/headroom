"""Extras this fork deliberately does not carry.

An extra is not free just because nobody installs it. `uv lock` resolves every
declared extra, so a dependency nobody imports still lands in uv.lock, still
gets scanned, and still has to be triaged by a human in ci/vuln-baseline.txt on
every sync. The `crewai` extra was costing six baseline entries, one of them a
CRITICAL pre-authentication code injection in chromadb with no published fix,
for an integration this estate does not use.

Dropping it is a fork decision, not a bug fix, so it needs a guard: upstream
still ships the extra, and a future sync will re-add it to pyproject.toml the
same way upstream's autouse fixture re-armed itself in the 0.37.0 sync. Without
this test that would come back silently, chromadb would reappear in the lock,
and osv-scanner would fail on a PR that has nothing to do with crewai.

What is NOT removed, deliberately:

  * headroom/integrations/crewai/ stays. It guards its own import
    (`try: from crewai.tools.base_tool import BaseTool / except ImportError`),
    so it is importable with crewai absent, and its module docstring already
    tells users `pip install headroom-ai crewai` rather than the extra.
  * tests/test_integrations/crewai/ stays. It is already
    `pytest.mark.skipif(not CREWAI_AVAILABLE)`.

So the capability is still there for anyone who installs crewai themselves.
Only the resolved-by-default dependency edge is gone.
"""

from __future__ import annotations

from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"

# Extra name -> what it drags in that this fork does not want to keep triaging.
DROPPED_EXTRAS = {"crewai": ("chromadb", "json-repair")}


def _optional_dependencies() -> dict[str, list[str]]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps: dict[str, list[str]] = data["project"].get("optional-dependencies", {})
    return deps


def _locked_package_names() -> set[str]:
    data = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return {pkg["name"] for pkg in data.get("package", [])}


def test_dropped_extras_are_not_declared() -> None:
    """A sync that re-adds the extra fails here, not in osv-scanner."""
    declared = set(_optional_dependencies())
    back = sorted(declared & set(DROPPED_EXTRAS))
    assert not back, (
        f"pyproject.toml declares extras this fork dropped: {back}. An upstream "
        f"sync most likely re-added them. Remove them again and re-lock, or "
        f"delete the entry here and take on triaging what they pull in."
    )


def test_dropped_extras_left_nothing_in_the_lock() -> None:
    """The extra being gone is only worth something if the lock followed."""
    locked = _locked_package_names()
    still = sorted({p for pulled in DROPPED_EXTRAS.values() for p in pulled if p in locked})
    assert not still, (
        f"uv.lock still resolves {still}. The extra was removed from "
        f"pyproject.toml without re-locking, so nothing actually changed for "
        f"the scanners. Run `uv lock` and commit the result."
    )


def test_the_integration_module_survives_without_the_extra() -> None:
    """Dropping the extra must not break importing the integration itself."""
    import headroom.integrations.crewai as integration

    assert integration.agents.CREWAI_AVAILABLE is False, (
        "crewai is installed in this environment, so this test cannot prove the "
        "guarded-import path works; it is the absent case that matters here."
    )
    assert callable(integration.wrap_tools_with_headroom)
