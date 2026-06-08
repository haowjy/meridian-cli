from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from meridian.lib.platform.process_scope import posix
from meridian.lib.platform.process_scope.base import CleanupResult
from tests.conftest import posix_only


@dataclass
class _FakeProc:
    pid: int
    _children: list[object]
    killed: int = 0

    def create_time(self) -> float:
        return 100.0

    def children(self, recursive: bool = False) -> list[object]:
        assert recursive is True
        return list(self._children)

    def kill(self) -> None:
        self.killed += 1


class _FakeChild:
    def __init__(self, pid: int) -> None:
        self.pid = pid


@posix_only
def test_terminate_pgid_degrades_to_tree_when_root_is_not_group_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(posix, "IS_WINDOWS", False)

    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    fallback_calls: list[dict[str, object]] = []
    fallback_result = CleanupResult(
        scope_id="backend",
        root_pid=111,
        descendant_count=0,
        reason="reaper",
        grace_seconds=5.0,
        kill_escalated=False,
        degraded_fallback=True,
        skip_reason=None,
    )

    def _fake_terminate_tree_sync(**kwargs: object) -> CleanupResult:
        fallback_calls.append(kwargs)
        return fallback_result

    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.fallback.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )

    result = posix.terminate_pgid(
        pgid=222,
        root_pid=111,
        created_at_epoch=100.0,
        grace_seconds=5.0,
        reason="reaper",
        scope_id="backend",
    )

    assert result == fallback_result
    assert fallback_calls == [
        {
            "pid": 111,
            "created_at_epoch": 100.0,
            "grace_secs": 5.0,
            "reason": "reaper",
            "scope_id": "backend",
            "degraded_fallback": True,
        }
    ]
    assert killpg_calls == []


def test_terminate_pgid_on_windows_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(posix, "IS_WINDOWS", True)

    with pytest.raises(RuntimeError, match="not available on Windows"):
        posix.terminate_pgid(
            pgid=222,
            root_pid=111,
            created_at_epoch=100.0,
            grace_seconds=5.0,
            reason="reaper",
            scope_id="backend",
        )


