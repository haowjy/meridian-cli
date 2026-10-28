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
@pytest.mark.parametrize("kind", ["control", "app"])
def test_socket_paths_are_bounded_and_deterministic_for_long_runtime_root(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_control_sockets: Path,
) -> None:
    runtime_root = Path.cwd() / ("pathologically-long-runtime-root-" * 10)
    assert len(os.fsencode(runtime_root)) > UNIX_SOCKET_PATH_MAX_BYTES

    if kind == "control":
        monkeypatch.setattr(control_socket_module, "_POSIX_SOCKET_ROOT", _isolate_control_sockets)
        first = control_socket_path(runtime_root, "spawn-one")
        repeated = control_socket_path(runtime_root, "spawn-one")
        second = control_socket_path(runtime_root, "spawn-two")
        assert first.parent == Path("/tmp") / f"meridian-{os.getuid()}"
        assert first != second
    else:
        first = bounded_socket_path(runtime_root, "app")
        repeated = bounded_socket_path(runtime_root, "app")

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
