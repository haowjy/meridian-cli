"""Round-trip tests for persisted launch request DTOs."""

import pytest
from pydantic import ValidationError

from meridian.lib.launch.request import (
    ExecutionBudget,
    LaunchArgvIntent,
    LaunchRuntime,
    RetryPolicy,
    SessionRequest,
    SpawnRequest,
)


def test_spawn_request_round_trip_json_with_all_fields() -> None:
    request = SpawnRequest(
        prompt="Implement phase 2",
        model="claude-sonnet-4",
        harness="claude",
        agent="coder",
        skills=("review", "verification"),
        extra_args=("--json", "--foo=bar"),
        mcp_tools=("github=gh",),
        sandbox="workspace-write",
        approval="auto",
        allowed_tools=("Read", "Write"),
        disallowed_tools=("Bash(rm)",),
        autocompact=65,
        effort="high",
        retry=RetryPolicy(max_attempts=3, backoff_secs=1.5),
        budget=ExecutionBudget(timeout_secs=600, kill_grace_secs=45),
        session=SessionRequest(
            continue_chat_id="c123",
            requested_harness_session_id="h456",
            continue_fork=True,
            source_execution_cwd="/tmp/source",
            source_claude_config_dir="/tmp/claude-config",
            forked_from_chat_id="c111",
            continue_harness="codex",
            continue_source_tracked=True,
            continue_source_ref="p42",
        ),
        context_from=("p9",),
        reference_files=("/tmp/a.md", "/tmp/b.md"),
        template_vars={"ticket": "123"},
        work_id_hint="launch-core-refactor",
        warning="normalized request warning",
        agent_metadata={"session_agent_path": "/tmp/agent.md"},
    )
    runtime = LaunchRuntime(
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
        unsafe_no_permissions=False,
        debug=True,
        report_output_path="/tmp/report.md",
        runtime_root="/tmp/state",
        project_paths_project_root="/tmp/repo",
        project_paths_execution_cwd="/tmp/repo",
    )

    encoded_request = request.model_dump_json()
    encoded_runtime = runtime.model_dump_json()

    decoded_request = SpawnRequest.model_validate_json(encoded_request)
    decoded_runtime = LaunchRuntime.model_validate_json(encoded_runtime)

    assert decoded_request == request
    assert decoded_runtime == runtime


def test_session_request_carries_all_continuation_fields() -> None:
    session = SessionRequest()
    assert set(session.model_dump()) == {
        "continue_chat_id",
        "requested_harness_session_id",
        "continue_fork",
        "source_execution_cwd",
        "source_claude_config_dir",
        "forked_from_chat_id",
        "continue_harness",
        "continue_source_tracked",
        "continue_source_ref",
        "primary_session_mode",
    }


def test_spawn_request_does_not_require_arbitrary_types() -> None:
    assert SpawnRequest.model_config.get("arbitrary_types_allowed") is not True
    assert SessionRequest.model_config.get("arbitrary_types_allowed") is not True
    assert LaunchRuntime.model_config.get("arbitrary_types_allowed") is not True


def test_spawn_request_rejects_bool_autocompact() -> None:
    with pytest.raises(ValidationError):
        SpawnRequest(
            prompt="Implement phase 2",
            autocompact=True,
        )
