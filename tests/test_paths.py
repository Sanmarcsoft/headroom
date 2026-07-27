"""Tests for ``headroom.paths`` -- canonical filesystem contract."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from headroom import paths

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Ensure every HEADROOM_* env var this module touches is unset."""

    for name in (
        paths.HEADROOM_CONFIG_DIR_ENV,
        paths.HEADROOM_WORKSPACE_DIR_ENV,
        paths.HEADROOM_SAVINGS_PATH_ENV,
        paths.HEADROOM_TOIN_PATH_ENV,
        paths.HEADROOM_SUBSCRIPTION_STATE_PATH_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def fake_home(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``Path.home()`` to ``tmp_path`` for isolation."""

    clean_env.setenv("HOME", str(tmp_path))
    # On Windows ``Path.home()`` reads ``USERPROFILE`` first, then ``HOME``.
    clean_env.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Canonical roots
# ---------------------------------------------------------------------------


def test_workspace_dir_default(fake_home: Path) -> None:
    assert paths.workspace_dir() == fake_home / ".headroom"


def test_workspace_dir_env_override(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "alt_ws"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(override))
    assert paths.workspace_dir() == override


def test_workspace_dir_tilde_expansion(fake_home: Path, clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, "~/custom")
    assert paths.workspace_dir() == fake_home / "custom"


def test_workspace_dir_blank_env_is_ignored(fake_home: Path, clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, "   ")
    assert paths.workspace_dir() == fake_home / ".headroom"


def test_config_dir_default(fake_home: Path) -> None:
    assert paths.config_dir() == fake_home / ".headroom" / "config"


def test_config_dir_follows_workspace_when_only_workspace_set(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "alt_ws"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(override))
    assert paths.config_dir() == override / "config"


def test_config_dir_explicit_env_overrides_workspace(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(tmp_path / "ws"))
    config_override = tmp_path / "cfg"
    clean_env.setenv(paths.HEADROOM_CONFIG_DIR_ENV, str(config_override))
    assert paths.config_dir() == config_override


def test_config_dir_tilde_expansion(fake_home: Path, clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(paths.HEADROOM_CONFIG_DIR_ENV, "~/cfg")
    assert paths.config_dir() == fake_home / "cfg"


# ---------------------------------------------------------------------------
# Ensure-* side effects
# ---------------------------------------------------------------------------


def test_workspace_dir_getter_no_mkdir(fake_home: Path) -> None:
    target = paths.workspace_dir()
    assert not target.exists()
    # Calling again must still not create it.
    assert not paths.workspace_dir().exists()


def test_config_dir_getter_no_mkdir(fake_home: Path) -> None:
    target = paths.config_dir()
    assert not target.exists()


def test_ensure_workspace_dir_creates(fake_home: Path) -> None:
    result = paths.ensure_workspace_dir()
    assert result.is_dir()
    assert result == fake_home / ".headroom"


def test_ensure_config_dir_creates(fake_home: Path) -> None:
    result = paths.ensure_config_dir()
    assert result.is_dir()
    assert result == fake_home / ".headroom" / "config"


# ---------------------------------------------------------------------------
# Directory permission hardening (FIX 4: ~/.headroom was observed at 0755 --
# world-traversable and filename-enumerable -- because mkdir(mode=) only
# applies at creation time and nothing ever chmod'd it afterwards).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_ensure_workspace_dir_creates_private(fake_home: Path) -> None:
    result = paths.ensure_workspace_dir()
    assert stat.S_IMODE(result.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_ensure_config_dir_creates_private(fake_home: Path) -> None:
    result = paths.ensure_config_dir()
    assert stat.S_IMODE(result.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_ensure_workspace_dir_repairs_preexisting_permissive_dir(fake_home: Path) -> None:
    """A directory created before this fix (e.g. plain ``mkdir()``) self-heals.

    ``Path.mkdir(mode=...)`` only applies the requested bits at creation
    time, so a directory that already exists at a looser mode would
    otherwise stay world-traversable forever even after upgrading to a
    version of Headroom with the hardened ``ensure_workspace_dir``.
    """
    target = fake_home / ".headroom"
    target.mkdir(mode=0o755)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755

    result = paths.ensure_workspace_dir()

    assert stat.S_IMODE(result.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_ensure_config_dir_repairs_preexisting_permissive_dir(fake_home: Path) -> None:
    target = fake_home / ".headroom" / "config"
    target.mkdir(mode=0o755, parents=True)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755

    result = paths.ensure_config_dir()

    assert stat.S_IMODE(result.stat().st_mode) == 0o700


# ---------------------------------------------------------------------------
# ``ensure_private_dir`` / ``touch_private_file`` / ``atomic_write_private``
# -- shared TOCTOU-safe primitives used by secret-bearing writers
# (copilot_auth.py, cache/backends/sqlite.py, memory/storage_router.py).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_ensure_private_dir_creates_at_mode(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir"
    result = paths.ensure_private_dir(target)
    assert result == target
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_ensure_private_dir_repairs_existing_dir(tmp_path: Path) -> None:
    target = tmp_path / "dir"
    target.mkdir(mode=0o755)
    paths.ensure_private_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_touch_private_file_creates_at_mode(tmp_path: Path) -> None:
    target = tmp_path / "secret.db"
    assert not target.exists()
    paths.touch_private_file(target)
    assert target.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_touch_private_file_is_idempotent_and_preserves_content(tmp_path: Path) -> None:
    """Must not truncate an existing file -- e.g. a live sqlite -wal file."""
    target = tmp_path / "secret.db"
    target.write_bytes(b"already-has-content")
    paths.touch_private_file(target)
    assert target.read_bytes() == b"already-has-content"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_touch_private_file_repairs_existing_permissive_file(tmp_path: Path) -> None:
    target = tmp_path / "secret.db"
    target.touch(mode=0o644)
    os.chmod(target, 0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644

    paths.touch_private_file(target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_touch_private_file_never_briefly_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file must exist at 0600 from the very ``os.open`` that creates it.

    Regression guard: an implementation that creates the file first and
    chmods afterward reintroduces the TOCTOU window this helper exists to
    close. We assert the mode by patching ``os.chmod`` to a no-op and
    checking the *creation* mode (as applied by ``os.open``) is already
    correct once umask is accounted for.
    """
    target = tmp_path / "secret.db"
    old_umask = os.umask(0o022)
    try:
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        paths.touch_private_file(target)
    finally:
        os.umask(old_umask)
    # Even with the explicit chmod call stubbed out to a no-op, os.open's
    # own mode argument (0o600) combined with umask 0o022 must already
    # yield 0o600 (0o022 doesn't clear any bits 0o600 sets).
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_private_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "secret.json"
    paths.atomic_write_private(target, '{"refresh": "gho_xxx"}')
    assert target.read_text(encoding="utf-8") == '{"refresh": "gho_xxx"}'


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_atomic_write_private_creates_at_0600(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    paths.atomic_write_private(target, "data")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_atomic_write_private_parent_dir_is_private(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "secret.json"
    paths.atomic_write_private(target, "data")
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
def test_atomic_write_private_overwrite_stays_0600(tmp_path: Path) -> None:
    """Rewriting an existing secret must never leave it world-readable."""
    target = tmp_path / "secret.json"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)

    paths.atomic_write_private(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_private_no_leftover_tmp_file_on_success(tmp_path: Path) -> None:
    paths.atomic_write_private(tmp_path / "secret.json", "data")
    leftovers = [p for p in tmp_path.iterdir() if p.name != "secret.json"]
    assert leftovers == []


def test_per_resource_getters_no_mkdir(fake_home: Path) -> None:
    # None of these should trigger directory creation.
    paths.savings_path()
    paths.toin_path()
    paths.subscription_state_path()
    paths.memory_db_path()
    paths.native_memory_dir()
    paths.license_cache_path()
    paths.session_stats_path()
    paths.sync_state_path()
    paths.bridge_state_path()
    paths.log_dir()
    paths.proxy_log_path()
    paths.debug_400_dir()
    paths.bin_dir()
    paths.rtk_path()
    paths.deploy_root()
    paths.beacon_lock_path(8787)
    paths.models_config_path()
    paths.plugin_config_dir("example")
    paths.plugin_workspace_dir("example")
    assert not (fake_home / ".headroom").exists()


# ---------------------------------------------------------------------------
# Per-resource precedence matrix
# ---------------------------------------------------------------------------


RESOURCES_WITH_LEGACY_ENV = [
    pytest.param(
        "savings_path",
        paths.HEADROOM_SAVINGS_PATH_ENV,
        "proxy_savings.json",
        id="savings",
    ),
    pytest.param(
        "toin_path",
        paths.HEADROOM_TOIN_PATH_ENV,
        "toin.json",
        id="toin",
    ),
    pytest.param(
        "subscription_state_path",
        paths.HEADROOM_SUBSCRIPTION_STATE_PATH_ENV,
        "subscription_state.json",
        id="subscription",
    ),
]


@pytest.mark.parametrize("fn_name,env_var,filename", RESOURCES_WITH_LEGACY_ENV)
def test_resource_default_under_home(
    fake_home: Path, fn_name: str, env_var: str, filename: str
) -> None:
    fn = getattr(paths, fn_name)
    assert fn() == fake_home / ".headroom" / filename


@pytest.mark.parametrize("fn_name,env_var,filename", RESOURCES_WITH_LEGACY_ENV)
def test_resource_derived_from_workspace_env(
    fake_home: Path,
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    fn_name: str,
    env_var: str,
    filename: str,
) -> None:
    ws = tmp_path / "state"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(ws))
    fn = getattr(paths, fn_name)
    assert fn() == ws / filename


@pytest.mark.parametrize("fn_name,env_var,filename", RESOURCES_WITH_LEGACY_ENV)
def test_resource_legacy_env_wins_over_workspace(
    fake_home: Path,
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    fn_name: str,
    env_var: str,
    filename: str,
) -> None:
    ws = tmp_path / "state"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(ws))
    legacy = tmp_path / "legacy_custom.json"
    clean_env.setenv(env_var, str(legacy))
    fn = getattr(paths, fn_name)
    # Legacy per-resource env var wins. Backward compatibility is preserved.
    assert fn() == legacy


@pytest.mark.parametrize("fn_name,env_var,filename", RESOURCES_WITH_LEGACY_ENV)
def test_resource_explicit_arg_wins(
    fake_home: Path,
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    fn_name: str,
    env_var: str,
    filename: str,
) -> None:
    ws = tmp_path / "state"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(ws))
    legacy = tmp_path / "legacy_custom.json"
    clean_env.setenv(env_var, str(legacy))
    explicit = tmp_path / "explicit.json"
    fn = getattr(paths, fn_name)
    assert fn(str(explicit)) == explicit


@pytest.mark.parametrize("fn_name,env_var,filename", RESOURCES_WITH_LEGACY_ENV)
def test_resource_legacy_env_tilde_expansion(
    fake_home: Path,
    clean_env: pytest.MonkeyPatch,
    fn_name: str,
    env_var: str,
    filename: str,
) -> None:
    clean_env.setenv(env_var, "~/foo.json")
    fn = getattr(paths, fn_name)
    assert fn() == fake_home / "foo.json"


@pytest.mark.parametrize("fn_name,env_var,filename", RESOURCES_WITH_LEGACY_ENV)
def test_resource_explicit_none_falls_through(
    fake_home: Path,
    clean_env: pytest.MonkeyPatch,
    fn_name: str,
    env_var: str,
    filename: str,
) -> None:
    fn = getattr(paths, fn_name)
    assert fn(None) == fake_home / ".headroom" / filename


@pytest.mark.parametrize("fn_name,env_var,filename", RESOURCES_WITH_LEGACY_ENV)
def test_resource_explicit_empty_string_falls_through(
    fake_home: Path,
    clean_env: pytest.MonkeyPatch,
    fn_name: str,
    env_var: str,
    filename: str,
) -> None:
    fn = getattr(paths, fn_name)
    assert fn("") == fake_home / ".headroom" / filename


# ---------------------------------------------------------------------------
# Resources without a legacy env var (derived-only from canonical roots)
# ---------------------------------------------------------------------------


def test_memory_db_path_default(fake_home: Path) -> None:
    assert paths.memory_db_path() == fake_home / ".headroom" / "memory.db"


def test_memory_db_path_follows_workspace_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(tmp_path / "ws"))
    assert paths.memory_db_path() == tmp_path / "ws" / "memory.db"


