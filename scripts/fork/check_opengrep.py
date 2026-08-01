#!/usr/bin/env python3
"""Gate the OpenGrep scan against ci/sast-baseline.txt.

This is the replacement for CodeQL, which had to go when the repository became
private: code scanning needs GitHub Advanced Security and the free plan does not
have it. The findings CodeQL held are snapshotted in ci/codeql-snapshot/, and
this check was measured against them rather than against nothing.

Why OpenGrep and not Semgrep. Semgrep relicensed its rule set, so
semgrep/semgrep-rules now ships under the proprietary "Semgrep Rules License
v1.0". opengrep/opengrep-rules is the fork taken before that change and stays on
LGPL-2.1 plus the Commons Clause, which forbids *selling* a service whose value
derives from the rules. Scanning our own private repositories is not selling, so
the fork is usable here and current upstream is not.

Three tiers, because a scanner that reports everything reports nothing:

  security/vuln   blocking. Something is wrong and someone can act on it.
  security/audit  reported, never blocking. "A human should look at this call",
                  which on a tool whose whole job is wrapping subprocesses and
                  driving sqlite means several dozen entirely correct hits.
  everything else dropped. The rule set carries 201 maintainability, 63
                  best-practice and 33 correctness findings on this tree, and
                  this repository already runs ruff, mypy and clippy. A SAST
                  gate that also argues about style is one people learn to skip.

The tier comes from each rule's own `metadata.category` / `metadata.subcategory`
rather than from the directory the rule file sits in, because the metadata is the
part rule authors actually maintain.

Why the baseline counts per rule instead of per finding. This is a fork of a
repository that lands roughly 20 commits a day, and almost every finding is in
code we do not own. Pinning each one by fingerprint or by file:line would
repaint the baseline on every sync, and a file that churns is a file nobody
reads. A per-rule ceiling still fails the build when somebody introduces the
twelfth md5 call, which is the case worth catching, and it survives upstream
moving code between files, which is not. The tradeoff it accepts, deliberately:
removing one finding and adding another under the same rule nets to zero and
passes.

Scan errors are findings in their own right. A file OpenGrep cannot parse is a
file OpenGrep is not scanning, and a rule that times out is coverage that
silently did not happen. Reporting a clean run on top of either one is exactly
the sort of proxy signal this repository's CI is written to refuse.
"""

from __future__ import annotations

import collections
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "ci" / "sast-baseline.txt"

# Baseline keys for the two error classes, so a new blind spot has to be
# acknowledged in a diff the same way a new finding does.
ERR_PARSE = "_scan-error:parse"
ERR_TIMEOUT = "_scan-error:timeout"


