"""Unit tests for posix.py process-group scope terminator.

Focuses on the dead-root / dissolved-PGID degraded path (PROC-004):
if the wrapper process dies before cleanup runs, descendant processes that
stayed in the same PGID should still be caught via the secondary orphan scan.

Classified unit: all psutil.Process, os.killpg, and _scan_by_pgid calls are
fully mocked — no real I/O.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import psutil
import pytest

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only test")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_mock_proc(pid: int) -> MagicMock:
    proc = MagicMock(spec=psutil.Process)
    proc.pid = pid
    return proc


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
            pgid=500,
            root_pid=12345,
            created_at_epoch=1_000_000.0,  # expected birth ≠ 9_999_999
            grace_seconds=0.1,
            reason="test_stop",
            scope_id="backend",
        )

    assert result.skip_reason == "pid_reuse_detected"
    mock_killpg.assert_not_called()
