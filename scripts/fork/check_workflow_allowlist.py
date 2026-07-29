#!/usr/bin/env python3
"""Fail when .github/workflows/ contains a file nobody on this fork reviewed.

This fork tracks a repository that ships roughly 20 commits a day. A sync can
add a workflow file, and a workflow file that lands on main runs on main. The
per-workflow "disabled" state GitHub stores is keyed by path, so it protects the
22 files that existed when the allowlist was written and says nothing about the
23rd.

The allowlist closes that. Every file under .github/workflows/ must appear in
ci/workflow-allowlist.txt, marked either `fork` (we wrote it, it is expected to
run) or `inherited` (upstream's, expected to be disabled). Anything else fails,
which turns a silent addition into a red check on the sync PR.

This is a file-level check. Whether the inherited ones are actually still
disabled is a different question, answered by check_workflow_state.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ALLOWLIST = REPO_ROOT / "ci" / "workflow-allowlist.txt"


def parse_allowlist(path: Path) -> dict[str, str]:
    """Return {filename: kind} from `<kind>  <filename>  # optional note`."""
    entries: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            sys.exit(f"{path}:{lineno}: expected '<kind> <filename>', got {raw!r}")
        kind, name = parts
        if kind not in {"fork", "inherited"}:
            sys.exit(f"{path}:{lineno}: kind must be 'fork' or 'inherited', got {kind!r}")
        entries[name] = kind
    return entries


def main() -> int:
    if not ALLOWLIST.exists():
        sys.exit(f"missing allowlist: {ALLOWLIST}")

    allowed = parse_allowlist(ALLOWLIST)
    present = {p.name for p in sorted(WORKFLOW_DIR.glob("*.y*ml"))}

    unreviewed = sorted(present - allowed.keys())
    missing = sorted(allowed.keys() - present)

    for name in unreviewed:
        print(
            f"::error file=.github/workflows/{name}::workflow file is not in "
            f"ci/workflow-allowlist.txt. If it arrived from an upstream sync, "
            f"review it, then add it as 'inherited' and disable it with "
            f"`gh api -X PUT repos/$REPO/actions/workflows/{name}/disable`."
        )

    # A missing entry is usually a deletion upstream, which is fine, but it is
    # reported so the allowlist does not quietly accumulate dead names.
    for name in missing:
        print(f"::warning::allowlist names {name}, which no longer exists; prune it")

    fork_owned = sorted(n for n, k in allowed.items() if k == "fork")
    print(f"fork-owned: {len(fork_owned)} — {', '.join(fork_owned)}")
    print(f"inherited:  {sum(1 for k in allowed.values() if k == 'inherited')}")

    if unreviewed:
        print(f"\n{len(unreviewed)} unreviewed workflow file(s): {', '.join(unreviewed)}")
        return 1

    print("\nevery workflow file is accounted for")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