def load_baseline() -> dict[str, tuple[int, str]]:
    """Map rule id -> (allowed count, reason). An entry without a reason is an error."""
    if not BASELINE.exists():
        return {}
    entries: dict[str, tuple[int, str]] = {}
    for lineno, raw in enumerate(BASELINE.read_text().splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        body, _, reason = raw.partition("#")
        reason = reason.strip()
        parts = body.split()
        if len(parts) != 2:
            print(
                f"::error::{BASELINE.name} line {lineno}: expected '<rule> <count>', "
                f"got {body.strip()!r}"
            )
            sys.exit(1)
        ident, count = parts
        if not count.isdigit():
            print(
                f"::error::{BASELINE.name} line {lineno}: {ident} count {count!r} is not a number."
            )
            sys.exit(1)
        if not reason:
            print(f"::error::{BASELINE.name} line {lineno}: {ident} has no reason.")
            sys.exit(1)
        entries[ident] = (int(count), reason)
    return entries


def short_rule(check_id: str) -> str:
    """Trim the config path OpenGrep prefixes onto every rule id.

    Pointing --config at a directory makes each id carry that directory, so one
    rule is `tmp.ogrules.python...foo` locally and `rules.python...foo` in CI.
    The baseline has to key on something that does not depend on where the rules
    were unpacked, so keep the last segment.
    """
    return check_id.split(".")[-1]


def classify(finding: dict) -> str:
    meta = finding.get("extra", {}).get("metadata", {}) or {}
    sub = meta.get("subcategory") or []
    if isinstance(sub, str):
        sub = [sub]
    if meta.get("category") != "security":
        return "other"
    return "vuln" if "vuln" in sub else "audit"


def render_summary(tiers, blocking, baseline, errors) -> str:
    lines = ["### Source SAST (OpenGrep)", ""]
    lines += ["| rule | found | allowed | status |", "|---|---|---|---|"]
    for rule, count in sorted(blocking.items()):
        allowed, _ = baseline.get(rule, (0, ""))
        status = "ok" if count <= allowed else "**REGRESSION**"
        lines.append(f"| `{rule}` | {count} | {allowed} | {status} |")

    audit = collections.Counter(short_rule(f["check_id"]) for f in tiers["audit"])
    if audit:
        lines += [
            "",
            f"<details><summary>security/audit, not blocking ({sum(audit.values())})</summary>",
            "",
            "| rule | hits |",
            "|---|---|",
        ]
        lines += [f"| `{rule}` | {n} |" for rule, n in audit.most_common()]
        lines += ["", "</details>"]

    if errors:
        lines += ["", "**Files this scan did not fully cover**", ""]
        lines += [f"- `{e.get('path', '?')}` ({e.get('type', 'error')})" for e in errors]

    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(__file__).name} <opengrep-json>", file=sys.stderr)
        return 2

    data = json.loads(Path(sys.argv[1]).read_text())

    tiers: dict[str, list[dict]] = collections.defaultdict(list)
    for finding in data.get("results", []):
        tiers[classify(finding)].append(finding)

    blocking = collections.Counter(short_rule(f["check_id"]) for f in tiers["vuln"])

    # Errors count toward two synthetic rule ids so they obey the same ceiling.
    errors = data.get("errors", [])
    for err in errors:
        blocking[ERR_TIMEOUT if err.get("type") == "Timeout" else ERR_PARSE] += 1

    baseline = load_baseline()
    scanned = len(data.get("paths", {}).get("scanned", []))

    print(
        f"scanned {scanned} files | "
        f"blocking(security/vuln) {len(tiers['vuln'])} | "
        f"reported(security/audit) {len(tiers['audit'])} | "
        f"dropped(non-security) {len(tiers['other'])} | "
        f"scan errors {len(errors)}"
    )

    regressions = [
        f"{rule}: {count} found, {baseline.get(rule, (0, ''))[0]} allowed by {BASELINE.name}"
        for rule, count in sorted(blocking.items())
        if count > baseline.get(rule, (0, ""))[0]
    ]

    # A count that dropped means somebody fixed something. Say so, do not fail:
    # a check that goes red on good news is a check that gets switched off.
    for rule, (allowed, _) in sorted(baseline.items()):
        found = blocking.get(rule, 0)
        if found < allowed:
            print(
                f"::notice::{rule} is down to {found} (baseline allows {allowed}). "
                f"Lower it in {BASELINE.name}."
            )

    # Say what actually went wrong, not just which of two buckets it fell into.
    # Everything that is not a Timeout used to be reported as "could not be
    # parsed", which is a guess: an out-of-memory kill and a genuine syntax
    # failure produced the same sentence. That cost real time on
    # code_compressor.py, where the useful detail turned out to be OpenGrep's
    # own OCaml exception ("Failure: int_of_string") and not anything about our
    # source at all. Carry the type and the tool's message through so the next
    # person can act on the annotation instead of re-deriving it.
    for err in errors:
        kind = "timed out" if err.get("type") == "Timeout" else "could not be parsed"
        path = err.get("path", "?")
        detail = err.get("long_msg") or err.get("message") or err.get("type") or ""
        detail = " ".join(str(detail).split())
        suffix = f" ({detail})" if detail else ""
        print(
            f"::warning file={path}::{path} {kind}{suffix}; it is not fully covered by this scan."
        )

    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a") as handle:
            handle.write(render_summary(tiers, blocking, baseline, errors) + "\n")

    for line in regressions:
        print(f"::error::{line}")
    if regressions:
        print(
            f"\nFix the finding, or raise the count in {BASELINE.name} with a reason "
            f"explaining why it is acceptable here."
        )

    return 1 if regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
