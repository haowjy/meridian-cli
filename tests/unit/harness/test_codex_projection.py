from __future__ import annotations

import json

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver


def _spec(*, interactive: bool) -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        harness=HarnessId.CODEX,
        prompt="test",
        permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
        interactive=interactive,
    )


@pytest.mark.parametrize("interactive", [True, False])
def test_codex_app_server_injects_idle_notify_only_when_interactive(
    interactive: bool,
) -> None:
    command = project_codex_spec_to_appserver_command(
        _spec(interactive=interactive),
        host="127.0.0.1",
        port=1234,
    )

    if interactive:
        notify_override = next(argument for argument in command if argument.startswith("notify="))
        key, separator, raw_command = notify_override.partition("=")
        assert key == "notify"
        assert separator == "="
        assert json.loads(raw_command) == [
            "meridian",
            "idle",
            "event",
            "--harness",
            "codex",
        ]
    else:
        assert not any(argument.startswith("notify=") for argument in command)
