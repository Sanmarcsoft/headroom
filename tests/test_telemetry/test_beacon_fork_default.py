"""Fork control (#79): the anonymous beacon fails closed.

FORK_CHARTER.md promises no telemetry. Upstream ships the beacon on by
default; this fork only enables it on an explicit ``HEADROOM_BEACON=on``.
"""

from __future__ import annotations

import pytest

from headroom.telemetry import beacon

_KNOBS = ("HEADROOM_BEACON", "DO_NOT_TRACK", "HEADROOM_OFFLINE", "HEADROOM_TELEMETRY")


@pytest.fixture
def clean_env(monkeypatch):
    for k in _KNOBS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_beacon_default_constant_is_off():
    assert beacon.BEACON_DEFAULT_ON is False


def test_beacon_disabled_with_empty_environment(clean_env):
    assert beacon.is_beacon_enabled() is False


@pytest.mark.parametrize("value", ["on", "1", "true"])
def test_beacon_only_on_by_explicit_opt_in(clean_env, value):
    clean_env.setenv("HEADROOM_BEACON", value)
    assert beacon.is_beacon_enabled() is True


def test_do_not_track_still_wins_over_opt_in(clean_env):
    clean_env.setenv("HEADROOM_BEACON", "on")
    clean_env.setenv("DO_NOT_TRACK", "1")
    assert beacon.is_beacon_enabled() is False
