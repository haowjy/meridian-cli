from __future__ import annotations

import json

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import PermissionPolicy, PlatformCommand, PlatformRule


def _spec(*, interactive: bool) -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        harness=HarnessId.CODEX,
        prompt="test",
        interactive=interactive,
    )


def test_interactive_codex_app_server_injects_idle_notify() -> None:
    command = project_codex_spec_to_appserver_command(
        _spec(interactive=True),
        host="127.0.0.1",
        port=1234,
    )

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


def test_noninteractive_codex_app_server_does_not_inject_idle_notify() -> None:
    command = project_codex_spec_to_appserver_command(
        _spec(interactive=False),
        host="127.0.0.1",
        port=1234,
    )

    assert not any(argument.startswith("notify=") for argument in command)


def test_codex_report_names_unsupported_meridian_command_match_forms() -> None:
    from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions

    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                platform_rules=(
                    PlatformRule(
                        kind="meridian-commands",
                        commands=(
                            PlatformCommand(command="meridian task-dir", match="exact"),
                            PlatformCommand(command="meridian spawn list", match="prefix"),
                        ),
                    ),
                )
            )
        }
    )

    _, report = compile_codex_permissions(spec)

    line = next(item for item in report if item.element.startswith("platform: allow Meridian"))
    assert line.status == "partial"
    assert "`meridian spawn` run outside Codex's sandbox" in line.note
    assert "exact 'meridian task-dir'" in line.note
    assert "prefix 'meridian spawn list'" in line.note


def test_codex_sandbox_report_describes_user_wide_spawn_escape() -> None:
    from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions

    spec = _spec(interactive=False).model_copy(
        update={"permission_policy": PermissionPolicy(sandbox="workspace-write")}
    )

    _, report = compile_codex_permissions(spec)

    line = next(item for item in report if item.element == "sandbox: workspace-write")
    assert "`meridian spawn` runs outside Codex's sandbox" in line.note
    assert "$CODEX_HOME/rules/meridian.rules" in line.note
    assert "every Codex session of this user" in line.note
