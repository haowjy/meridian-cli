from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections.permissions_codex import project_codex_network
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
    project_codex_spec_to_thread_request,
)
from meridian.lib.harness.projections.project_codex_subprocess import (
    project_codex_spec_to_cli_args,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.permissions import SPAWN_PLATFORM_COMMAND
from meridian.lib.safety.policy import (
    PermissionPolicy,
    PlatformCommand,
    PlatformRule,
    network_policy_name,
)


def _spec(*, interactive: bool) -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        harness=HarnessId.CODEX,
        prompt="test",
        interactive=interactive,
    )


def _effective_network(spec: ResolvedLaunchSpec) -> str:
    projection = project_codex_network(
        spec.permission_policy,
        native_config=spec.native_config,
    )
    return network_policy_name(projection.effective)


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
                ),
            )
        }
    )

    _, report = compile_codex_permissions(spec)

    line = next(item for item in report if item.element.startswith("platform: allow Meridian"))
    assert line.status == "unsupported"
    assert "run outside Codex's sandbox" not in line.note
    assert "exact 'meridian task-dir'" in line.note
    assert "prefix 'meridian spawn list'" in line.note


def test_codex_sandbox_report_describes_user_wide_spawn_escape() -> None:
    from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions

    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                platform_rules=(
                    PlatformRule(
                        kind="meridian-commands",
                        commands=(SPAWN_PLATFORM_COMMAND,),
                    ),
                ),
            )
        }
    )

    _, report = compile_codex_permissions(spec)

    line = next(item for item in report if item.element == "sandbox: workspace-write")
    assert "would write" in line.note
    assert "$CODEX_HOME/rules/meridian.rules" in line.note


@pytest.mark.parametrize(
    ("network", "expected"),
    [(None, False), ("allow", True), ("deny", False)],
    ids=["sandbox-default", "allow", "deny"],
)
def test_codex_workspace_network_reaches_every_launch_boundary(
    network: str | None, expected: bool
) -> None:
    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                network=network,
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

    network_override = f"sandbox_workspace_write.network_access={str(expected).lower()}"
    assert network_override in subprocess
    assert network_override in appserver
    assert thread["config"] == {
        "sandbox_workspace_write.writable_roots": ["/workspace/extra"],
        "sandbox_workspace_write.network_access": expected,
    }


def test_codex_allowlist_reaches_every_launch_boundary_as_one_domain_map() -> None:
    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                network={
                    "allow": ["github.com"],
                    "deny": ["example.com"],
                },
            )
        }
    )

    subprocess = project_codex_spec_to_cli_args(spec, base_command=("codex", "exec"))
    appserver = project_codex_spec_to_appserver_command(
        spec,
        host="127.0.0.1",
        port=1234,
    )
    _, thread = project_codex_spec_to_thread_request(spec, cwd="/workspace")
    domain_override = (
        'features.network_proxy.domains={ "github.com" = "allow", "example.com" = "deny" }'
    )

    for command in (subprocess, appserver):
        assert "sandbox_workspace_write.network_access=true" in command
        assert "features.network_proxy.enabled=true" in command
        assert command.count(domain_override) == 1
    assert thread["config"] == {
        "sandbox_workspace_write.network_access": True,
        "features.network_proxy.enabled": True,
        "features.network_proxy.domains": {
            "github.com": "allow",
            "example.com": "deny",
        },
    }


