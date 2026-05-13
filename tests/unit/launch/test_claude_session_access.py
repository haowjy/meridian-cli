# qa-validated: test-suite-redesign
"""Unit tests for Claude continue/fork session-access source resolution."""

from pathlib import Path

from meridian.lib.launch.claude_session_access import resolve_claude_session_access_source
from meridian.lib.launch.request import SessionRequest


def test_claude_session_access_prefers_source_control_root_over_legacy_execution_cwd() -> None:
    control_root = Path("/control/root")
    legacy_task_cwd = Path("/task/cwd")
    explicit_source_root = Path("/source/control/root")

    resolved = resolve_claude_session_access_source(
        SessionRequest(
            requested_harness_session_id="session-1",
            source_control_root=explicit_source_root.as_posix(),
            source_execution_cwd=legacy_task_cwd.as_posix(),
            source_claude_config_dir="/claude/config",
            continue_source_tracked=True,
        ),
        control_root=control_root,
        materialization_root=Path("/materialized/claude"),
        target_config_root=Path("/target/claude"),
    )

    assert resolved.should_seed is True
    assert resolved.source_session_id == "session-1"
    assert resolved.source_control_root == explicit_source_root
    assert resolved.target_control_root == control_root


def test_claude_session_access_falls_back_to_current_control_root_when_missing() -> None:
    control_root = Path("/control/root")

    resolved = resolve_claude_session_access_source(
        SessionRequest(
            requested_harness_session_id="session-2",
            continue_source_tracked=True,
        ),
        control_root=control_root,
        materialization_root=Path("/materialized/claude"),
        target_config_root=Path("/target/claude"),
    )

    assert resolved.should_seed is True
    assert resolved.source_control_root == control_root
    assert resolved.target_control_root == control_root


def test_claude_session_access_no_seed_without_requested_session_id() -> None:
    resolved = resolve_claude_session_access_source(
        SessionRequest(),
        control_root=Path("/control/root"),
        materialization_root=Path("/materialized/claude"),
        target_config_root=Path("/target/claude"),
    )

    assert resolved.should_seed is False
    assert resolved.source_session_id is None
