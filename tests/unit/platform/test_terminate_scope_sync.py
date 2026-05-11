"""Tests for terminate_scope_sync dispatch matrix."""

from __future__ import annotations

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


def test_posix_pgid_with_pgid_calls_terminate_pgid(monkeypatch) -> None:
    """posix_pgid + non-null pgid → terminate_pgid is called."""
    calls: list[dict] = []

    def _fake_terminate_pgid(**kwargs) -> CleanupResult:
        calls.append(kwargs)
        return _ok_result(degraded_fallback=False)

    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.posix.terminate_pgid",
        _fake_terminate_pgid,
    )

    scope = _scope("posix_pgid", pgid=42)
    result = terminate_scope_sync(scope, grace_seconds=5.0, reason="reaper")

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


def test_posix_pgid_with_null_pgid_falls_back_degraded(monkeypatch) -> None:
    """posix_pgid + null pgid → terminate_tree_sync with degraded_fallback=True."""
    calls: list[dict] = []

    def _fake_terminate_tree_sync(**kwargs) -> CleanupResult:
        calls.append(kwargs)
        return _ok_result(degraded_fallback=kwargs.get("degraded_fallback", False))

    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )

    scope = _scope("posix_pgid", pgid=None)
    result = terminate_scope_sync(scope, grace_seconds=5.0, reason="reaper")

    assert calls[0]["degraded_fallback"] is True
    assert result.degraded_fallback is True


def test_windows_job_falls_back_degraded(monkeypatch) -> None:
    """windows_job → terminate_tree_sync with degraded_fallback=True."""
    calls: list[dict] = []

    def _fake_terminate_tree_sync(**kwargs) -> CleanupResult:
        calls.append(kwargs)
        return _ok_result(degraded_fallback=kwargs.get("degraded_fallback", False))

    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )

    scope = _scope("windows_job")
    result = terminate_scope_sync(scope, grace_seconds=5.0, reason="reaper")

    assert calls[0]["degraded_fallback"] is True
    assert result.degraded_fallback is True


def test_pid_tree_fallback_not_degraded(monkeypatch) -> None:
    """pid_tree_fallback → terminate_tree_sync with degraded_fallback=False."""
    calls: list[dict] = []

    def _fake_terminate_tree_sync(**kwargs) -> CleanupResult:
        calls.append(kwargs)
        return _ok_result(degraded_fallback=kwargs.get("degraded_fallback", False))

    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )

    scope = _scope("pid_tree_fallback")
    result = terminate_scope_sync(scope, grace_seconds=5.0, reason="reaper")

    assert calls[0]["degraded_fallback"] is False
    assert result.degraded_fallback is False


def test_unknown_containment_falls_back_degraded(monkeypatch) -> None:
    """Unknown containment → terminate_tree_sync with degraded_fallback=True."""
    calls: list[dict] = []

    def _fake_terminate_tree_sync(**kwargs) -> CleanupResult:
        calls.append(kwargs)
        return _ok_result(degraded_fallback=kwargs.get("degraded_fallback", False))

    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )

    scope = _scope("some_unknown_containment")
    result = terminate_scope_sync(scope, grace_seconds=5.0, reason="reaper")

    assert calls[0]["degraded_fallback"] is True
    assert result.degraded_fallback is True
