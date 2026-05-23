"""Cursor subprocess projection tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from meridian.lib.harness.projections.project_cursor import (
    HarnessCapabilityMismatch,
    project_cursor_spec_to_cli_args,
)
from meridian.lib.launch.constants import BASE_COMMAND_CURSOR_SUBPROCESS
from meridian.lib.launch.launch_types import PermissionResolver, ResolvedLaunchSpec
from meridian.lib.safety.permissions import ApprovalMode, PermissionConfig


class _Resolver(PermissionResolver):
    def __init__(self, *, approval: str) -> None:
        self._config = PermissionConfig(approval=cast("ApprovalMode", approval))

    @property
    def config(self) -> PermissionConfig:
        return self._config

    def resolve_flags(self) -> tuple[str, ...]:
        return ("--dangerously-bypass-approvals-and-sandbox",)


@pytest.mark.parametrize(
    ("approval", "expected_flag"),
    [
        ("default", None),
        ("confirm", None),
        ("auto", "--force"),
        ("yolo", "--yolo"),
    ],
)
def test_cursor_projection_maps_approval_flags_and_keeps_prompt_last(
    approval: str,
    expected_flag: str | None,
    tmp_path: Path,
) -> None:
    task_cwd = str(tmp_path / "task-cwd")
    spec = ResolvedLaunchSpec(
        harness="cursor",
        model="composer-2.5",
        prompt="Reply with exactly OK",
        permission_resolver=_Resolver(approval=approval),
        task_cwd=task_cwd,
        extra_args=("--foo", "bar"),
    )

    command = project_cursor_spec_to_cli_args(spec, base_command=BASE_COMMAND_CURSOR_SUBPROCESS)

    assert command[:5] == ["cursor", "agent", "--print", "--output-format", "stream-json"]
    assert command[5] == "--trust"
    assert command[command.index("--model") + 1] == "composer-2.5"
    assert command[command.index("--workspace") + 1] == task_cwd
    if expected_flag is None:
        assert "--force" not in command
        assert "--yolo" not in command
    else:
        assert expected_flag in command
    # Projection must ignore resolver-provided shared flags.
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[-3:] == ["--foo", "bar", "Reply with exactly OK"]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"continue_session_id": "ses-123"}, "session resume"),
        ({"continue_session_id": "ses-123", "continue_fork": True}, "continue_fork"),
        ({"mcp_tools": ("fs",)}, "mcp_tools"),
        ({"interactive": True}, "interactive mode"),
    ],
)
def test_cursor_projection_rejects_mvp_unsupported_fields(
    updates: dict[str, object],
    message: str,
) -> None:
    base_spec = ResolvedLaunchSpec(
        harness="cursor",
        prompt="hello",
        permission_resolver=_Resolver(approval="default"),
    )

    with pytest.raises(HarnessCapabilityMismatch, match=message):
        project_cursor_spec_to_cli_args(
            base_spec.model_copy(update=updates),
            base_command=BASE_COMMAND_CURSOR_SUBPROCESS,
        )


def test_cursor_projection_ignores_projected_roots_for_mvp(tmp_path: Path) -> None:
    task_cwd = str(tmp_path / "task-cwd")
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    spec = ResolvedLaunchSpec(
        harness="cursor",
        prompt="hello",
        task_cwd=task_cwd,
        projected_roots=(root_a, root_b),
        permission_resolver=_Resolver(approval="default"),
    )

    command = project_cursor_spec_to_cli_args(spec, base_command=BASE_COMMAND_CURSOR_SUBPROCESS)

    assert command[command.index("--workspace") + 1] == task_cwd
    assert str(root_a) not in command
    assert str(root_b) not in command
