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
def test_dead_root_dissolved_pgid_triggers_orphan_scan() -> None:
    """PROC-004: When root is dead and os.killpg raises ProcessLookupError,
    _scan_by_pgid must be called and any found orphans must be waited on.
    """
    from meridian.lib.platform.process_scope.posix import terminate_pgid

    orphan = _make_mock_proc(pid=99999)

    with (
        # Root is dead: both create_time() and Process(root_pid) raise
        patch(
            "meridian.lib.platform.process_scope.posix.psutil.Process",
            side_effect=psutil.NoSuchProcess(pid=12345),
        ),
        # PGID has dissolved: SIGTERM raises ProcessLookupError
        patch("os.killpg", side_effect=ProcessLookupError),
        # Secondary scan finds one orphan
        patch(
            "meridian.lib.platform.process_scope.posix._scan_by_pgid",
            return_value=[orphan],
        ) as mock_scan,
        # wait_procs: grace period expires with no alive processes
        patch(
            "meridian.lib.platform.process_scope.posix.psutil.wait_procs",
            return_value=([], []),
        ),
    ):
        result = terminate_pgid(
            pgid=500,
            root_pid=12345,
            created_at_epoch=1_000_000.0,
            grace_seconds=0.1,
            reason="test_stop",
            scope_id="backend",
        )

    # Seam verification: orphan scan must activate on dissolved PGID
    mock_scan.assert_called_once_with(500)
    assert result.degraded_fallback is True, "must be degraded when root was already dead"
    assert result.skip_reason is None, "must not be skipped — orphans were found"
    assert result.descendant_count == 1, "one orphan was found via PGID scan"


@posix_only
def test_dead_root_dissolved_pgid_no_orphans_found() -> None:
    """PROC-004: When orphan scan finds nothing, result is degraded with
    descendant_count=None (nothing to count / kill).
    """
    from meridian.lib.platform.process_scope.posix import terminate_pgid

    with (
        patch(
            "meridian.lib.platform.process_scope.posix.psutil.Process",
            side_effect=psutil.NoSuchProcess(pid=12345),
        ),
        patch("os.killpg", side_effect=ProcessLookupError),
        patch(
            "meridian.lib.platform.process_scope.posix._scan_by_pgid",
            return_value=[],
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

    # Seam verification: orphan scan must activate on dissolved PGID
    mock_scan.assert_called_once_with(500)
    assert result.degraded_fallback is True
    assert result.skip_reason is None
    assert result.descendant_count is None


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
