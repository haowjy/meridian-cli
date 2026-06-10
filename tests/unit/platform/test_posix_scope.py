"""Unit tests for posix.py process-group scope terminator.

Focuses on the dead-root / dissolved-PGID degraded path (PROC-004):
if the wrapper process dies before cleanup runs, descendant processes that
stayed in the same PGID should still be caught via the secondary orphan scan.

Classified unit: all psutil.Process, os.killpg, and _scan_by_pgid calls are
fully mocked — no real I/O.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import psutil
import pytest

from meridian.lib.platform.process_scope import posix
from meridian.lib.platform.process_scope.base import PROCESS_BIRTH_UNKNOWN_EPOCH, CleanupResult

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only test")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_mock_proc(pid: int) -> MagicMock:
    proc = MagicMock(spec=psutil.Process)
    proc.pid = pid
    return proc


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


# ---------------------------------------------------------------------------
# PROC-004: dead root + dissolved PGID → orphan scan runs
# ---------------------------------------------------------------------------


@posix_only
def test_dead_root_pgid_still_has_members_no_scan_needed() -> None:
    """PROC-004: Root is dead, but SIGTERM to the PGID succeeds (group still
    exists). No secondary scan needed because SIGTERM was delivered to the group
    via os.killpg — the orphan-scan path only activates on ProcessLookupError.
    """
    from meridian.lib.platform.process_scope.posix import terminate_pgid

    with (
        patch(
            "meridian.lib.platform.process_scope.posix.psutil.Process",
            side_effect=psutil.NoSuchProcess(pid=12345),
        ),
        # SIGTERM succeeds (group still exists)
        patch("os.killpg"),
        patch(
            "meridian.lib.platform.process_scope.posix._scan_by_pgid",
        ) as mock_scan,
    ):
        result = terminate_pgid(
            pgid=500,
            root_pid=12345,
            created_at_epoch=1_000_000.0,
            grace_seconds=0.1,
            reason="test_stop",
            scope_id="backend",
        )

    # Group kill succeeded — no need for orphan scan
    mock_scan.assert_not_called()
    assert result.degraded_fallback is True  # root was dead → degraded
    assert result.skip_reason is None


@posix_only
def test_confirmed_pid_reuse_still_skips() -> None:
    """PROC-006 guard: confirmed PID reuse must still cause a hard skip,
    even with the new dead-root handling.
    """
    from meridian.lib.platform.process_scope.posix import terminate_pgid

    alive_proc = MagicMock(spec=psutil.Process)
    alive_proc.create_time.return_value = 9_999_999.0  # birth time far from expected

    with (
        patch(
            "meridian.lib.platform.process_scope.posix.psutil.Process",
            return_value=alive_proc,
        ),
        patch("os.killpg") as mock_killpg,
    ):
        result = terminate_pgid(
            pgid=12345,
            root_pid=12345,
            created_at_epoch=1_000_000.0,  # expected birth ≠ 9_999_999
            grace_seconds=0.1,
            reason="test_stop",
            scope_id="backend",
        )

    assert result.skip_reason == "pid_reuse_detected"
    mock_killpg.assert_not_called()


@posix_only
def test_unknown_birth_time_proceeds_to_signal_owned_pgid() -> None:
    """Unknown birth time means unverified, not reused: owned teardown proceeds."""
    from meridian.lib.platform.process_scope.posix import terminate_pgid

    root_proc = _make_mock_proc(12345)
    root_proc.create_time.side_effect = AssertionError("birth guard should be skipped")
    root_proc.children.return_value = []

    with (
        patch(
            "meridian.lib.platform.process_scope.posix.psutil.Process",
            return_value=root_proc,
        ),
        patch("meridian.lib.platform.process_scope.posix.psutil.wait_procs", return_value=([], [])),
        patch("os.killpg") as mock_killpg,
    ):
        result = terminate_pgid(
            pgid=12345,
            root_pid=12345,
            created_at_epoch=PROCESS_BIRTH_UNKNOWN_EPOCH,
            grace_seconds=0.1,
            reason="test_stop",
            scope_id="backend",
        )

    assert result.skip_reason is None
    mock_killpg.assert_called_once()


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
