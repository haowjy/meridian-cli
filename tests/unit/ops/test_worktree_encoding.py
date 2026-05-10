"""Tests that _run_git() passes encoding="utf-8" to subprocess.run.

Without an explicit encoding, subprocess.run(text=True) inherits the locale
code page on Windows (often cp1252), which raises UnicodeDecodeError when git
emits branch names or commit messages containing non-ASCII characters.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meridian.lib.ops.worktree_ops import (
    WorktreeRepoResolutionError,
    _run_git,
    detect_git_repo,
)


def _mock_completed(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    result: subprocess.CompletedProcess[str] = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = 0
    return result


# ---------------------------------------------------------------------------
# Encoding parameter
# ---------------------------------------------------------------------------


def test_run_git_passes_utf8_encoding(tmp_path: Path) -> None:
    """subprocess.run receives encoding='utf-8', not the locale default."""
    with patch("subprocess.run", return_value=_mock_completed(stdout="true")) as mock_run:
        _run_git(
            tmp_path, ["rev-parse", "--is-inside-work-tree"],
            error_type=WorktreeRepoResolutionError,
        )

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs.get("encoding") == "utf-8", (
        "encoding='utf-8' must be passed explicitly; "
        "text=True alone inherits the locale code page on Windows"
    )


def test_run_git_uses_text_mode(tmp_path: Path) -> None:
    """text=True is still set alongside encoding='utf-8'."""
    with patch("subprocess.run", return_value=_mock_completed(stdout="true")) as mock_run:
        _run_git(
            tmp_path, ["rev-parse", "--is-inside-work-tree"],
            error_type=WorktreeRepoResolutionError,
        )

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs.get("text") is True


def test_run_git_captures_output(tmp_path: Path) -> None:
    """capture_output=True is set so stdout/stderr are captured."""
    with patch("subprocess.run", return_value=_mock_completed(stdout="true")) as mock_run:
        _run_git(
            tmp_path, ["rev-parse", "--is-inside-work-tree"],
            error_type=WorktreeRepoResolutionError,
        )

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs.get("capture_output") is True


# ---------------------------------------------------------------------------
# Integration: detect_git_repo reads UTF-8 output correctly
# ---------------------------------------------------------------------------


def test_detect_git_repo_decodes_utf8_output(tmp_path: Path) -> None:
    """detect_git_repo works correctly when git output is decoded as utf-8."""
    with patch("subprocess.run", return_value=_mock_completed(stdout="true\n")):
        assert detect_git_repo(tmp_path) is True

    with patch("subprocess.run", return_value=_mock_completed(stdout="false\n")):
        assert detect_git_repo(tmp_path) is False


def test_run_git_propagates_file_not_found(tmp_path: Path) -> None:
    """FileNotFoundError (git not installed) is wrapped in the given error_type."""
    with (
        patch("subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(WorktreeRepoResolutionError, match="git is not installed"),
    ):
        _run_git(
            tmp_path, ["rev-parse", "--is-inside-work-tree"],
            error_type=WorktreeRepoResolutionError,
        )


def test_run_git_propagates_called_process_error(tmp_path: Path) -> None:
    """Non-zero exit is wrapped in the given error_type."""
    exc = subprocess.CalledProcessError(128, ["git"], stderr="not a git repository")
    with (
        patch("subprocess.run", side_effect=exc),
        pytest.raises(WorktreeRepoResolutionError, match="not a git repository"),
    ):
        _run_git(
            tmp_path, ["rev-parse", "--is-inside-work-tree"],
            error_type=WorktreeRepoResolutionError,
        )
