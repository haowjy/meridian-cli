"""Unit tests for shared CLI helper contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.cli import utils as cli_utils


def test_implicit_session_ops_payload_skips_project_for_direct_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_utils,
        "require_established_project_root",
        lambda: pytest.fail("project root should not be required for --file"),
    )

    assert cli_utils.implicit_session_ops_payload(file_path="session.jsonl") == {}


def test_implicit_session_ops_payload_requires_project_without_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.setattr(cli_utils, "require_established_project_root", lambda: project_root)

    assert cli_utils.implicit_session_ops_payload(file_path=None) == {
        "project_root": project_root.as_posix()
    }
    assert cli_utils.implicit_session_ops_payload(file_path="   ") == {
        "project_root": project_root.as_posix()
    }
