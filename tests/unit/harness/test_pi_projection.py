"""Pi subprocess projection tests."""

from __future__ import annotations

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections.project_pi_subprocess import project_pi_spec_to_cli_args
from meridian.lib.launch.constants import BASE_COMMAND_PI_SUBPROCESS
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def test_pi_subprocess_projection_includes_isolation_resume_and_inline_system_prompt() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        model="anthropic/claude-sonnet-4",
        effort="high",
        prompt="solve this",
        continue_session_id="019e3113",
        continue_fork=True,
        appended_system_prompt="You are meridian worker",
        extra_args=("--provider", "anthropic"),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    command = project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)

    assert command == [
        "meridian-pi",
        "-p",
        "--mode",
        "json",
        "--model",
        "anthropic/claude-sonnet-4:high",
        "--append-system-prompt",
        "You are meridian worker",
        "--fork",
        "019e3113",
        "--no-extensions",
        "--no-skills",
        "--no-context-files",
        "--no-prompt-templates",
        "--provider",
        "anthropic",
        "solve this",
    ]


def test_pi_subprocess_projection_uses_session_without_fork_when_continue_fork_false() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        continue_session_id="abc1234",
        continue_fork=False,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    command = project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)

    assert "--session" in command
    assert "--fork" not in command
