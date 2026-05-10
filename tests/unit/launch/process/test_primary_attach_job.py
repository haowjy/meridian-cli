"""Unit tests for Windows Job Object wiring in primary_attach.

Exercises _try_assign_backend_job() and assign_to_new_job() to verify:
  - POSIX path: no job assignment, returns None
  - Windows path: assign_to_new_job() called with backend PID
  - Windows failure: degrade cleanly (None), never crash
  - windows_job.assign_to_new_job() is importable and returns None on POSIX
"""

from __future__ import annotations

import pytest

from meridian.lib.launch.process.primary_attach import _try_assign_backend_job
from meridian.lib.platform.windows_job import assign_to_new_job

# ---------------------------------------------------------------------------
# windows_job.assign_to_new_job — importability and POSIX no-op
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_assign_to_new_job_returns_none_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """assign_to_new_job() is a no-op on POSIX; must return None."""
    monkeypatch.setattr("meridian.lib.platform.windows_job.IS_WINDOWS", False)
    assert assign_to_new_job(12345) is None


# ---------------------------------------------------------------------------
# _try_assign_backend_job — POSIX path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_try_assign_backend_job_posix_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX IS_WINDOWS=False — never calls into windows_job."""
    monkeypatch.setattr("meridian.lib.launch.process.primary_attach.IS_WINDOWS", False)
    assert _try_assign_backend_job(99999) is None


# ---------------------------------------------------------------------------
# _try_assign_backend_job — Windows success path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_try_assign_backend_job_windows_returns_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, a successful assign_to_new_job returns the job handle."""
    fake_handle = object()
    monkeypatch.setattr("meridian.lib.launch.process.primary_attach.IS_WINDOWS", True)
    monkeypatch.setattr(
        "meridian.lib.platform.windows_job.assign_to_new_job",
        lambda pid: ("meridian-scope-abc123", fake_handle),
    )
    result = _try_assign_backend_job(1234)
    assert result is fake_handle


@pytest.mark.unit
def test_try_assign_backend_job_passes_correct_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backend PID is forwarded to assign_to_new_job unchanged."""
    received: list[int] = []
    fake_handle = object()
    monkeypatch.setattr("meridian.lib.launch.process.primary_attach.IS_WINDOWS", True)
    monkeypatch.setattr(
        "meridian.lib.platform.windows_job.assign_to_new_job",
        lambda pid: (received.append(pid) or ("job", fake_handle)),  # type: ignore[func-returns-value]
    )
    _try_assign_backend_job(5678)
    assert received == [5678]


# ---------------------------------------------------------------------------
# _try_assign_backend_job — Windows degraded paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_try_assign_backend_job_none_from_assign_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """assign_to_new_job returning None means degrade: _try_assign returns None."""
    monkeypatch.setattr("meridian.lib.launch.process.primary_attach.IS_WINDOWS", True)
    monkeypatch.setattr(
        "meridian.lib.platform.windows_job.assign_to_new_job",
        lambda pid: None,
    )
    assert _try_assign_backend_job(999) is None


@pytest.mark.unit
def test_try_assign_backend_job_exception_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception inside assign_to_new_job must not propagate — return None."""

    def _fail(pid: int) -> None:
        raise OSError("access denied")

    monkeypatch.setattr("meridian.lib.launch.process.primary_attach.IS_WINDOWS", True)
    monkeypatch.setattr("meridian.lib.platform.windows_job.assign_to_new_job", _fail)
    # Must not raise.
    assert _try_assign_backend_job(111) is None


@pytest.mark.unit
def test_try_assign_backend_job_runtime_error_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any unexpected error inside assign_to_new_job degrades cleanly to None."""

    def _fail(pid: int) -> None:
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr("meridian.lib.launch.process.primary_attach.IS_WINDOWS", True)
    monkeypatch.setattr("meridian.lib.platform.windows_job.assign_to_new_job", _fail)
    assert _try_assign_backend_job(333) is None
