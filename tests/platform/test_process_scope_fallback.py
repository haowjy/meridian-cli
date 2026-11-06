from __future__ import annotations

from dataclasses import dataclass

import psutil
from typing import TYPE_CHECKING

from meridian.lib.platform.process_scope import fallback


if TYPE_CHECKING:
    import pytest


@dataclass
class _FakeProc:
    pid: int
    terminate_exc: BaseException | None = None
    kill_exc: BaseException | None = None
    terminated: int = 0
    killed: int = 0
    create_time_value: float = 100.0
    _children: list[object] | None = None

    def create_time(self) -> float:
        return self.create_time_value

    def children(self, recursive: bool = False) -> list[object]:
        assert recursive is True
        return list(self._children or [])

    def terminate(self) -> None:
        self.terminated += 1
        if self.terminate_exc is not None:
            raise self.terminate_exc

    def kill(self) -> None:
        self.killed += 1
        if self.kill_exc is not None:
            raise self.kill_exc


def test_terminate_tree_sync_skips_on_pid_reuse_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _FakeProc(pid=111, create_time_value=100.0)
    monkeypatch.setattr(psutil, "Process", lambda _pid: root)

    result = fallback.terminate_tree_sync(
        111,
        created_at_epoch=999.0,
        grace_secs=1.0,
        reason="reaper",
        scope_id="backend",
    )

    assert result.skip_reason == "pid_reuse_detected"
    assert result.kill_escalated is False
    assert root.terminated == 0
    assert root.killed == 0


