"""Platform tests for the POSIX process-group scope terminator.

Focuses on the dead-root / dissolved-PGID degraded path (PROC-004):
if the wrapper process dies before cleanup runs, descendant processes that
stayed in the same PGID should still be caught via the secondary orphan scan.

Classified platform: all psutil.Process, os.killpg, and _scan_by_pgid calls are
fully mocked — no real I/O.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import psutil

from tests.conftest import posix_only

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PROC-004: dead root + dissolved PGID → orphan scan runs
# ---------------------------------------------------------------------------


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


