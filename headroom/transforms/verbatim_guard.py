"""Detect content that must never be lossily compressed.

Lossy word-level compression (kompress) drops function words. On prose that
is a readable summary, which is the product working. On source code it drops
*keywords*, which is silent corruption. Observed live in this proxy::

    if config.exclude_tools:        ->        config.exclude_tools:

An agent reading code through the proxy and then editing it writes wrong
code, and nothing in the transcript says the code was altered. The same
applies to doctrine: an instruction file that loses "Never" or "MUST"
inverts its own meaning.

Classification is by *content*, not by tool. Tool identity is the wrong axis
because ``Bash`` is polymorphic: ``pytest`` output should compress hard and
``sed -n '1,200p' server.py`` should not compress lossily at all. Keying on
the tool name would either blanket-protect logs (destroying the ratio that
justifies the proxy) or leave every shell-read file exposed. Keying on the
payload gets both right.

The guard is deliberately asymmetric, and the asymmetry is the whole design:

* A **false positive** costs compression ratio on one block.
* A **false negative** hands the caller corrupted code or inverted
  instructions, silently, and the damage propagates into whatever they write
  next.

So signals are OR-composed. Any single signal trips protection, a small
amount of code inside a large amount of prose trips protection, and content
we cannot inspect at all trips protection. We buy fidelity with ratio,
never the reverse.

The one deliberate exception is bulk record data: JSON arrays, concatenated
object streams, and oversized payloads stay compressible even though they are
structured. Nobody edits a query result the way they edit a config file, so
the false-negative cost that justifies the asymmetry is absent, while the
ratio cost is the largest in the product. Config is therefore recognised by
parsing to a single mapping, not by looking structured.
"""

from __future__ import annotations

import json
import re
from enum import Enum

__all__ = ["VerbatimReason", "classify_verbatim", "is_verbatim_critical"]


class VerbatimReason(Enum):
    """Why a block is verbatim-critical. Ordered by evaluation precedence."""

    PATCH = "patch"
    SOURCE_CODE = "source_code"
    STRUCTURED_CONFIG = "structured_config"
    INSTRUCTION_TEXT = "instruction_text"


# A unified diff is unusable if a single character shifts, and it is cheap to
# recognise, so it is checked first.
_PATCH_RE = re.compile(r"(?m)^(?:@@ -\d|\+\+\+ [ab]/|--- [ab]/|diff --git )")

# Code signals. Each is individually sufficient. These target *syntax*, not
# vocabulary, so English prose discussing a "class of problems" or "a function
# of time" does not trip them.
_CODE_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?m)^#!"),  # shebang
    re.compile(r"(?m)^\s*(?:def|class)\s+\w+\s*[(:]"),  # python
    re.compile(r"(?m)^\s*(?:async\s+)?(?:function|fn|func)\s+\w+\s*[(<]"),
    re.compile(r"(?m)^\s*(?:import|from|use|package|#include)\s+[\w.{/<\"]"),
    re.compile(r"(?m)^\s*(?:export|public|private|protected)\s+\w"),
    re.compile(r"(?m)^\s*(?:impl|trait|struct|enum|interface|type)\s+\w+"),
    re.compile(r"(?m)^\s*(?:if|for|while|match|switch|elif|else)\b.*[:{]\s*$"),
    re.compile(r"(?m)^\s*(?:return|yield|await|throw)\s+\S"),
    re.compile(r"(?m)^\s*(?:const|let|var)\s+\w+\s*[:=]"),
    re.compile(r"(?m)^\s*@\w+"),  # decorator / annotation
    re.compile(r"(?m)^\s*(?:set -euo|set -e)\b"),  # shell strict mode
    re.compile(r"```[a-zA-Z+#]*\n"),  # fenced code block
    re.compile(r"\w+\s*=\s*\$\((?:[^)]|\n)*\)"),  # shell command substitution
)

# Structured config that must round-trip exactly. Requires several sibling
# keys so a stray "word: word" line in prose does not trip it.
_YAML_KEY_RE = re.compile(r"(?m)^\s*[\w.-]+:(?:\s|$)")
_TOML_KEY_RE = re.compile(r"(?m)^\s*(?:\[[\w.\"-]+\]|[\w.-]+\s*=\s*\S)")

