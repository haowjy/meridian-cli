from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeIdentity
from meridian.lib.harness.projections.project_pi_native_tui import (
    project_pi_native_tui_spec_to_cli_args,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def _spec(**overrides: object) -> ResolvedLaunchSpec:
    values: dict[str, object] = {
        "interactive": True,
        "prompt": "Reply exactly: hello",
        "permission_resolver": UnsafeNoOpPermissionResolver(_suppress_warning=True),
        "native_identity": NativeIdentity("pi", "create", "/tmp/pi", "new-id", None, None),
    }
    values.update(overrides)
    return ResolvedLaunchSpec(**values)


@pytest.mark.parametrize("operation", ["create", "resume", "fork"])
def test_interactive_prompt_is_last_after_identity(operation: str) -> None:
    identity = NativeIdentity(
        "pi",
        operation,
        "/tmp/pi",
        "target-id",
        "source-id" if operation != "create" else None,
        Path("/tmp/source.jsonl") if operation != "create" else None,
    )
    command = project_pi_native_tui_spec_to_cli_args(
        _spec(native_identity=identity), base_command=("pi",)
    )

    assert command[-2:] == ["--", "Reply exactly: hello"]
    if operation == "create":
        assert command[command.index("--session-id") + 1] == "target-id"
    elif operation == "resume":
        assert command[command.index("--session") + 1] == "/tmp/source.jsonl"
    else:
        assert command[command.index("--fork") + 1] == "/tmp/source.jsonl"
        assert command[command.index("--session-id") + 1] == "target-id"


def test_at_prefixed_prompt_is_not_interpreted_as_a_file() -> None:
    command = project_pi_native_tui_spec_to_cli_args(
        _spec(prompt="@literal text"), base_command=("pi",)
    )
    assert command[-2:] == ["--", " @literal text"]


def test_long_interactive_prompt_fails_clearly() -> None:
    with pytest.raises(ValueError, match=r"starting prompt is .*128 KiB"):
        project_pi_native_tui_spec_to_cli_args(
            _spec(prompt="x" * (128 * 1024)), base_command=("pi",)
        )
