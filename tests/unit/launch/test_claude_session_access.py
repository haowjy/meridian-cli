from pathlib import Path

from meridian.lib.launch.claude_session_access import (
    resolve_claude_session_access_source,
)
from meridian.lib.launch.request import SessionRequest


def test_resolve_claude_session_access_source_preserves_explicit_tracked_metadata() -> None:
    child_cwd = Path("/tmp/child")
    materialization_root = Path("/tmp/durable")
    target_config_root = Path("/tmp/target")

    resolved = resolve_claude_session_access_source(
        SessionRequest(
            requested_harness_session_id="session-1",
            source_execution_cwd="/tmp/source",
            source_claude_config_dir="/tmp/source-config",
            continue_source_tracked=True,
        ),
        child_cwd=child_cwd,
        materialization_root=materialization_root,
        target_config_root=target_config_root,
    )

    assert resolved.should_seed is True
    assert resolved.source_session_id == "session-1"
    assert resolved.source_cwd == Path("/tmp/source")
    assert resolved.source_config_root == Path("/tmp/source-config")
    assert resolved.target_config_root == target_config_root


def test_resolve_claude_session_access_source_uses_raw_untracked_fallback() -> None:
    child_cwd = Path("/tmp/child")
    materialization_root = Path("/tmp/durable")
    target_config_root = Path("/tmp/target")

    resolved = resolve_claude_session_access_source(
        SessionRequest(
            requested_harness_session_id="raw-session",
            continue_source_tracked=False,
            continue_source_ref="raw-session",
        ),
        child_cwd=child_cwd,
        materialization_root=materialization_root,
        target_config_root=target_config_root,
    )

    assert resolved.should_seed is True
    assert resolved.source_session_id == "raw-session"
    assert resolved.source_cwd == child_cwd
    assert resolved.source_config_root == materialization_root
    assert resolved.target_config_root == target_config_root
    

def test_resolve_claude_session_access_source_skips_tracked_request_without_source_cwd() -> None:
    resolved = resolve_claude_session_access_source(
        SessionRequest(
            requested_harness_session_id="tracked-session",
            continue_source_tracked=True,
        ),
        child_cwd=Path("/tmp/child"),
        materialization_root=Path("/tmp/durable"),
        target_config_root=Path("/tmp/target"),
    )

    assert resolved.should_seed is False
