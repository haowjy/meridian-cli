from __future__ import annotations

import os
from pathlib import Path

import pytest

from meridian.lib.streaming import control_socket as control_socket_module
from meridian.lib.streaming.control_socket import (
    UNIX_SOCKET_PATH_MAX_BYTES,
    bounded_socket_path,
    control_socket_path,
)
from tests.conftest import posix_only


@posix_only
def test_control_socket_path_is_bounded_and_unique_for_long_runtime_root() -> None:
    runtime_root = Path.cwd() / ("pathologically-long-runtime-root-" * 10)

    first = control_socket_path(runtime_root, "spawn-one")
    repeated = control_socket_path(runtime_root, "spawn-one")
    second = control_socket_path(runtime_root, "spawn-two")

    assert len(os.fsencode(first)) <= UNIX_SOCKET_PATH_MAX_BYTES
    assert first.parent == Path("/tmp") / f"meridian-{os.getuid()}"
    assert first == repeated
    assert first != second


@posix_only
def test_bounded_app_socket_path_is_deterministic_for_long_runtime_root() -> None:
    runtime_root = Path.cwd() / ("pathologically-long-runtime-root-" * 10)

    first = bounded_socket_path(runtime_root, "app")
    repeated = bounded_socket_path(runtime_root, "app")

    assert len(os.fsencode(runtime_root)) > UNIX_SOCKET_PATH_MAX_BYTES
    assert len(os.fsencode(first)) <= UNIX_SOCKET_PATH_MAX_BYTES
    assert first == repeated


@posix_only
def test_control_socket_path_rejects_overlong_fixed_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control_socket_module,
        "_POSIX_SOCKET_ROOT",
        Path("/") / ("overlong-socket-root" * 10),
    )

    with pytest.raises(ValueError, match="exceeding the platform limit"):
        control_socket_path(Path("runtime"), "spawn-one")
