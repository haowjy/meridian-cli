from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.streaming import control_socket as control_socket_module
from meridian.lib.streaming.control_socket import control_socket_path
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest


@posix_only
def test_socket_paths_are_short_and_deterministic_for_long_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
    _isolate_control_sockets: Path,
) -> None:
    runtime_root = Path.cwd() / ("pathologically-long-runtime-root-" * 10)
    monkeypatch.setattr(control_socket_module, "_POSIX_SOCKET_ROOT", _isolate_control_sockets)
    first = control_socket_path(runtime_root, "spawn-one")
    repeated = control_socket_path(runtime_root, "spawn-one")
    second = control_socket_path(runtime_root, "spawn-two")

    assert first.parent == Path("/tmp") / f"meridian-{os.getuid()}"
    # Under macOS's 104-byte sun_path whatever the runtime root's length.
    assert len(os.fsencode(first)) < 100
    assert first != second
    assert first == repeated
