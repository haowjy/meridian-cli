"""Unit tests for _terminate_pid in managed_primary."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import psutil
import pytest

from meridian.lib.state.managed_primary import _terminate_pid


def test_rejects_zero_pid() -> None:
    assert _terminate_pid(0) is False


def test_rejects_negative_pid() -> None:
    assert _terminate_pid(-1) is False


def test_rejects_own_pid() -> None:
    assert _terminate_pid(os.getpid()) is False


def test_calls_psutil_terminate_and_returns_true() -> None:
    mock_proc = MagicMock()
    with patch("meridian.lib.state.managed_primary.psutil.Process", return_value=mock_proc) as mock_cls:
        result = _terminate_pid(12345)
    mock_cls.assert_called_once_with(12345)
    mock_proc.terminate.assert_called_once()
    assert result is True


def test_returns_false_on_no_such_process() -> None:
    mock_proc = MagicMock()
    mock_proc.terminate.side_effect = psutil.NoSuchProcess(12345)
    with patch("meridian.lib.state.managed_primary.psutil.Process", return_value=mock_proc):
        assert _terminate_pid(12345) is False


def test_returns_false_on_access_denied() -> None:
    mock_proc = MagicMock()
    mock_proc.terminate.side_effect = psutil.AccessDenied(12345)
    with patch("meridian.lib.state.managed_primary.psutil.Process", return_value=mock_proc):
        assert _terminate_pid(12345) is False


def test_returns_false_on_os_error() -> None:
    mock_proc = MagicMock()
    mock_proc.terminate.side_effect = OSError("permission denied")
    with patch("meridian.lib.state.managed_primary.psutil.Process", return_value=mock_proc):
        assert _terminate_pid(12345) is False
