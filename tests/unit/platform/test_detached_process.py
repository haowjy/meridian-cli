"""Parent-death linkage helpers for detached subprocess backends."""

from __future__ import annotations

import signal
from typing import TYPE_CHECKING, Any

from meridian.lib.platform import detached_process

if TYPE_CHECKING:
    import pytest


def test_detached_backend_subprocess_kwargs_sets_posix_parent_death_preexec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)
    calls: list[signal.Signals] = []

    def _set_parent_death_signal(signum: signal.Signals) -> None:
        calls.append(signum)

    monkeypatch.setattr(
        detached_process,
        "_set_parent_death_signal",
        _set_parent_death_signal,
    )
    monkeypatch.setattr(detached_process.os, "getppid", lambda: 4242)

    kwargs = detached_process.detached_backend_subprocess_kwargs()
    preexec = kwargs["preexec_fn"]

    assert kwargs["start_new_session"] is True
    assert callable(preexec)
    preexec()
    assert calls == [signal.SIGKILL]


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

    assert detached_process.detached_backend_subprocess_kwargs() == {}

    link = detached_process.link_child_lifetime_to_parent(1234)

    assert assigned == [1234]
    assert link.job_name == "job-1"
    assert link.job_handle is handle
