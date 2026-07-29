#!/usr/bin/env python3
"""Keep ci/quarantine.txt from becoming a place to put failing tests.

The gate deselects everything listed there. That is the honest way to run CI on
a fork whose inherited suite already has failures, but it is one edit away from
being the way anyone makes a red build green. The ceiling makes that edit
visible: raising it is a diff, in a PR, with a reason attached.

The ceiling and every entry's reason are checked here. An entry without a reason
is how a quarantine list stops being reviewable after six months.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
QUARANTINE = REPO_ROOT / "ci" / "quarantine.txt"

# Set from the measured pre-existing failure count on the clean tree at the time
# the gate was introduced (2026-07-29). It may go down. It goes up only with a
# deliberate edit to this line.
CEILING = 20

NODE_ID = re.compile(r"^[\w./-]+\.py(::[\w\[\]-]+)+$")


def main() -> int:
    if not QUARANTINE.exists():
        print(f"no {QUARANTINE.relative_to(REPO_ROOT)}; nothing quarantined")
        return 0

    entries: list[tuple[int, str, str]] = []
    problems: list[str] = []

    for lineno, raw in enumerate(QUARANTINE.read_text().splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        node, _, reason = raw.partition("#")
        node, reason = node.strip(), reason.strip()
        if not reason:
            problems.append(f"line {lineno}: {node} has no reason. Every entry needs one.")
        if not NODE_ID.match(node):
            problems.append(f"line {lineno}: {node!r} does not look like a pytest node id.")
        entries.append((lineno, node, reason))

    duplicates = {n for _, n, _ in entries if [x for _, x, _ in entries].count(n) > 1}
    for dup in sorted(duplicates):
        problems.append(f"{dup} is listed more than once")

    print(f"quarantined: {len(entries)}  ceiling: {CEILING}")
    for _, node, reason in entries:
        print(f"  {node}\n      {reason}")

    if len(entries) > CEILING:
        problems.append(
            f"quarantine holds {len(entries)} tests, ceiling is {CEILING}. "
            f"Fix the test, or raise CEILING in {Path(__file__).name} and say why in the PR."
        )

    for p in problems:
        print(f"::error::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
