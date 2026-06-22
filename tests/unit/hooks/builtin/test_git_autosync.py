from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from meridian.lib.hooks.builtin.git_autosync import GitAutosync

if TYPE_CHECKING:
    import pytest


def _completed() -> subprocess.CompletedProcess[str]:
    result: subprocess.CompletedProcess[str] = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = ""
    result.stderr = ""
    result.returncode = 0
    return result


def _git_result(
    returncode: int,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_run_git_strips_repo_scoped_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/real-checkout/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/real-checkout")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/test-gitconfig")

    with patch("subprocess.run", return_value=_completed()) as mock_run:
        GitAutosync()._run_git("/tmp/work", ["status"], timeout=5)

    call_env = mock_run.call_args.kwargs["env"]
    assert "GIT_DIR" not in call_env
    assert "GIT_WORK_TREE" not in call_env
    assert call_env["GIT_CONFIG_GLOBAL"] == "/tmp/test-gitconfig"
    assert call_env["LC_ALL"] == "C"


def test_check_divergence_no_upstream_is_debug_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    autosync = GitAutosync()

    def fake_run_git(
        work_dir: str,
        args: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        _ = work_dir, timeout
        assert args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
        return _git_result(
            128,
            stderr="fatal: no upstream configured for branch 'main'",
        )

    monkeypatch.setattr(autosync, "_run_git", fake_run_git)

    with patch("meridian.lib.hooks.builtin.git_autosync.logger") as mock_logger:
        assert autosync._check_divergence("/tmp/clone") is None

    mock_logger.debug.assert_called_once()
    assert mock_logger.debug.call_args.args == ("git_autosync_divergence_check_skipped",)
    assert mock_logger.debug.call_args.kwargs["skip_reason"] == "no_upstream"
    mock_logger.warning.assert_not_called()


def test_check_divergence_with_upstream_computes_ahead_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    autosync = GitAutosync()
    calls: list[list[str]] = []

    def fake_run_git(
        work_dir: str,
        args: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        _ = work_dir, timeout
        calls.append(args)
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]:
            return _git_result(0, stdout="origin/main\n")
        if args == ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]:
            return _git_result(0, stdout="2\t3\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(autosync, "_run_git", fake_run_git)

    with patch("meridian.lib.hooks.builtin.git_autosync.logger") as mock_logger:
        assert autosync._check_divergence("/tmp/clone", "main") == (2, 3)

    assert calls == [
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
    ]
    mock_logger.warning.assert_not_called()
