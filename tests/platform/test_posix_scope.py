"""Platform tests for the POSIX process-group scope terminator.

Focuses on the dead-root / dissolved-PGID degraded path (PROC-004):
if the wrapper process dies before cleanup runs, descendant processes that
stayed in the same PGID should still be caught via the secondary orphan scan.

Classified platform: all psutil.Process, os.killpg, and _scan_by_pgid calls are
fully mocked — no real I/O.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import psutil

from meridian.lib.platform.process_scope import posix
from meridian.lib.platform.process_scope.base import CleanupResult
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest

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