def test_native_memory_dir_default(fake_home: Path) -> None:
    assert paths.native_memory_dir() == fake_home / ".headroom" / "memories"


def test_license_cache_path_default(fake_home: Path) -> None:
    assert paths.license_cache_path() == fake_home / ".headroom" / "license_cache.json"


def test_session_stats_path_default(fake_home: Path) -> None:
    assert paths.session_stats_path() == fake_home / ".headroom" / "session_stats.jsonl"


def test_sync_state_path_default(fake_home: Path) -> None:
    assert paths.sync_state_path() == fake_home / ".headroom" / "sync_state.json"


def test_bridge_state_path_default(fake_home: Path) -> None:
    assert paths.bridge_state_path() == fake_home / ".headroom" / "bridge_state.json"


def test_log_dir_default(fake_home: Path) -> None:
    assert paths.log_dir() == fake_home / ".headroom" / "logs"


def test_proxy_log_path_default(fake_home: Path) -> None:
    assert paths.proxy_log_path() == fake_home / ".headroom" / "logs" / "proxy.log"


def test_debug_400_dir_default(fake_home: Path) -> None:
    assert paths.debug_400_dir() == fake_home / ".headroom" / "logs" / "debug_400"


def test_bin_dir_default(fake_home: Path) -> None:
    assert paths.bin_dir() == fake_home / ".headroom" / "bin"


