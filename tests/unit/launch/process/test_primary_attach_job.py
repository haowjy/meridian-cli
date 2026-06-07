"""Regression tests for shared Windows parent-death linkage."""

from __future__ import annotations

from typing import Any

import pytest

from meridian.lib.platform import detached_process
from meridian.lib.platform.process_scope.windows_job import assign_to_new_job


@pytest.mark.unit
def test_assign_to_new_job_returns_none_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """assign_to_new_job() is a no-op on POSIX; must return None."""
    monkeypatch.setattr("meridian.lib.platform.process_scope.windows_job.IS_WINDOWS", False)
    assert assign_to_new_job(12345) is None


@pytest.mark.unit
def test_link_child_lifetime_posix_returns_empty_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX, parent-death linkage is carried by preexec_fn, not a job handle."""
    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)

    link = detached_process.link_child_lifetime_to_parent(99999)

    assert link.job_name is None
    assert link.job_handle is None


@pytest.mark.unit
def test_link_child_lifetime_windows_returns_job_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, a successful assign_to_new_job returns the stored job handle."""
    fake_handle = object()
    monkeypatch.setattr(detached_process, "IS_WINDOWS", True)
    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.windows_job.assign_to_new_job",
        lambda pid: ("meridian-scope-abc123", fake_handle),
    )

    link = detached_process.link_child_lifetime_to_parent(1234)

    assert link.job_name == "meridian-scope-abc123"
    assert link.job_handle is fake_handle


@pytest.mark.unit
def test_link_child_lifetime_passes_correct_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backend PID is forwarded to assign_to_new_job unchanged."""
    received: list[int] = []
    fake_handle = object()

    def _assign(pid: int) -> tuple[str, Any]:
        received.append(pid)
        return "job", fake_handle

    monkeypatch.setattr(detached_process, "IS_WINDOWS", True)
    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.windows_job.assign_to_new_job",
        _assign,
    )

    detached_process.link_child_lifetime_to_parent(5678)

    assert received == [5678]


@pytest.mark.unit
def test_link_child_lifetime_none_from_assign_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """assign_to_new_job returning None leaves an empty parent-death link."""
    monkeypatch.setattr(detached_process, "IS_WINDOWS", True)
    monkeypatch.setattr(
        "meridian.lib.platform.process_scope.windows_job.assign_to_new_job",
        lambda pid: None,
    )

    link = detached_process.link_child_lifetime_to_parent(999)

    assert link.job_name is None
    assert link.job_handle is None
