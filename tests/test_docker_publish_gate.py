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
    ungated = [name for name, job in workflow["jobs"].items() if job.get("if") != GATE]
    assert ungated == [], (
        f"these docker.yml jobs would run without the publish opt-in: {ungated}. "
        f"Every job must carry `if: {GATE}`."
    )


def test_the_gate_is_a_variable_not_a_hardcoded_true(workflow: dict) -> None:
    """The gate has to be externally controlled, not a constant."""
    for name, job in workflow["jobs"].items():
        condition = job.get("if", "")
        assert "vars." in condition, f"job {name} gate is not variable-driven: {condition!r}"
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