def test_rtk_path_suffix(fake_home: Path) -> None:
    expected_name = "rtk.exe" if os.name == "nt" else "rtk"
    assert paths.rtk_path().name == expected_name
    assert paths.rtk_path().parent == paths.bin_dir()


def test_lean_ctx_path_suffix(fake_home: Path) -> None:
    expected_name = "lean-ctx.exe" if os.name == "nt" else "lean-ctx"
    assert paths.lean_ctx_path().name == expected_name
    assert paths.lean_ctx_path().parent == paths.bin_dir()


def test_deploy_root_default(fake_home: Path) -> None:
    assert paths.deploy_root() == fake_home / ".headroom" / "deploy"


def test_beacon_lock_path_includes_port(fake_home: Path) -> None:
    assert paths.beacon_lock_path(8787) == fake_home / ".headroom" / ".beacon_lock_8787"


# Every derived-only helper must also honor HEADROOM_WORKSPACE_DIR overrides so
# that a single env var relocates the whole workspace bucket. One row per
# helper, each asserting the override flows through end-to-end.
DERIVED_WORKSPACE_HELPERS = [
    pytest.param("native_memory_dir", "memories", id="native_memory_dir"),
    pytest.param("license_cache_path", "license_cache.json", id="license_cache_path"),
    pytest.param("session_stats_path", "session_stats.jsonl", id="session_stats_path"),
    pytest.param("sync_state_path", "sync_state.json", id="sync_state_path"),
    pytest.param("bridge_state_path", "bridge_state.json", id="bridge_state_path"),
    pytest.param("log_dir", "logs", id="log_dir"),
    pytest.param("debug_400_dir", "logs/debug_400", id="debug_400_dir"),
    pytest.param("bin_dir", "bin", id="bin_dir"),
    pytest.param("deploy_root", "deploy", id="deploy_root"),
]


