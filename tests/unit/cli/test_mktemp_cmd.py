"""Tests for `meridian mktemp`."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import meridian.cli.main as cli_main


def _run_mktemp(*args: str) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer), pytest.raises(SystemExit) as exc_info:
        cli_main.main(["mktemp", *args])
    assert exc_info.value.code in {0, None}
    return buffer.getvalue().strip()


def test_mktemp_creates_file_with_default_suffix() -> None:
    cli_main._register_commands_for_invocation(["mktemp"], agent_mode=False)

    path = Path(_run_mktemp())
    try:
        assert path.exists()
        assert path.is_file()
        assert path.suffix == ".md"
        assert path.name.startswith("meridian-")
    finally:
        path.unlink(missing_ok=True)


def test_mktemp_honors_suffix_flag() -> None:
    cli_main._register_commands_for_invocation(["mktemp"], agent_mode=False)

    path = Path(_run_mktemp("--suffix", ".txt"))
    try:
        assert path.exists()
        assert path.suffix == ".txt"
    finally:
        path.unlink(missing_ok=True)
