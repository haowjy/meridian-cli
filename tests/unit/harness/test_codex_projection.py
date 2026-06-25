"""Codex subprocess projection and bind-time report path tests."""

from __future__ import annotations

from pathlib import Path

import meridian.lib.harness.cursor as cursor_harness
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.projections.project_codex_streaming import (
    APPSERVER_ACCEPTED_FLAGS,
    project_codex_spec_to_appserver_command,
)
from meridian.lib.harness.projections.project_codex_subprocess import (
    project_codex_spec_to_cli_args,
)
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.composition_spawn import bind_spawn_launch_context
from meridian.lib.launch.constants import (
    BASE_COMMAND_CODEX_SUBPROCESS,
    DRY_RUN_REPORT_PATH,
    REPORT_FILENAME,
)
from meridian.lib.launch.context import RuntimeBindings
from meridian.lib.launch.plan import build_spawn_mars_runtime
from meridian.lib.launch.request import LaunchArgvIntent
from meridian.lib.ops.runtime import build_runtime
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver
from meridian.lib.state.paths import resolve_spawn_log_dir
from tests.support.launch import stub_bundle_request_and_resolve


def test_codex_adapter_leaves_report_output_path_none(tmp_path: Path) -> None:
    adapter = CodexAdapter()

    spec = adapter.resolve_launch_spec(
        SpawnParams(prompt="do work"),
        TieredPermissionResolver(config=PermissionConfig()),
    )

    assert spec.report_output_path is None


def test_codex_subprocess_projection_emits_no_o_without_report_output_path() -> None:
    adapter = CodexAdapter()
    spec = adapter.resolve_launch_spec(
        SpawnParams(prompt="do work"),
        TieredPermissionResolver(config=PermissionConfig()),
    )

    command = project_codex_spec_to_cli_args(
        spec,
        base_command=BASE_COMMAND_CODEX_SUBPROCESS,
    )

    assert "-o" not in command


def test_codex_subprocess_projection_emits_o_flag_for_report_output_path(
    tmp_path: Path,
) -> None:
    report_path = str(tmp_path / "spawns" / "abc" / "report.md")
    spec = CodexAdapter().resolve_launch_spec(
        SpawnParams(prompt="do work"),
        TieredPermissionResolver(config=PermissionConfig()),
    ).model_copy(update={"report_output_path": report_path})

    command = project_codex_spec_to_cli_args(
        spec,
        base_command=BASE_COMMAND_CODEX_SUBPROCESS,
    )

    o_index = command.index("-o")
    assert command[o_index + 1] == report_path


def test_codex_subprocess_projection_emits_search_when_web_search_enabled() -> None:
    spec = CodexAdapter().resolve_launch_spec(
        SpawnParams(prompt="research topic"),
        TieredPermissionResolver(config=PermissionConfig()),
    ).model_copy(update={"web_search_enabled": True})

    command = project_codex_spec_to_cli_args(
        spec,
        base_command=BASE_COMMAND_CODEX_SUBPROCESS,
    )

    assert "tools.web_search=true" in command


def test_codex_subprocess_projection_omits_search_without_web_grant() -> None:
    spec = CodexAdapter().resolve_launch_spec(
        SpawnParams(prompt="do work"),
        TieredPermissionResolver(config=PermissionConfig()),
    )

    command = project_codex_spec_to_cli_args(
        spec,
        base_command=BASE_COMMAND_CODEX_SUBPROCESS,
    )

    assert "tools.web_search=true" not in command


def test_codex_streaming_projection_emits_search_when_web_search_enabled() -> None:
    spec = CodexAdapter().resolve_launch_spec(
        SpawnParams(prompt="research topic"),
        TieredPermissionResolver(config=PermissionConfig()),
    ).model_copy(update={"web_search_enabled": True})

    command = project_codex_spec_to_appserver_command(spec, host="127.0.0.1", port=8765)

    assert "tools.web_search=true" in command


