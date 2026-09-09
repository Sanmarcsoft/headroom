"""Guard against workflow step conditions that can never be true.

GitHub Actions resolves an unknown context reference to the empty string
instead of failing. A condition copied between jobs therefore keeps its
syntax, keeps passing YAML validation, keeps passing actionlint, and
silently evaluates to false forever.

That is exactly how the upstream sync killed this fork's ``:latest``
promotion: ``promote-latest`` is a fork-only job with no matrix and no
step with ``id: manifest``, and it inherited the in-manifest condition
``steps.manifest.outputs.index_digest != '' && matrix.variant.name == ''``
from upstream's ``docker-manifest`` job. Both operands are empty in that
job, so ``'' != ''`` is false on every run. Nothing in the merge showed
it: no conflict marker, no duplicate key, no parse error.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

STEPS_REF = re.compile(r"\bsteps\.([A-Za-z0-9_-]+)\.")
MATRIX_REF = re.compile(r"\bmatrix\.")


def _jobs(path: Path) -> dict[str, Any]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(workflow, dict):
        return {}
    jobs = workflow.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def test_workflow_files_are_discovered() -> None:
    """A silent empty glob would make every guard below vacuously pass."""
    assert WORKFLOWS, "no workflow files found under .github/workflows"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_step_conditions_reference_only_ids_declared_in_their_own_job(
    path: Path,
) -> None:
    """``steps.<id>`` in an ``if:`` must name a step in the same job."""
    offenders: list[str] = []
    for job_name, job in _jobs(path).items():
        steps = job.get("steps") if isinstance(job, dict) else None
        if not isinstance(steps, list):
            continue
        declared = {step["id"] for step in steps if isinstance(step, dict) and "id" in step}
        for step in steps:
            if not isinstance(step, dict):
                continue
            condition = str(step.get("if", ""))
            for referenced in STEPS_REF.findall(condition):
                if referenced not in declared:
                    offenders.append(
                        f"{path.name}:{job_name}:{step.get('name', '<unnamed>')}"
                        f" references steps.{referenced} which that job never declares"
                    )
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_step_conditions_use_matrix_only_in_matrix_jobs(path: Path) -> None:
    """``matrix.*`` in an ``if:`` is always empty outside a matrix job."""
    offenders: list[str] = []
    for job_name, job in _jobs(path).items():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy")
        has_matrix = isinstance(strategy, dict) and "matrix" in strategy
        if has_matrix:
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            if MATRIX_REF.search(str(step.get("if", ""))):
                offenders.append(
                    f"{path.name}:{job_name}:{step.get('name', '<unnamed>')}"
                    " uses matrix.* but the job declares no matrix"
                )
    assert not offenders, "\n".join(offenders)
