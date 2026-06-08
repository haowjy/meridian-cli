"""Parent-death linkage helpers for detached subprocess backends."""

from __future__ import annotations

import signal
from typing import Any

import pytest
from structlog.testing import capture_logs

from meridian.lib.platform import IS_WINDOWS, detached_process


@pytest.mark.skipif(IS_WINDOWS, reason="Linux prctl parent-death preexec is POSIX-only")
def test_detached_subprocess_config_links_linux_parent_death_preexec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)
    monkeypatch.setattr(detached_process, "_linux_parent_death_sig_available", lambda: True)
    calls: list[signal.Signals] = []

    def _set_parent_death_signal(signum: signal.Signals) -> None:
        calls.append(signum)

    monkeypatch.setattr(
        detached_process,
        "_set_parent_death_signal",
        _set_parent_death_signal,
    )
    monkeypatch.setattr(detached_process.os, "getppid", lambda: 4242)

    config = detached_process.detached_subprocess_config()
    preexec = config.kwargs["preexec_fn"]

    assert config.parent_death_linked is True
    assert config.kwargs["start_new_session"] is True
    assert callable(preexec)
    preexec()
    assert calls == [signal.SIGKILL]
    assert detached_process.detached_backend_subprocess_kwargs() == config.kwargs


def test_detached_subprocess_config_degrades_without_prctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import structlog

    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)
    monkeypatch.setattr(detached_process, "_linux_parent_death_sig_available", lambda: False)

    with capture_logs() as logs:
        detached_process.logger = structlog.get_logger(detached_process.__name__)
        config = detached_process.detached_subprocess_config()

    assert config.parent_death_linked is False
    assert config.kwargs == {"start_new_session": True}
    assert "preexec_fn" not in config.kwargs
    degraded = next(
        log for log in logs if log["event"] == "detached_backend_parent_death_unavailable"
    )
    assert degraded["platform"] == detached_process.sys.platform
    assert degraded["containment"] == "start_new_session_only"


def test_detached_backend_subprocess_kwargs_links_windows_child_with_job(
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
    assert config.parent_death_linked is False
    assert config.kwargs == {}
    assert detached_process.detached_backend_subprocess_kwargs() == {}

    link = detached_process.link_child_lifetime_to_parent(1234)

    assert assigned == [1234]
    assert link.job_name == "job-1"
    assert link.job_handle is handle
    assert link.parent_death_linked is True


def test_link_child_lifetime_to_parent_reports_windows_job_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.platform.process_scope import windows_job

    monkeypatch.setattr(detached_process, "IS_WINDOWS", True)
    monkeypatch.setattr(windows_job, "assign_to_new_job", lambda _pid: None)

    link = detached_process.link_child_lifetime_to_parent(999)

    assert link.job_name is None
    assert link.job_handle is None
    assert link.parent_death_linked is False


def test_link_child_lifetime_to_parent_posix_is_not_linked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)

    link = detached_process.link_child_lifetime_to_parent(4242)

    assert link == detached_process.ParentDeathLink(parent_death_linked=False)