def test_appserver_command_emits_only_appserver_accepted_flags() -> None:
    """Guard: `codex app-server` accepts a tiny flag set — everything else must be
    `-c key=value` (or per-WS-request). A bare flag valid on `codex exec` but not
    app-server (e.g. `--search`) crashes the streaming transport, and that's
    invisible to dry-runs and unit-level projection tests. Build a fully-featured
    app-server command and assert every dash-led token is app-server-accepted.
    """
    spec = CodexAdapter().resolve_launch_spec(
        SpawnParams(prompt="research topic"),
        TieredPermissionResolver(
            config=PermissionConfig(sandbox="workspace-write", approval="never")
        ),
    ).model_copy(
        update={
            "web_search_enabled": True,
            "mcp_tools": ("demo=run-demo",),
            "projected_roots": (Path("/tmp/workspace-root"),),
        }
    )

    command = project_codex_spec_to_appserver_command(spec, host="127.0.0.1", port=8765)

    # Sanity: the features we set must actually have produced `-c` overrides, else
    # the guard would pass vacuously.
    assert "tools.web_search=true" in command
    assert any(tok.startswith("sandbox_mode=") for tok in command)
    assert any(tok.startswith("approval_policy=") for tok in command)
    assert any(tok.startswith("mcp.servers.") for tok in command)
    assert any(tok.startswith("sandbox_workspace_write.writable_roots=") for tok in command)

    offenders: list[str] = []
    skip_next = False
    for token in command:
        if skip_next:
            # `-c`/`--config` payload is `key=value`, not a flag.
            skip_next = False
            continue
        if token in ("-c", "--config"):
            skip_next = True
            continue
        if token.startswith("-") and token not in APPSERVER_ACCEPTED_FLAGS:
            offenders.append(token)

    assert not offenders, (
        f"codex app-server rejects these bare flags: {offenders}. "
        "Pass them as `-c key=value` config overrides instead."
    )


def test_bind_codex_argv_includes_search_when_bundle_grants_web_search(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (project_root / ".meridian").mkdir()
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
        tools_allowed=("web_search",),
    )
    runtime = build_runtime(project_root)
    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="research topic",
            model="gpt-5.4-mini",
            harness="codex",
            project_root=str(project_root),
        ),
        runtime=runtime,
    )
    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=project_root / ".meridian",
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.REQUIRED,
    )
    bound = bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(spawn_id="p-codex-web", dry_run=False),
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )

    assert bound.binding.spec.web_search_enabled is True
    assert "tools.web_search=true" in bound.binding.argv


def test_bind_codex_argv_omits_search_without_web_grant(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (project_root / ".meridian").mkdir()
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    runtime = build_runtime(project_root)
    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="do work",
            model="gpt-5.4",
            harness="codex",
            project_root=str(project_root),
        ),
        runtime=runtime,
    )
    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=project_root / ".meridian",
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.REQUIRED,
    )
    bound = bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(spawn_id="p-codex-no-web", dry_run=False),
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )

    assert bound.binding.spec.web_search_enabled is False
    assert "tools.web_search=true" not in bound.binding.argv


def test_bind_sets_codex_report_output_path_from_spawn_log_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (project_root / ".meridian").mkdir()
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    runtime = build_runtime(project_root)
    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="do work",
            model="gpt-5.4",
            harness="codex",
            project_root=str(project_root),
        ),
        runtime=runtime,
    )
    spawn_id = "p-codex-report"
    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=project_root / ".meridian",
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.REQUIRED,
    )
    bound = bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(spawn_id=spawn_id, dry_run=False),
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )

    expected_report = resolve_spawn_log_dir(project_root, spawn_id) / REPORT_FILENAME
    assert bound.binding.spec.report_output_path == expected_report.as_posix()
    assert "-o" in bound.binding.argv
    o_index = bound.binding.argv.index("-o")
    assert bound.binding.argv[o_index + 1] == expected_report.as_posix()


def test_bind_codex_dry_run_uses_report_path_sentinel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (project_root / ".meridian").mkdir()
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    runtime = build_runtime(project_root)
    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="do work",
            model="gpt-5.4",
            harness="codex",
            project_root=str(project_root),
            dry_run=True,
        ),
        runtime=runtime,
    )
    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=project_root / ".meridian",
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.REQUIRED,
    )
    bound = bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(spawn_id="dry-run", dry_run=True),
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )

    assert bound.binding.spec.report_output_path == DRY_RUN_REPORT_PATH
    o_index = bound.binding.argv.index("-o")
    assert bound.binding.argv[o_index + 1] == DRY_RUN_REPORT_PATH


def test_bind_non_codex_leaves_report_output_path_none(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (project_root / ".meridian").mkdir()
    monkeypatch.setattr(cursor_harness.shutil, "which", lambda _command: "/usr/bin/cursor")
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="opus47",
        harness=HarnessId.CURSOR,
    )
    runtime = build_runtime(project_root)
    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="do work",
            model="opus47",
            harness="cursor",
            project_root=str(project_root),
        ),
        runtime=runtime,
    )
    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=project_root / ".meridian",
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.REQUIRED,
    )
    bound = bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(spawn_id="p-cursor", dry_run=False),
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )

    assert bound.binding.spec.report_output_path is None
    assert "-o" not in bound.binding.argv
