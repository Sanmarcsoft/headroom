"""The Docker publish workflow must stay opt-in.

`docker.yml` pushes to ghcr.io/<this repo> and cosign-signs the result with the
repository's own keyless Sigstore identity. On a fork that mirrors upstream,
syncing means pushing to main, so every sync published signed images of code
nobody here had reviewed. A signature that vouches for unreviewed third-party
work is worse than no signature.

These tests fail if the opt-in gate is removed or weakened.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docker.yml"
GATE = "vars.HEADROOM_PUBLISH_IMAGES == 'true'"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_every_job_is_gated(workflow: dict) -> None:
    """No job may run without the explicit opt-in.

    The gate is repeated on each job rather than relying on `needs`, so that
    removing or reordering a dependency cannot silently re-enable publishing.
    """
    ungated = [name for name, job in workflow["jobs"].items() if GATE not in job.get("if", "")]
    assert ungated == [], (
        f"these docker.yml jobs would run without the publish opt-in: {ungated}. "
        f"Every job must carry `if: {GATE}`."
    )


def test_no_job_declares_two_if_keys() -> None:
    """A second `if:` silently replaces the first, because YAML keeps the last.

    This is not hypothetical. The upstream sync (#56) added
    `if: ${{ always() }}` to docker-manifest alongside the gate, and the gate
    was dead from that merge until it was found. yaml.safe_load cannot show
    this, because by the time it returns there is only one key left.
    """
    import yaml as _yaml

    duplicates: list[str] = []

    class _Loader(_yaml.SafeLoader):
        pass

    def _mapping(loader, node, deep=False):  # type: ignore[no-untyped-def]
        seen: dict = {}
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                duplicates.append(f"{key!r} redefined at line {key_node.start_mark.line + 1}")
            seen[key] = True
        return _yaml.SafeLoader.construct_mapping(loader, node, deep)

    _Loader.add_constructor(_yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    _yaml.load(WORKFLOW.read_text(encoding="utf-8"), _Loader)

    assert duplicates == [], f"duplicate keys in docker.yml: {duplicates}"


def test_the_gate_is_a_variable_not_a_hardcoded_true(workflow: dict) -> None:
    """The gate has to be externally controlled, not a constant."""
    for name, job in workflow["jobs"].items():
        condition = job.get("if", "")
        assert "vars." in condition, f"job {name} gate is not variable-driven: {condition!r}"
        assert GATE in condition, f"job {name} lost the publish gate: {condition!r}"
        assert condition.strip() not in {"true", "True", "${{ true }}"}, (
            f"job {name} is gated on a constant"
        )


def test_workflow_still_pushes_and_signs(workflow: dict) -> None:
    """Guards the premise of this test file.

    If docker.yml ever stops pushing and signing, the gate is no longer load
    bearing and this file should be revisited rather than left to rot.
    """
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "push: true" in raw or "push=true" in raw, "workflow no longer pushes; revisit the gate"
    assert "cosign" in raw, "workflow no longer signs; revisit the gate"
