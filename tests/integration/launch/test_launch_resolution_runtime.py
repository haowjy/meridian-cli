# qa-validated: test-suite-redesign
"""Launch environment, warning, reference, and session identity tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.core.caller_boundary import (
    CALLER_BOUNDARY_ENV,
    CallerBoundary,
    current_pid_namespace,
)
from meridian.lib.core.context_ref import SpawnContextRef
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.bundle import project_effective_policy
from meridian.lib.harness.registry import (
    get_default_harness_registry,
)
from meridian.lib.launch import LaunchRequest as PrimaryLaunchRequest
from meridian.lib.launch import launch_primary
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.launch.plan import (
    build_primary_launch_runtime,
    build_primary_spawn_request,
)
from meridian.lib.launch.types import LaunchRequest
from meridian.lib.safety.policy import (
    Host,
    Hosts,
)
from meridian.lib.state import work_repository
from tests.support.fixtures import write_agent
from tests.support.launch import build_launch_context, stub_bundle_request_and_resolve

pytestmark = pytest.mark.slow


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize("override", [None, True, False])
def test_launch_direnv_policy_uses_snapshot_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: bool | None,
) -> None:
    import meridian.lib.launch.context as context_module

    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4",
        harness=HarnessId.CLAUDE,
    )
    monkeypatch.setenv("MERIDIAN_IGNORE_DIRENV", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-direnv-key")
    monkeypatch.setenv(CALLER_BOUNDARY_ENV, "inherited-boundary-must-not-survive")
    runtime = build_primary_launch_runtime(project_root=tmp_path)
    # A captured policy must not change when the ambient input disappears.
    monkeypatch.delenv("MERIDIAN_IGNORE_DIRENV")
    monkeypatch.setattr(
        context_module,
        "undo_direnv",
        lambda env: {key: value for key, value in env.items() if key != "ANTHROPIC_API_KEY"},
    )
    preview = build_launch_context(
        spawn_id="dry-run-direnv",
        request=build_primary_spawn_request(
            request=LaunchRequest(
                model="claude-sonnet-4",
                ignore_direnv=override,
            )
        ),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    env = preview.binding.environment.final_env
    expected = override is not False
    assert env["MERIDIAN_IGNORE_DIRENV"] == str(expected).lower()
    boundary = CallerBoundary.from_env_value(env[CALLER_BOUNDARY_ENV])
    assert boundary == CallerBoundary.from_policy(
        preview.binding.spec.permission_policy,
        effective=project_effective_policy(
            preview.harness.id,
            preview.binding.spec.permission_policy,
            native_config=preview.binding.spec.native_config,
        ),
        pid_namespace=current_pid_namespace(),
    )
    assert ("ANTHROPIC_API_KEY" not in env) == expected


def test_claude_user_allowed_domains_warns_without_changing_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    user_config = tmp_path / "claude-home"
    user_config.mkdir()
    (user_config / "settings.json").write_text(
        json.dumps(
            {
                "sandbox": {
                    "network": {
                        "allowedDomains": ["API.example.com.", "localhost"],
                        "deniedDomains": ["BLOCKED.example.com"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    project_settings = tmp_path / ".claude" / "settings.json"
    project_settings.parent.mkdir()
    project_settings.write_text(
        json.dumps({"sandbox": {"network": {"allowedDomains": ["project.example.com"]}}}),
        encoding="utf-8",
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4",
        harness=HarnessId.CLAUDE,
        execution_policy=ResolvedExecutionPolicy(
            sandbox="workspace-write",
            network="deny",
        ),
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", user_config.as_posix())

    preview = build_launch_context(
        spawn_id="dry-run-claude-network-settings",
        request=build_primary_spawn_request(request=LaunchRequest(model="claude-sonnet-4")),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    boundary = CallerBoundary.from_env_value(
        preview.binding.environment.final_env[CALLER_BOUNDARY_ENV]
    )
    assert boundary.network == Hosts()
    network_line = next(
        line for line in preview.enforcement_report() if line.element == "network: deny"
    )
    warning = next(warning for warning in preview.warnings if warning.code.endswith("may_widen"))
    assert (user_config / "settings.json").as_posix() in warning.message
    assert "beyond Meridian's projected policy" in warning.message
    assert "project.example.com" not in network_line.note


@pytest.mark.parametrize("port_key", ["httpProxyPort", "socksProxyPort"])
def test_claude_user_proxy_port_warns_without_changing_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    port_key: str,
) -> None:
    _write_minimal_mars_config(tmp_path)
    user_config = tmp_path / "claude-home"
    user_config.mkdir()
    settings_path = user_config / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "sandbox": {
                    "network": {
                        port_key: 3128.0,
                        "deniedDomains": ["blocked.example"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4",
        harness=HarnessId.CLAUDE,
        execution_policy=ResolvedExecutionPolicy(
            sandbox="workspace-write",
            network={"allow": ["github.com"], "deny": ["evil.example"]},
        ),
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", user_config.as_posix())

    preview = build_launch_context(
        spawn_id="dry-run-claude-user-proxy",
        request=build_primary_spawn_request(request=LaunchRequest(model="claude-sonnet-4")),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    boundary = CallerBoundary.from_env_value(
        preview.binding.environment.final_env[CALLER_BOUNDARY_ENV]
    )
    assert boundary.network == Hosts(
        allow=frozenset((Host("github.com"),)),
        deny=frozenset((Host("evil.example"),)),
    )
    network_line = next(
        line for line in preview.enforcement_report() if line.element.startswith("network: allow")
    )
    warning = next(warning for warning in preview.warnings if warning.code.endswith("may_widen"))
    assert settings_path.as_posix() in warning.message
    assert port_key in warning.message
    assert settings_path.as_posix() not in network_line.note


@pytest.mark.parametrize("config_source", ["home", "relative-home", "project", "parent"])
def test_codex_allow_domain_warns_without_changing_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_source: str,
) -> None:
    project_root = tmp_path / "nested" / "project" if config_source == "parent" else tmp_path
    project_root.mkdir(parents=True, exist_ok=True)
    _write_minimal_mars_config(project_root)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    if config_source == "project":
        config_path = project_root / ".codex" / "config.toml"
    elif config_source == "parent":
        config_path = project_root.parent / ".codex" / "config.toml"
    else:
        config_path = codex_home / "config.toml"
    config_path.parent.mkdir(exist_ok=True)
    config_path.write_text(
        """[features.network_proxy.domains]
