"""``trust_remote_code=True`` must never reach an attacker-controlled repo id.

``AutoTokenizer.from_pretrained(..., trust_remote_code=True)`` downloads and
*executes* a Python file shipped in the target HuggingFace repo. The proxy's
``/v1/chat/completions`` and other OpenAI-compatible endpoints are reachable
by any caller and accept an arbitrary ``model`` string in the request body
(see ``headroom/proxy/token_counting.py``). ``get_tokenizer_name()`` returns
the caller-supplied ``model`` string verbatim whenever it doesn't match a
curated entry in ``MODEL_TO_TOKENIZER``, and ``registry.py`` routes several
prefixes (``^codegen``, ``^qwen``, ``^deepseek``, ...) to the HuggingFace
backend with no guarantee the suffix is in the map. So a request such as
``{"model": "attacker/repo"}`` could previously reach
``AutoTokenizer.from_pretrained("attacker/repo", trust_remote_code=True)`` and
achieve remote code execution on the proxy host.

``trust_remote_code`` must only be True for the curated, developer-controlled
repo ids in ``MODEL_TO_TOKENIZER.values()``, never for a name derived from
untrusted request input.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from headroom.tokenizers.huggingface import (
    _TRUSTED_REMOTE_CODE_REPOS,
    MODEL_TO_TOKENIZER,
    _load_tokenizer,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    _load_tokenizer.cache_clear()
    yield
    _load_tokenizer.cache_clear()


def _install_fake_transformers(monkeypatch: pytest.MonkeyPatch, from_pretrained) -> None:
    fake = types.ModuleType("transformers")
    fake.AutoTokenizer = type(
        "AutoTokenizer", (), {"from_pretrained": staticmethod(from_pretrained)}
    )
    monkeypatch.setitem(sys.modules, "transformers", fake)


def test_allowlist_is_built_from_curated_mapping_values() -> None:
    """The allowlist must be exactly the developer-curated repo ids -- never
    something derived from caller input."""
    assert _TRUSTED_REMOTE_CODE_REPOS == frozenset(MODEL_TO_TOKENIZER.values())
    # Sanity: a known-good mapped repo id is present.
    assert "meta-llama/Meta-Llama-3-8B" in _TRUSTED_REMOTE_CODE_REPOS


def test_mapped_model_preserves_trust_remote_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing behavior for a curated, known-good repo id is unchanged."""
    calls: list[dict[str, Any]] = []

    def fake_from_pretrained(name: str, **kwargs: Any):
        calls.append(kwargs)
        return "tokenizer-for-" + name

    _install_fake_transformers(monkeypatch, fake_from_pretrained)

    result = _load_tokenizer("meta-llama/Meta-Llama-3-8B")
    assert result == "tokenizer-for-meta-llama/Meta-Llama-3-8B"
    assert calls[0]["trust_remote_code"] is True


def test_attacker_controlled_repo_never_gets_trust_remote_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arbitrary repo id (the fallback path for an unmapped ``model``)
    must load with trust_remote_code=False on both the cache-only and
    network attempts."""
    calls: list[dict[str, Any]] = []

    def fake_from_pretrained(name: str, **kwargs: Any):
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            raise OSError("not in cache")
        return "tokenizer-for-" + name

    _install_fake_transformers(monkeypatch, fake_from_pretrained)
    monkeypatch.setenv("HEADROOM_HF_TOKENIZER_LOAD_TIMEOUT_SECS", "5")

    result = _load_tokenizer("attacker/evil-repo")
    assert result == "tokenizer-for-attacker/evil-repo"
    assert len(calls) == 2
    assert calls[0]["trust_remote_code"] is False, "cache-only attempt must not trust remote code"
    assert calls[1]["trust_remote_code"] is False, "network attempt must not trust remote code"


def test_attacker_controlled_repo_needing_remote_code_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If an untrusted repo genuinely requires custom code to load, the
    (correctly) disabled trust_remote_code causes AutoTokenizer to raise --
    and the existing exception handling fails open to None (estimation),
    never executing the remote code."""

    def fake_from_pretrained(name: str, **kwargs: Any):
        if not kwargs.get("trust_remote_code"):
            raise ValueError(
                f"Loading {name} requires you to execute the configuring code "
                "in that repo on your local machine. Set trust_remote_code=True."
            )
        raise AssertionError("must never be called with trust_remote_code=True")

    _install_fake_transformers(monkeypatch, fake_from_pretrained)
    monkeypatch.setenv("HEADROOM_HF_TOKENIZER_LOAD_TIMEOUT_SECS", "5")

    assert _load_tokenizer("attacker/needs-custom-code") is None