def test_codex_network_report_stays_on_meridian_projected_policy() -> None:
    from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions

    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                network={"allow": ["github.com"], "deny": ["evil.example"]},
            )
        }
    )
    permissions, report = compile_codex_permissions(spec)
    line = next(item for item in report if item.element.startswith("network: allowlist"))

    assert permissions.network.domains == (
        ("github.com", "allow"),
        ("evil.example", "deny"),
    )
    assert project_codex_network(
        spec.permission_policy,
        native_config=spec.native_config,
    ).effective == spec.permission_policy.network
    assert "features.network_proxy.domains=<inline map" in line.note


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
            )
        }
    )
    allowlisted = defaulted.model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="read-only",
                network={"allow": ["github.com"], "deny": []},
            )
        }
    )

    _, defaulted_report = compile_codex_permissions(defaulted)
    _, authored_report = compile_codex_permissions(authored)
    _, allowlisted_report = compile_codex_permissions(allowlisted)

    defaulted_line = next(line for line in defaulted_report if line.element == "network: deny")
    authored_line = next(line for line in authored_report if line.element == "network: allow")
    allowlisted_line = next(
        line for line in allowlisted_report if line.element.startswith("network: allowlist")
    )
    assert defaulted_line.status == "native"
    assert _effective_network(defaulted) == "deny"
    assert authored_line.status == "unsupported"
    assert allowlisted_line.status == "unsupported"
    assert _effective_network(authored) == "deny"


def test_codex_deprecated_native_network_override_wins_for_warning_release() -> None:
    from meridian.lib.harness.codex import CodexAdapter
    from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions

    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                network="allow",
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
    assert _effective_network(spec) == "deny"
    assert native_line.status == "native"
    warnings = CodexAdapter().launch_spec_warnings(spec)
    assert [warning.code for warning in warnings] == [
        "deprecated_codex_network_access_native_config"
    ]


def test_codex_native_network_override_matching_profile_reports_native() -> None:
    from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions

    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                network="deny",
            ),
            "native_config": {"sandbox_workspace_write": {"network_access": False}},
        }
    )

    _, report = compile_codex_permissions(spec)

    network_line = next(line for line in report if line.element == "network: deny")
    assert network_line.status == "native"


@pytest.mark.parametrize(
    "native_config",
    [
        {"sandbox_workspace_write": {"network_access": "false"}},
        {"sandbox_workspace_write": {"network_access": None}},
        {"sandbox_workspace_write": {"network_access": {}}},
        {"sandbox_workspace_write.network_access": False},
    ],
    ids=["non-boolean", "unencodable", "empty-mapping", "unaddressable"],
)
def test_codex_invalid_native_network_override_keeps_portable_deny(
    native_config: dict[str, object],
) -> None:
    from meridian.lib.harness.codex import CodexAdapter
    from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions

    spec = _spec(interactive=False).model_copy(
        update={
            "permission_policy": PermissionPolicy(
                sandbox="workspace-write",
                network="deny",
            ),
            "native_config": native_config,
        }
    )

    _, report = compile_codex_permissions(spec)
    subprocess = project_codex_spec_to_cli_args(spec, base_command=("codex", "exec"))
    appserver = project_codex_spec_to_appserver_command(
        spec,
        host="127.0.0.1",
        port=1234,
    )
    _, thread = project_codex_spec_to_thread_request(spec, cwd="/workspace")

    portable_deny = "sandbox_workspace_write.network_access=false"
    assert subprocess.count(portable_deny) == 1
    assert appserver.count(portable_deny) == 1
    assert thread["config"] == {"sandbox_workspace_write.network_access": False}
    assert _effective_network(spec) == "deny"
    network_line = next(line for line in report if line.element == "network: deny")
    native_line = next(line for line in report if line.element.startswith("native_config:"))
    assert network_line.status == "partial"
    assert native_line.status == "unsupported"
    assert "ignored" in native_line.note
    assert [warning.code for warning in CodexAdapter().launch_spec_warnings(spec)] == [
        "invalid_codex_network_access_native_config"
    ]


def test_spawn_capability_requires_the_canonical_spawn_command() -> None:
    unrelated_escape = PlatformCommand(
        command="other-tool launch",
        match="prefix",
        role="launch",
        runs_outside_sandbox=True,
    )

    assert not PermissionPolicy(
        platform_rules=(PlatformRule(kind="meridian-commands", commands=(unrelated_escape,)),)
    ).has_spawn_capability
    assert PermissionPolicy(
        platform_rules=(PlatformRule(kind="meridian-commands", commands=(SPAWN_PLATFORM_COMMAND,)),)
    ).has_spawn_capability
