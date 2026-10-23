from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
    project_codex_spec_to_thread_request,
)
from meridian.lib.harness.projections.project_codex_subprocess import (
    project_codex_spec_to_cli_args,
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


def test_codex_workspace_network_reaches_every_launch_boundary() -> None:
    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                network="allow",
                network_explicit=True,
            ),
            "projected_roots": (Path("/workspace/extra"),),
        }
    )

    subprocess = project_codex_spec_to_cli_args(spec, base_command=("codex", "exec"))
    appserver = project_codex_spec_to_appserver_command(
        spec,
        host="127.0.0.1",
        port=1234,
    )
    _, thread = project_codex_spec_to_thread_request(spec, cwd="/workspace")

    network_override = "sandbox_workspace_write.network_access=true"
    assert network_override in subprocess
    assert network_override in appserver
    assert thread["config"] == {
        "sandbox_workspace_write.writable_roots": ["/workspace/extra"],
        "sandbox_workspace_write.network_access": True,
    }


def test_codex_read_only_reports_only_authored_allow_as_unsupported() -> None:
    from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions

    defaulted = _spec(interactive=False).model_copy(
        update={"permission_policy": PermissionPolicy(sandbox="read-only")}
    )
    authored = defaulted.model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="read-only",
                network="allow",
                network_explicit=True,
            )
        }
    )

    _, defaulted_report = compile_codex_permissions(defaulted)
    _, authored_report = compile_codex_permissions(authored)

    defaulted_line = next(line for line in defaulted_report if line.element == "network: allow")
    authored_line = next(line for line in authored_report if line.element == "network: allow")
    assert defaulted_line.status == "native"
    assert "no authored allow request to report as a gap" in defaulted_line.note
    assert authored_line.status == "unsupported"


def test_codex_deprecated_native_network_override_wins_for_warning_release() -> None:
    from meridian.lib.harness.codex import CodexAdapter
    from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions

    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                network="allow",
                network_explicit=True,
            ),
            "native_config": {"sandbox_workspace_write": {"network_access": False}},
        }
    )

    subprocess = project_codex_spec_to_cli_args(spec, base_command=("codex", "exec"))
    appserver = project_codex_spec_to_appserver_command(
        spec,
        host="127.0.0.1",
        port=1234,
    )
    _, thread = project_codex_spec_to_thread_request(spec, cwd="/workspace")

    override = "sandbox_workspace_write.network_access=false"
    assert subprocess.count(override) == 1
    assert appserver.count(override) == 1
    assert "config" not in thread
    _, report = compile_codex_permissions(spec)
    network_line = next(line for line in report if line.element == "network: allow")
    native_line = next(
        line
        for line in report
        if line.element == "native_config: sandbox_workspace_write.network_access = false"
    )
    assert network_line.status == "partial"
    assert "takes precedence" in network_line.note
    assert "deprecated" in native_line.note
    warnings = CodexAdapter().launch_spec_warnings(spec)
    assert [warning.code for warning in warnings] == [
        "deprecated_codex_network_access_native_config"
    ]