@pytest.mark.parametrize("fn_name,rel", DERIVED_WORKSPACE_HELPERS)
def test_derived_helper_follows_workspace_env(
    fake_home: Path,
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    fn_name: str,
    rel: str,
) -> None:
    ws = tmp_path / "state"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(ws))
    fn = getattr(paths, fn_name)
    expected = ws
    for part in rel.split("/"):
        expected = expected / part
    assert fn() == expected


def test_proxy_log_path_follows_workspace_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "state"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(ws))
    assert paths.proxy_log_path() == ws / "logs" / "proxy.log"


def test_rtk_path_follows_workspace_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "state"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(ws))
    expected_name = "rtk.exe" if os.name == "nt" else "rtk"
    assert paths.rtk_path() == ws / "bin" / expected_name


def test_lean_ctx_path_follows_workspace_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "state"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(ws))
    expected_name = "lean-ctx.exe" if os.name == "nt" else "lean-ctx"
    assert paths.lean_ctx_path() == ws / "bin" / expected_name


def test_beacon_lock_path_follows_workspace_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "state"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(ws))
    assert paths.beacon_lock_path(9999) == ws / ".beacon_lock_9999"


def test_ensure_workspace_dir_follows_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(ws))
    result = paths.ensure_workspace_dir()
    assert result == ws
    assert result.is_dir()


