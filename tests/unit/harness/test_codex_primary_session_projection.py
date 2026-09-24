"""Generated Codex selection remains distinct from Meridian local-fork intent."""

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
