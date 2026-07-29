#!/usr/bin/env python3
"""Fail on any dependency advisory that is not already recorded in the baseline.

pip-audit answers a narrower question than it appears to. It audits the
production export (``uv export --no-dev --extra all``, 620 packages) and
reported zero vulnerabilities while GitHub held 22 open Dependabot alerts. Both
numbers were correct: every one of those 22 sat outside the export, in an
opt-in extra or in one of the five npm lockfiles nothing here was reading.

So this reads the lockfiles instead of an export, across every ecosystem the
repository actually ships: uv.lock, Cargo.lock, and the npm lockfiles. That
also covers cargo, which Dependabot on this repo does not scan at all.

A fork inherits vulnerabilities it did not introduce and mostly cannot fix, so
failing on the total would produce a permanently red job, which is the same as
no job. The baseline holds the findings that have been looked at, each with a
reason. Anything not in it fails. That way a newly disclosed CVE is a red build
on the PR that would ship it, and the inherited backlog stays visible without
blocking.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "ci" / "vuln-baseline.txt"


def load_baseline() -> dict[str, str]:
    """Map advisory id -> reason. An entry without a reason is an error."""
    if not BASELINE.exists():
        return {}
    entries: dict[str, str] = {}
    for lineno, raw in enumerate(BASELINE.read_text().splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        ident, _, reason = raw.partition("#")
        ident, reason = ident.strip(), reason.strip()
        if not reason:
            print(f"::error::{BASELINE.name} line {lineno}: {ident} has no reason.")
            sys.exit(1)
        entries[ident] = reason
    return entries


def load_findings(report: Path) -> list[dict[str, str]]:
    """Flatten osv-scanner's nested JSON into one row per (package, advisory)."""
    data = json.loads(report.read_text())
    rows: list[dict[str, str]] = []
    for result in data.get("results", []):
        source = result.get("source", {}).get("path", "?")
        try:
            source = str(Path(source).relative_to(REPO_ROOT))
        except ValueError:
            pass
        for pkg in result.get("packages", []):
            info = pkg.get("package", {})
            for vuln in pkg.get("vulnerabilities", []):
                rows.append(
                    {
                        "id": vuln.get("id", "?"),
                        "source": source,
                        "package": f"{info.get('name', '?')}@{info.get('version', '?')}",
                        "summary": (vuln.get("summary") or "").split("\n")[0][:90],
                    }
                )
    return rows


def summary_table(rows: list[dict[str, str]], baseline: dict[str, str]) -> str:
    lines = [
        "### Dependency advisories",
        "",
        "| advisory | package | lockfile | status |",
        "|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: (r["source"], r["package"], r["id"])):
        status = "known" if row["id"] in baseline else "**NEW**"
        lines.append(f"| {row['id']} | {row['package']} | `{row['source']}` | {status} |")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(__file__).name} <osv-scanner-json>", file=sys.stderr)
        return 2

    rows = load_findings(Path(sys.argv[1]))
    baseline = load_baseline()

    found_ids = {r["id"] for r in rows}
    new = [r for r in rows if r["id"] not in baseline]
    stale = sorted(set(baseline) - found_ids)

    print(f"advisories found: {len(rows)}  baselined: {len(baseline)}  new: {len(new)}")

    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as fh:
            fh.write(summary_table(rows, baseline) + "\n")

    # A baselined advisory that no longer appears means the dependency was
    # upgraded. Prune it, or the file slowly stops describing anything. Not a
    # failure: it is good news, and failing on good news trains people to skip
    # the check.
    for ident in stale:
        print(f"::notice::{ident} is no longer reported. Remove it from {BASELINE.name}.")

    for row in new:
        print(
            f"::error::{row['id']} in {row['package']} ({row['source']}) is not in "
            f"{BASELINE.name}. Upgrade the dependency, or add it with a reason."
        )
        print(f"    {row['summary']}")

    return 1 if new else 0


if __name__ == "__main__":
    raise SystemExit(main())