def test_ensure_config_dir_follows_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "cfg"
    clean_env.setenv(paths.HEADROOM_CONFIG_DIR_ENV, str(cfg))
    result = paths.ensure_config_dir()
    assert result == cfg
    assert result.is_dir()


# ---------------------------------------------------------------------------
# Config bucket
# ---------------------------------------------------------------------------


def test_models_config_path_default(fake_home: Path) -> None:
    assert paths.models_config_path() == fake_home / ".headroom" / "config" / "models.json"


def test_models_config_path_follows_config_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setenv(paths.HEADROOM_CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    assert paths.models_config_path() == tmp_path / "cfg" / "models.json"


def test_models_config_path_follows_workspace_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(tmp_path / "ws"))
    assert paths.models_config_path() == tmp_path / "ws" / "config" / "models.json"


# ---------------------------------------------------------------------------
# Plugin namespace isolation
# ---------------------------------------------------------------------------


def test_plugin_config_dir_namespaced(fake_home: Path) -> None:
    a = paths.plugin_config_dir("alpha")
    b = paths.plugin_config_dir("beta")
    assert a != b
    assert a == fake_home / ".headroom" / "config" / "plugins" / "alpha"
    assert b == fake_home / ".headroom" / "config" / "plugins" / "beta"


def test_plugin_workspace_dir_namespaced(fake_home: Path) -> None:
    a = paths.plugin_workspace_dir("alpha")
    b = paths.plugin_workspace_dir("beta")
    assert a != b
    assert a == fake_home / ".headroom" / "plugins" / "alpha"
    assert b == fake_home / ".headroom" / "plugins" / "beta"


@pytest.mark.parametrize("bad_name", ["", "foo/bar", "foo\\bar", "..", ".", "\x00"])
def test_plugin_dirs_reject_bad_names(fake_home: Path, bad_name: str) -> None:
    with pytest.raises(ValueError):
        paths.plugin_config_dir(bad_name)
    with pytest.raises(ValueError):
        paths.plugin_workspace_dir(bad_name)


def test_plugin_dirs_reject_traversal_escapes_sandbox(fake_home: Path) -> None:
    """A plugin name of ``..`` must not resolve outside ``plugins/``.

    Without this guard, ``plugin_config_dir("..") == config_dir()`` — a plugin
    can read/write the entire config root (models catalog, other plugins'
    settings), and ``plugin_workspace_dir("..") == workspace_dir()`` exposes
    the license cache, savings ledger, memory DB, and logs.
    """

    for name in ("..", "."):
        with pytest.raises(ValueError):
            paths.plugin_config_dir(name)
        with pytest.raises(ValueError):
            paths.plugin_workspace_dir(name)


def test_plugin_config_dir_follows_config_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setenv(paths.HEADROOM_CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    assert paths.plugin_config_dir("alpha") == tmp_path / "cfg" / "plugins" / "alpha"


def test_plugin_workspace_dir_follows_workspace_env(
    fake_home: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setenv(paths.HEADROOM_WORKSPACE_DIR_ENV, str(tmp_path / "ws"))
    assert paths.plugin_workspace_dir("alpha") == tmp_path / "ws" / "plugins" / "alpha"


# ---------------------------------------------------------------------------
# Returns Path, not str
# ---------------------------------------------------------------------------


def test_all_helpers_return_path(fake_home: Path) -> None:
    assert isinstance(paths.workspace_dir(), Path)
    assert isinstance(paths.config_dir(), Path)
    assert isinstance(paths.savings_path(), Path)
    assert isinstance(paths.toin_path(), Path)
    assert isinstance(paths.subscription_state_path(), Path)
    assert isinstance(paths.memory_db_path(), Path)
    assert isinstance(paths.models_config_path(), Path)
    assert isinstance(paths.plugin_config_dir("x"), Path)
    assert isinstance(paths.plugin_workspace_dir("x"), Path)
