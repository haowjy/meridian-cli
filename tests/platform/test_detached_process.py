"""Parent-death linkage helpers for detached subprocess backends."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, Any

from meridian.lib.platform import detached_process
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest


@posix_only
def test_detached_subprocess_config_isolates_backend_without_preexec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)

    config = detached_process.detached_subprocess_config()

    assert config.kwargs == {"start_new_session": True}
    assert "preexec_fn" not in config.kwargs


def test_link_child_lifetime_to_parent_posix_starts_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Process:
        pid = 4567

    class _Watchdog:
        process = _Process()

        def register_scope(self, scope: ProcessScopeSnapshot) -> bool:
            registered.append(scope)
            return True

        def close(self) -> None:
            return None

    registered: list[ProcessScopeSnapshot] = []

    def _pgid(_pid: int) -> int:
        return 1234

    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)
    monkeypatch.setattr(detached_process, "_process_group_id", _pgid)
    monkeypatch.setattr(detached_process, "start_parent_watchdog", _Watchdog)

    link = detached_process.link_child_lifetime_to_parent(1234)

    assert link.parent_death_linked is True
    assert link.watchdog_process is not None
    assert link.watchdog_process.pid == 4567
    assert [(scope.root_pid, scope.pgid) for scope in registered] == [(1234, 1234)]


def test_detached_subprocess_config_links_windows_child_with_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.platform.process_scope import windows_job

    handle = object()
    assigned: list[int] = []

    def _assign(pid: int) -> tuple[str, Any]:
        assigned.append(pid)
        return "job-1", handle

    monkeypatch.setattr(detached_process, "IS_WINDOWS", True)
    monkeypatch.setattr(windows_job, "assign_to_new_job", _assign)

    config = detached_process.detached_subprocess_config()
    assert config.kwargs == {}

    link = detached_process.link_child_lifetime_to_parent(1234)

    assert assigned == [1234]
    assert link.job_name == "job-1"
    assert link.job_handle is handle
    assert link.parent_death_linked is True


def test_link_child_lifetime_to_parent_reports_windows_job_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.platform.process_scope import windows_job

    def _assign_failed(_pid: int) -> None:
        return None

    monkeypatch.setattr(detached_process, "IS_WINDOWS", True)
    monkeypatch.setattr(windows_job, "assign_to_new_job", _assign_failed)

    link = detached_process.link_child_lifetime_to_parent(999)

    assert link.job_name is None
    assert link.job_handle is None
    assert link.parent_death_linked is False


def test_link_child_lifetime_to_parent_posix_falls_back_to_pid_tree_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _pgid_unavailable(_pid: int) -> None:
        return None

    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)
    monkeypatch.setattr(detached_process, "_process_group_id", _pgid_unavailable)

    child = subprocess.Popen((sys.executable, "-c", "import time; time.sleep(60)"))
    link = detached_process.link_child_lifetime_to_parent(child.pid)

    try:
        assert link.parent_death_linked is True
        assert link.watchdog_process is not None
    finally:
        child.terminate()
        child.wait(timeout=5.0)
        detached_process.release_parent_death_link(link)


def test_watchdog_terminates_target_when_parent_dies() -> None:
    from meridian.lib.platform.parent_watchdog import WatchedProcess, watch_parent_until_exit

    parent = WatchedProcess(pid=1, created_at_epoch=1.0)
    target_scope = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id="spawn-1",
        role="harness_backend",
        containment="posix_pgid",
        root_pid=2,
        root_created_at_epoch=2.0,
        pgid=2,
        job_name=None,
        degraded_reason=None,
    )
    terminated: list[tuple[ProcessScopeSnapshot, float, str]] = []

    def _parent_alive(_parent: WatchedProcess) -> bool:
        return False

    def _target_alive(_target: ProcessScopeSnapshot) -> bool:
        return True

    def _terminate_scope(
        scope: ProcessScopeSnapshot,
        *,
        grace_seconds: float,
        reason: str,
    ) -> None:
        terminated.append((scope, grace_seconds, reason))

    def _sleep(_seconds: float) -> None:
        return None

    result = watch_parent_until_exit(
        parent=parent,
        target_scope=target_scope,
        parent_alive=_parent_alive,
        target_alive=_target_alive,
        terminate_scope=_terminate_scope,
        sleep=_sleep,
    )

    assert result == 0
    assert terminated == [(target_scope, 5.0, "parent_exit_watchdog")]
