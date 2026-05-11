# qa-validated: reaper-escape-fix-test-cleanup
"""Behavior tests for terminate_scope_sync dispatch."""

from __future__ import annotations

import pytest

from meridian.lib.platform.process_scope import terminate_scope_sync
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot


def _scope(
    containment: str,
    *,
    pgid: int | None = None,
) -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id="test",
        owner_policy="spawn_owned",
        owner_id="spawn-1",
        role="harness_backend",
        containment=containment,
        root_pid=100,
        root_created_at_epoch=10.0,
        pgid=pgid,
        job_name=None,
        degraded_reason=None,
    )


def _ok_result(scope_id: str = "test", degraded_fallback: bool = False) -> CleanupResult:
    return CleanupResult(
        scope_id=scope_id,
        root_pid=100,
        descendant_count=0,
        reason="reaper",
        grace_seconds=5.0,
        kill_escalated=False,
        degraded_fallback=degraded_fallback,
        skip_reason=None,
    )


def test_posix_pgid_with_group_dispatches_to_terminate_pgid(monkeypatch) -> None:
    """A validated process group should use the stronger PGID terminator."""
    calls: list[dict[str, object]] = []

    def _fake_terminate_pgid(**kwargs: object) -> CleanupResult:
        calls.append(kwargs)
        return _ok_result(degraded_fallback=False)

    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.posix.terminate_pgid",
        _fake_terminate_pgid,
    )

    result = terminate_scope_sync(_scope("posix_pgid", pgid=42), grace_seconds=5.0, reason="reaper")

    assert calls == [
        {
            "pgid": 42,
            "root_pid": 100,
            "created_at_epoch": 10.0,
            "grace_seconds": 5.0,
            "reason": "reaper",
            "scope_id": "test",
        }
    ]
    assert result.degraded_fallback is False


@pytest.mark.parametrize(
    ("containment", "pgid", "expected_degraded"),
    [
        ("posix_pgid", None, True),
        ("windows_job", None, True),
        ("pid_tree_fallback", None, False),
        ("some_unknown_containment", None, True),
    ],
)
def test_non_native_scope_paths_use_tree_fallback_with_expected_degraded_flag(
    monkeypatch,
    containment: str,
    pgid: int | None,
    expected_degraded: bool,
) -> None:
    """Fallback dispatch should preserve whether the path is native or degraded."""
    calls: list[dict[str, object]] = []

    def _fake_terminate_tree_sync(**kwargs: object) -> CleanupResult:
        calls.append(kwargs)
        return _ok_result(degraded_fallback=bool(kwargs["degraded_fallback"]))

    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )

    result = terminate_scope_sync(
        _scope(containment, pgid=pgid), grace_seconds=5.0, reason="reaper"
    )

    assert calls == [
        {
            "pid": 100,
            "created_at_epoch": 10.0,
            "grace_secs": 5.0,
            "reason": "reaper",
            "scope_id": "test",
            "degraded_fallback": expected_degraded,
        }
    ]
    assert result.degraded_fallback is expected_degraded


def test_terminator_exception_returns_degraded_result(monkeypatch) -> None:
    """Termination errors should degrade to a CleanupResult instead of escaping."""

    def _raising_terminate_tree_sync(**_kwargs: object) -> CleanupResult:
        raise OSError("kill failed")

    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.terminate_tree_sync",
        _raising_terminate_tree_sync,
    )

    result = terminate_scope_sync(_scope("pid_tree_fallback"), grace_seconds=5.0, reason="reaper")

    assert result.degraded_fallback is True
    assert result.skip_reason == "termination_exception"
    assert result.scope_id == "test"
    assert result.root_pid == 100
    assert result.reason == "reaper"