"example.com" = "allow"
"blocked.example" = "deny"
""",
        encoding="utf-8",
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.6-luna",
        harness=HarnessId.CODEX,
        execution_policy=ResolvedExecutionPolicy(
            sandbox="workspace-write",
            network={"allow": ["github.com"], "deny": ["evil.example"]},
        ),
    )
    monkeypatch.setenv(
        "CODEX_HOME",
        codex_home.name if config_source == "relative-home" else codex_home.as_posix(),
    )

    preview = build_launch_context(
        spawn_id="dry-run-codex-user-domains",
        request=build_primary_spawn_request(request=LaunchRequest(model="gpt-5.6-luna")),
        runtime=build_primary_launch_runtime(project_root=project_root),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    boundary = CallerBoundary.from_env_value(
        preview.binding.environment.final_env[CALLER_BOUNDARY_ENV]
    )
    assert boundary.network == Hosts(
        allow=frozenset((Host("github.com"),)),
        deny=frozenset((Host("evil.example"),)),
    )
    network_line = next(
        line
        for line in preview.enforcement_report()
        if line.element.startswith("network: allowlist")
    )
    warning = next(warning for warning in preview.warnings if warning.code.endswith("may_widen"))
    assert config_path.as_posix() in warning.message
    assert "features.network_proxy.domains" in warning.message
    assert config_path.as_posix() not in network_line.note
    assert "example.com" not in " ".join(preview.binding.argv)
    assert "blocked.example" not in " ".join(preview.binding.argv)


@pytest.mark.parametrize("harness", [HarnessId.CLAUDE, HarnessId.CODEX])
def test_unreadable_user_network_config_warns_and_contributes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: HarnessId,
) -> None:
    _write_minimal_mars_config(tmp_path)
    config_home = tmp_path / f"{harness.value}-home"
    config_home.mkdir()
    config_name = "settings.json" if harness == HarnessId.CLAUDE else "config.toml"
    config_path = config_home / config_name
    config_path.write_text("not valid config }", encoding="utf-8")
    monkeypatch.setenv(
        "CLAUDE_CONFIG_DIR" if harness == HarnessId.CLAUDE else "CODEX_HOME",
        config_home.as_posix(),
    )
    model = "claude-sonnet-4" if harness == HarnessId.CLAUDE else "gpt-5.6-luna"
    stub_bundle_request_and_resolve(
        monkeypatch,
        model=model,
        harness=harness,
        execution_policy=ResolvedExecutionPolicy(
            sandbox="workspace-write",
            network={"allow": ["github.com"], "deny": []},
        ),
    )

    preview = build_launch_context(
        spawn_id=f"dry-run-{harness.value}-unreadable-config",
        request=build_primary_spawn_request(request=LaunchRequest(model=model)),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    boundary = CallerBoundary.from_env_value(
        preview.binding.environment.final_env[CALLER_BOUNDARY_ENV]
    )
    assert boundary.network == Hosts(allow=frozenset((Host("github.com"),)))
    warning = next(
        warning for warning in preview.warnings if warning.code.endswith("config_unreadable")
    )
    assert config_path.as_posix() in warning.message
    assert "may widen network access beyond Meridian's projected policy" in warning.message


def _prior(spawn_id: str = "p999", work_id: str | None = None) -> SpawnContextRef:
    """A resolved ``--from`` ref, as the cli or spawn prepare hands it to launch."""
    return SpawnContextRef(
        spawn_id=spawn_id,
        status="succeeded",
        agent="coder",
        desc="prior task",
        model="gpt-5.4-mini",
        harness="codex",
        report_text="prior context",
        work_id=work_id,
    )


def test_primary_launch_resolves_reference_files_from_work_task_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    task_dir = tmp_path / "task"
    from_work_task_dir = tmp_path / "from-work-task"
    project_root.mkdir()
    task_dir.mkdir()
    from_work_task_dir.mkdir()
    _write_minimal_mars_config(project_root)
    (project_root / "task-only.txt").write_text("project shadow", encoding="utf-8")
    (task_dir / "task-only.txt").write_text("task marker", encoding="utf-8")
    (from_work_task_dir / "task-only.txt").write_text("from-work marker", encoding="utf-8")
    state_dir = project_root / ".meridian"
    work_repository.ensure_work_item_metadata(state_dir, "task-work")
    work_repository.update_work_item_task_dir(
        state_dir,
        "task-work",
        task_dir=task_dir.as_posix(),
    )
    work_repository.ensure_work_item_metadata(state_dir, "from-work")
    work_repository.update_work_item_task_dir(
        state_dir,
        "from-work",
        task_dir=from_work_task_dir.as_posix(),
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )

    result = launch_primary(
        project_root=project_root,
        request=PrimaryLaunchRequest(
            model="gpt-5.4-mini",
            harness=HarnessId.CODEX.value,
            work_id="task-work",
            context_refs=(_prior("p123", "from-work"),),
            reference_files=("task-only.txt",),
            dry_run=True,
        ),
        harness_registry=get_default_harness_registry(),
    )

    assert result.launch_plan is not None
    prompt = result.launch_plan.attach_command[-1]
    assert (task_dir / "task-only.txt").as_posix() in prompt
    assert "task marker" in prompt
    assert from_work_task_dir.as_posix() not in prompt
    assert "from-work marker" not in prompt
    assert "project shadow" not in prompt


def test_primary_launch_resolves_reference_files_from_explicit_task_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    task_dir = tmp_path / "task"
    project_root.mkdir()
    task_dir.mkdir()
    _write_minimal_mars_config(project_root)
    (project_root / "task-only.txt").write_text("project shadow", encoding="utf-8")
    (task_dir / "task-only.txt").write_text("task marker", encoding="utf-8")
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )

    result = launch_primary(
        project_root=project_root,
        request=PrimaryLaunchRequest(
            model="gpt-5.4-mini",
            harness=HarnessId.CODEX.value,
            task_dir=task_dir.as_posix(),
            reference_files=("task-only.txt",),
            dry_run=True,
        ),
        harness_registry=get_default_harness_registry(),
    )

    assert result.launch_plan is not None
    prompt = result.launch_plan.attach_command[-1]
    assert (task_dir / "task-only.txt").as_posix() in prompt
    assert "task marker" in prompt
    assert "project shadow" not in prompt


def test_opencode_from_creates_fresh_session_with_context_in_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R7: ``--from`` starts a fresh session and injects context via the prompt."""

    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".opencode"]\n',
        encoding="utf-8",
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="openai/gpt-5.5",
        harness=HarnessId.OPENCODE,
    )
    write_agent(tmp_path, name="dev-orchestrator", model="openai/gpt-5.5")

    preview = build_launch_context(
        spawn_id="dry-run-opencode-from",
        request=build_primary_spawn_request(
            request=LaunchRequest(
                model="openai/gpt-5.5",
                harness=HarnessId.OPENCODE.value,
                agent="dev-orchestrator",
                context_refs=(_prior(),),
            )
        ),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.harness.id is HarnessId.OPENCODE
    assert preview.binding.spec.continue_session_id is None
    assert preview.projected_content is not None
    assert "prior context" in preview.projected_content.user_turn_content
    assert "prior context" not in preview.projected_content.system_prompt
