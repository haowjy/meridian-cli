"""Parent-death linkage helpers for detached subprocess backends."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from meridian.lib.platform import detached_process


def test_link_child_lifetime_to_parent_links_windows_child_with_job(
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