# JSON config is recognised by *parsing*, not by pattern. A regex anchored on
# a leading `{` and a trailing `}` cannot distinguish one object from a
# whitespace-joined stream of them, and search-result streams look exactly like
# that. Only a document that parses to a single mapping counts as config; a
# top-level array, or a concatenation that fails to parse at all, is a record
# set — the largest payload class the proxy exists to shrink, already handled
# structurally by the tabular and SmartCrusher paths.
#
# Config files are small by nature. The cap keeps the parse off the hot path
# for bulk payloads, where a `{` start means "data dump" rather than "config".
_JSON_PARSE_MAX_BYTES = 256 * 1024

# Instruction text. Two families: structural fingerprints of instruction
# files, and imperative-directive density in prose.
_INSTRUCTION_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<system-reminder>", re.I),
    re.compile(r"(?s)\A\s*---\r?\n.*?^\s*(?:name|description|title):", re.M),
    re.compile(r"(?m)^\s*#+\s*(?:Operational Rules|Global Guardrails|Rules|Doctrine)\b", re.I),
    re.compile(r"\b(?:CLAUDE|AGENTS|SKILL)\.md\b"),
)

# Directives that invert meaning when a single word is dropped.
_DIRECTIVE_RE = re.compile(
    r"(?<![\w-])(?:MUST NOT|MUST|NEVER|ALWAYS|MANDATORY|REQUIRED|SHALL NOT|SHALL|"
    r"DO NOT|Zero exceptions|non-negotiable)(?![\w-])"
)

# Two directives is enough. Instruction files repeat them; ordinary prose that
# happens to shout once does not.
_DIRECTIVE_THRESHOLD = 2

# Enough sibling keys to look like a config document rather than a stray colon.
_CONFIG_KEY_THRESHOLD = 4


def _looks_like_source_code(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CODE_SIGNALS)


def _is_json_object_document(text: str) -> bool:
    """True when the whole document parses to a single JSON mapping."""

    stripped = text.lstrip()
    if not stripped.startswith("{") or len(text) > _JSON_PARSE_MAX_BYTES:
        return False
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _looks_like_structured_config(text: str) -> bool:
    if _is_json_object_document(text):
        return True
    if len(_YAML_KEY_RE.findall(text)) >= _CONFIG_KEY_THRESHOLD:
        return True
    return len(_TOML_KEY_RE.findall(text)) >= _CONFIG_KEY_THRESHOLD


def _has_instruction_fingerprint(text: str) -> bool:
    """Structural marks of an instruction file (frontmatter, system-reminder, ...)."""

    return any(pattern.search(text) for pattern in _INSTRUCTION_SIGNALS)


def _is_directive_dense(text: str) -> bool:
    """Prose carrying enough hard directives that dropping one inverts meaning."""

    return len(_DIRECTIVE_RE.findall(text)) >= _DIRECTIVE_THRESHOLD


def classify_verbatim(text: object) -> VerbatimReason | None:
    """Return why ``text`` must be preserved byte-exact, or None if it may compress.

    Anything that is not a non-empty string is reported as
    :attr:`VerbatimReason.SOURCE_CODE`: we cannot inspect it, so we cannot
    clear it. Empty and whitespace-only strings are the one exception, since
    there is nothing there to corrupt and protecting them would only cost
    ratio.
    """

    if not isinstance(text, str):
        return VerbatimReason.SOURCE_CODE
    if not text.strip():
        return None

    if _PATCH_RE.search(text):
        return VerbatimReason.PATCH
    if _looks_like_source_code(text):
        return VerbatimReason.SOURCE_CODE
    # Instruction *fingerprints* outrank the config heuristic: a doc with YAML
    # frontmatter reads as config by key count, but it is an instruction file
    # and the reason should say so.
    if _has_instruction_fingerprint(text):
        return VerbatimReason.INSTRUCTION_TEXT
    if _looks_like_structured_config(text):
        return VerbatimReason.STRUCTURED_CONFIG
    if _is_directive_dense(text):
        return VerbatimReason.INSTRUCTION_TEXT
    return None


def is_verbatim_critical(text: object) -> bool:
    """True when ``text`` must not be handed to a lossy compressor."""

    return classify_verbatim(text) is not None
