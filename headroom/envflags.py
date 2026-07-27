"""Strict parsing for boolean environment flags.

Truthiness checks on environment variables are a recurring security bug:
``os.environ.get("ALLOW_X")`` treats ``ALLOW_X=0``, ``=false``, ``=no`` and
``=off`` as ENABLED, because any non-empty string is truthy in Python. An
operator who types ``=0`` to harden a system gets the opposite of what they
asked for, and reviewers reading the call site see a flag name that reads
correctly.

Every gate that gets to weaken a security control uses :func:`env_flag_enabled`
so the semantics are identical everywhere and readable at a glance. This module
imports nothing beyond the standard library so it can be used from any layer.
"""

from __future__ import annotations

import os

# Deliberately narrow. Anything else, including "0", "false", "no" and "off",
# is not an affirmative answer.
TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag_enabled(name: str, environ: dict[str, str] | None = None) -> bool:
    """Return True only for an explicit affirmative value of env var ``name``.

    Unset, empty, whitespace, and every negative spelling return False.
    """
    source = os.environ if environ is None else environ
    return source.get(name, "").strip().lower() in TRUTHY
