"""Regression checks for the OrbStack sidecar deployment.

These settings lived only in the host's working tree for a month, so a
`git checkout` on the deploy host would have silently reverted them and
reintroduced the 2026-08-12 prefix-cache thrash. They are in git now; these
tests keep them there.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "orbstack-sidecar" / "docker-compose.yml"


def _service() -> dict:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return compose["services"]["headroom"]


def test_sidecar_disables_prompt_rewriting() -> None:
    """--no-optimize is load-bearing, not a leftover.

    With rewriting on, Headroom re-compressed the conversation mid-turn and
    collapsed the provider prefix cache to the system-prompt boundary. The
    compression was worth 1.6% of spend; the cache it destroyed was worth
    roughly eighty times that.
    """
    assert "--no-optimize" in _service()["command"]


def test_sidecar_enables_persistent_memory() -> None:
    assert "--memory" in _service()["command"]


def test_sidecar_pins_memory_db_to_the_named_volume() -> None:
    """Without this the DB defaults under WORKDIR and lands on an anonymous
    volume that `down` + `up` orphans, discarding everything agents saved."""
    env = _service()["environment"]
    assert env["HEADROOM_MEMORY_DB_PATH"] == "/data/.headroom/memory.db"
    assert env["HEADROOM_WORKSPACE_DIR"] == "/data/.headroom"
    assert env["HOME"] == "/data"
    assert "headroom-state:/data" in _service()["volumes"]


def test_sidecar_memory_default_never_touches_the_cache_hot_zone() -> None:
    """The compose file relies on the *default* injection mode being tail
    injection. If upstream ever changes that default, --memory silently starts
    mutating the cache hot zone again, which is the failure --no-optimize
    exists to prevent. Assert the default rather than trusting the comment."""
    import dataclasses

    from headroom.proxy.memory_handler import MemoryMode
    from headroom.proxy.models import ProxyConfig

    assert MemoryMode.AUTO_TAIL.value == "auto_tail"
    fields = {f.name: f for f in dataclasses.fields(ProxyConfig)}
    assert fields["memory_mode"].default == "auto_tail"
