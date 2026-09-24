"""Generated Codex selection remains distinct from Meridian local-fork intent."""

from pathlib import Path

from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_thread_request,
)
from meridian.lib.harness.projections.project_codex_subprocess import (
    project_codex_spec_to_cli_args,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver


def _spec(session_id: str, *, fork: bool = False) -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        continue_session_id=session_id,
        continue_fork=fork,
        permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
    )


def test_generated_subprocess_resume_targets_exact_native_source() -> None:
    command = project_codex_spec_to_cli_args(
        _spec("123e4567-e89b-12d3-a456-426614174000"), base_command=("codex", "exec")
    )
    assert command[2:4] == ["resume", "123e4567-e89b-12d3-a456-426614174000"]


def test_generated_exec_roots_precede_resume_without_hoisting_raw_tail() -> None:
    source_id = "123e4567-e89b-12d3-a456-426614174000"
    command = project_codex_spec_to_cli_args(
        ResolvedLaunchSpec(
            continue_session_id=source_id,
            permission_resolver=TieredPermissionResolver(
                config=PermissionConfig(sandbox="workspace-write", approval="auto")
            ),
            projected_roots=(Path("/project/root"),),
            extra_args=("--model=gpt-5", "-c", "model_reasoning_effort=high"),
        ),
        base_command=("codex", "exec"),
    )

    root_index = command.index("--add-dir")
    resume_index = command.index("resume")
    raw_model_index = command.index("--model=gpt-5")
    assert command[root_index + 1] == "/project/root"
    assert root_index < resume_index < raw_model_index
    assert command[resume_index : resume_index + 2] == ["resume", source_id]
    assert command[raw_model_index:] == ["--model=gpt-5", "-c", "model_reasoning_effort=high"]
    assert command.index("--sandbox") < resume_index
    assert command.index('approval_policy="on-request"') < resume_index


def test_fresh_and_materialized_fork_target_keep_root_in_exec_prefix() -> None:
    target_id = "223e4567-e89b-12d3-a456-426614174000"
    for session_id in (None, target_id):
        command = project_codex_spec_to_cli_args(
            ResolvedLaunchSpec(
                continue_session_id=session_id,
                permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
                projected_roots=(Path("/project/root"),),
            ),
            base_command=("codex", "exec"),
        )
        root_index = command.index("--add-dir")
        if session_id is None:
            assert command[root_index : root_index + 2] == ["--add-dir", "/project/root"]
            assert "resume" not in command
        else:
            assert root_index < command.index("resume")
            assert command[command.index("resume") : command.index("resume") + 2] == [
                "resume",
                target_id,
            ]


def test_local_fork_projects_fork_source_not_generated_resume_target() -> None:
    method, payload = project_codex_spec_to_thread_request(
        _spec("123e4567-e89b-12d3-a456-426614174000", fork=True), cwd="/project"
    )
    assert method == "thread/fork"
    assert payload["threadId"] == "123e4567-e89b-12d3-a456-426614174000"
    assert payload["ephemeral"] is False


def test_materialized_local_fork_target_projects_as_plain_resume() -> None:
    method, payload = project_codex_spec_to_thread_request(
        _spec("223e4567-e89b-12d3-a456-426614174000"), cwd="/project"
    )
    assert method == "thread/resume"
    assert payload["threadId"] == "223e4567-e89b-12d3-a456-426614174000"
    assert "ephemeral" not in payload
