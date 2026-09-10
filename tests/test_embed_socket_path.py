"""Unit tests for embedding server default Unix socket path and permission hardening."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from headroom.cli import main
from headroom.cli.proxy import default_embed_socket_path


def test_default_with_xdg_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With XDG_RUNTIME_DIR set, default is <xdg>/headroom/embed-<port>.sock with mode 0o700."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    socket_path = default_embed_socket_path(8787)

    expected_dir = tmp_path / "headroom"
    expected_path = expected_dir / "embed-8787.sock"
    assert socket_path == str(expected_path)
    assert expected_dir.is_dir()
    mode = os.stat(expected_dir).st_mode & 0o777
    assert mode == 0o700


def test_default_without_xdg_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without XDG_RUNTIME_DIR (or when empty), default uses <tempdir>/headroom-<uid>/embed-<port>.sock."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)

    uid = os.getuid()
    socket_path = default_embed_socket_path(9000)

    expected_dir = tmp_path / f"headroom-{uid}"
    expected_path = expected_dir / "embed-9000.sock"
    assert socket_path == str(expected_path)
    assert expected_dir.is_dir()
    mode = os.stat(expected_dir).st_mode & 0o777
    assert mode == 0o700

    # Also test empty string XDG_RUNTIME_DIR=""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    socket_path_empty_xdg = default_embed_socket_path(9001)
    assert socket_path_empty_xdg == str(expected_dir / "embed-9001.sock")


def test_preexisting_group_or_world_writable_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing directory that is group- or world-writable raises a ClickException."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    socket_dir = tmp_path / "headroom"
    socket_dir.mkdir(mode=0o777)
    socket_dir.chmod(0o777)

    with pytest.raises(click.ClickException) as exc_info:
        default_embed_socket_path(8787)

    err = str(exc_info.value)
    assert str(socket_dir) in err
    assert "insecure permissions" in err.lower()

    # Also test group-writable only (0o770)
    socket_dir.chmod(0o770)
    with pytest.raises(click.ClickException) as exc_info:
        default_embed_socket_path(8787)
    assert "insecure permissions" in str(exc_info.value).lower()

    # Also test world-writable only (0o702)
    socket_dir.chmod(0o702)
    with pytest.raises(click.ClickException) as exc_info:
        default_embed_socket_path(8787)
    assert "insecure permissions" in str(exc_info.value).lower()


def test_preexisting_owned_by_another_uid_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing directory owned by another UID raises a ClickException."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    socket_dir = tmp_path / "headroom"
    socket_dir.mkdir(mode=0o700)
    socket_dir.chmod(0o700)

    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 999)

    with pytest.raises(click.ClickException) as exc_info:
        default_embed_socket_path(8787)

    err = str(exc_info.value)
    assert str(socket_dir) in err
    assert "owned by uid" in err.lower()


def test_symlink_directory_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A symlink at the directory path raises a ClickException."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    real_dir = tmp_path / "actual_dir"
    real_dir.mkdir(mode=0o700)
    real_dir.chmod(0o700)

    symlink_dir = tmp_path / "headroom"
    symlink_dir.symlink_to(real_dir)

    with pytest.raises(click.ClickException) as exc_info:
        default_embed_socket_path(8787)

    err = str(exc_info.value)
    assert str(symlink_dir) in err
    assert "symlink" in err.lower()


def test_broken_symlink_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken symlink at the directory path raises a ClickException."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    symlink_dir = tmp_path / "headroom"
    symlink_dir.symlink_to(tmp_path / "nonexistent_dir")

    with pytest.raises(click.ClickException) as exc_info:
        default_embed_socket_path(8787)

    err = str(exc_info.value)
    assert str(symlink_dir) in err
    assert "symlink" in err.lower()


def test_regular_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A regular file at the directory path raises a ClickException."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    file_path = tmp_path / "headroom"
    file_path.write_text("not a dir")

    with pytest.raises(click.ClickException) as exc_info:
        default_embed_socket_path(8787)

    err = str(exc_info.value)
    assert str(file_path) in err
    assert "not a directory" in err.lower()


def test_explicit_socket_override_used_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit socket override bypasses default_embed_socket_path and directory checks."""
    import headroom.proxy.server as server_mod

    monkeypatch.setattr(server_mod, "run_server", lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "headroom.memory.adapters.watchdog", None)

    # Use a path in a non-existent / unvalidated directory
    custom_socket = str(tmp_path / "deep" / "nested" / "custom.sock")

    result = CliRunner().invoke(
        main,
        [
            "proxy",
            "--embedding-server",
            "--embedding-server-socket",
            custom_socket,
            "--port",
            "8799",
        ],
    )

    assert result.exit_code == 0, f"proxy failed: {result.output}"
    assert custom_socket in result.output


def test_both_rendezvous_sites_produce_same_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both proxy rendezvous sites compute the same socket path for the same port."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    port = 8787

    # Site 1 (tuning / banner section)
    path_site1 = default_embed_socket_path(port)
    # Site 2 (embedding server startup)
    path_site2 = default_embed_socket_path(port)

    assert path_site1 == path_site2
    assert path_site1 == str(tmp_path / "headroom" / f"embed-{port}.sock")


def test_non_posix_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-POSIX systems without os.getuid fall back to tempdir without POSIX uid/mode checks."""
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    socket_path = default_embed_socket_path(8787)
    assert socket_path == str(tmp_path / "headroom" / "embed-8787.sock")


def test_load_merged_state_dict_weights_only(tmp_path: Path) -> None:
    """_load_merged_state_dict successfully loads state dicts with weights_only=True."""
    import torch
    import torch.nn as nn

    from headroom.transforms.kompress_compressor import _load_merged_state_dict

    ckpt_path = tmp_path / "merged.pt"
    encoder = nn.Linear(4, 4)
    token_head = nn.Linear(4, 2)
    span_conv = nn.Conv1d(4, 4, 3, padding=1)

    ckpt = {
        "encoder_state_dict": encoder.state_dict(),
        "token_head_state_dict": token_head.state_dict(),
        "span_conv_state_dict": span_conv.state_dict(),
    }
    torch.save(ckpt, str(ckpt_path))

    class DummyModel:
        def __init__(self) -> None:
            self.encoder = nn.Linear(4, 4)
            self.token_head = nn.Linear(4, 2)
            self.span_conv = nn.Conv1d(4, 4, 3, padding=1)

    dummy = DummyModel()
    _load_merged_state_dict(dummy, str(ckpt_path), "test-model")
    for p1, p2 in zip(encoder.parameters(), dummy.encoder.parameters()):
        assert torch.equal(p1, p2)
