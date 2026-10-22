from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian.lib.catalog.model_aliases import MarsResultCache
from meridian.lib.core.types import HarnessId
from meridian.lib.diagnostics import capture_library_diagnostics
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.bundle_adapter import LoadedSkillEntry
from meridian.lib.launch.composition import AvailableSkillEntry
from meridian.lib.launch.composition_spawn import compose_spawn_launch_surface
from meridian.lib.launch.context import (
    LaunchContext,
    PreparedLaunchSurface,
    RuntimeBindings,
    bind_launch_context,
    prepare_prompt_payload,
)
from meridian.lib.launch.launch_types import (
    CompositionWarning,
    PreparedLaunchContent,
    PreparedLaunchRuntimeSeeds,
    ResolvedExecutionPolicy,
)
from meridian.lib.launch.policy_snapshot import build_launch_policy_snapshot
from meridian.lib.launch.reference import load_reference_items
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest
from meridian.lib.state.paths import resolve_kb_dir
from meridian.lib.tools import NativeStatus, ToolAction, ToolRule


@dataclass(frozen=True, kw_only=True)
class BuiltLaunchContext(LaunchContext):
    """Launch context plus the prepared surface required by the primary runner."""

    prepared: PreparedLaunchSurface


def tool_rule(
    capability: str,
    action: ToolAction,
    *native: str,
    pattern: str | None = None,
    status: NativeStatus = "known",
) -> ToolRule:
    """One Mars-resolved tool rule; ``native`` are the harness's names, already scoped."""

    return ToolRule(
        capability=capability, pattern=pattern, action=action, native=native, native_status=status
    )


@dataclass(frozen=True)
class FakeBundleResult:
    model: str
    model_token: str
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    agent_source_path: str | None = None
    provider_constraint: str | None = None
    selection_report: dict[str, object] | None = None
    warnings: tuple[str, ...] = ()
    prompt_surface_inventory_prompt: str = ""
    tool_rules: tuple[ToolRule, ...] = ()
    tools_mcp: tuple[str, ...] = ()
    skills_loaded: tuple[LoadedSkillEntry, ...] = ()
    skills_available: tuple[AvailableSkillEntry, ...] = ()
    skills_missing: tuple[str, ...] = ()


def _direct_prepared_surface(
    *,
    request: SpawnRequest,
    project_root: Path,
    reference_anchor: Path,
    harness_registry: HarnessRegistry,
) -> PreparedLaunchSurface:
    """Build the test-only DIRECT surface for an already-resolved request."""

    harness_name = (request.harness or "").strip()
    if not harness_name:
        raise ValueError("SpawnRequest.harness is required.")
    try:
        harness_id = HarnessId(harness_name)
    except ValueError as exc:
        raise ValueError(f"Unknown harness '{harness_name}'.") from exc

    resolved_project_root = project_root.expanduser().resolve()
    loaded_references = load_reference_items(
        request.reference_files,
        reference_anchor=reference_anchor.expanduser().resolve(),
        kb_dir=resolve_kb_dir(resolved_project_root),
    )
    resolved_request = (
        request
        if request.launch_policy_snapshot is not None
        else request.model_copy(
            update={"launch_policy_snapshot": build_launch_policy_snapshot(request)}
        )
    )
    request_warning = (request.warning or "").strip()
    warnings = (
        (CompositionWarning(code="request_warning", message=request_warning),)
        if request_warning
        else ()
    )
    return PreparedLaunchSurface(
        request=resolved_request,
        harness=harness_registry.get_subprocess_harness(harness_id),
        composition_warnings=warnings,
        content=PreparedLaunchContent(
            final_prompt=request.prompt,
            loaded_references=loaded_references,
            prompt_payload=prepare_prompt_payload(
                adhoc_agent_payload=request.prompt_payload.adhoc_agent_payload,
                appended_system_prompt=request.prompt_payload.appended_system_prompt,
                user_turn_content=request.prompt_payload.user_turn_content,
            ),
        ),
        runtime_seeds=PreparedLaunchRuntimeSeeds(
            seed_harness_session_id=(
                (request.session.requested_harness_session_id or "").strip() or None
            )
        ),
        profile_tool_rules=(),
        model_selection=None,
        launch_request=resolved_request,
    )


def _attach_prepared(
    context: LaunchContext,
    prepared: PreparedLaunchSurface,
) -> BuiltLaunchContext:
    return BuiltLaunchContext(
        request=context.request,
        runtime=context.runtime,
        project_root=context.project_root,
        execution_cwd=context.execution_cwd,
        control_root=context.control_root,
        task_cwd=context.task_cwd,
        runtime_root=context.runtime_root,
        work_id=context.work_id,
        binding=context.binding,
        harness=context.harness,
        resolved_request=context.resolved_request,
        projected_content=context.projected_content,
        seed_harness_session_id=context.seed_harness_session_id,
        seed_harness_session_args=context.seed_harness_session_args,
        warnings=context.warnings,
        model_selection=context.model_selection,
        prepared=prepared,
    )


def build_launch_context(
    *,
    spawn_id: str,
    request: SpawnRequest,
    runtime: LaunchRuntime,
    harness_registry: HarnessRegistry,
    dry_run: bool = False,
    plan_overrides: Mapping[str, str] | None = None,
    runtime_work_id: str | None = None,
    forked_harness_session_id: str | None = None,
    cache: MarsResultCache | None = None,
) -> BuiltLaunchContext:
    """Test facade over the production prepare and bind phases."""

    with capture_library_diagnostics():
        resolved_config_root = Path(runtime.resolved_config_root).expanduser().resolve()
        resolved_requested_task_cwd = (
            Path(runtime.resolved_requested_task_cwd).expanduser().resolve()
            if runtime.resolved_requested_task_cwd is not None
            else Path(runtime.resolved_control_root).expanduser().resolve()
        )
        prepared = (
            _direct_prepared_surface(
                request=request,
                project_root=resolved_config_root,
                reference_anchor=resolved_requested_task_cwd,
                harness_registry=harness_registry,
            )
            if runtime.composition_surface == LaunchCompositionSurface.DIRECT
            else compose_spawn_launch_surface(
                request=request,
                runtime=runtime,
                harness_registry=harness_registry,
                dry_run=dry_run,
                runtime_work_id=runtime_work_id,
                cache=cache,
            )
        )
        context = bind_launch_context(
            prepared=prepared,
            bindings=RuntimeBindings(
                spawn_id=spawn_id,
                runtime_work_id=runtime_work_id,
                forked_harness_session_id=forked_harness_session_id,
                plan_overrides=dict(plan_overrides or {}),
                dry_run=dry_run,
            ),
            runtime=runtime,
            project_root=resolved_config_root,
            harness_registry=harness_registry,
        )
    return _attach_prepared(context, prepared)


def stub_bundle_request_and_resolve(
    monkeypatch: Any,
    *,
    model: str,
    harness: HarnessId,
    model_token: str | None = None,
    harness_model: str | None = None,
    execution_policy: ResolvedExecutionPolicy | None = None,
    provenance: dict[str, str] | None = None,
    warnings: tuple[str, ...] = (),
    prompt_surface_inventory_prompt: str = "",
    tool_rules: tuple[ToolRule, ...] = (),
    tools_mcp: tuple[str, ...] = (),
    skills_loaded: tuple[LoadedSkillEntry, ...] = (),
) -> list[bundle_adapter.BundleRequest]:
    captured_requests: list[bundle_adapter.BundleRequest] = []

    def _fake_request_and_resolve(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> FakeBundleResult:
        _ = harness_registry
        captured_requests.append(request)
        resolved_policy = execution_policy or ResolvedExecutionPolicy()
        resolved_provenance = provenance or {"model_source": "cli", "harness_source": "provider"}
        resolved_model_token = (
            model_token if model_token is not None else request.model_override or model
        )
        resolved_harness_model = harness_model if harness_model is not None else model
        return FakeBundleResult(
            model=model,
            model_token=resolved_model_token,
            harness=harness,
            harness_model=resolved_harness_model,
            execution_policy=resolved_policy,
            provenance=resolved_provenance,
            warnings=warnings,
            prompt_surface_inventory_prompt=prompt_surface_inventory_prompt,
            tool_rules=tool_rules,
            tools_mcp=tools_mcp,
            skills_loaded=skills_loaded,
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _fake_request_and_resolve)
    return captured_requests


def assert_task_cwd_instruction(system_prompt: str, task_dir: Path | str) -> None:
    """Assert the harness-aware task-dir guidance is present and unambiguous."""
    task_dir_posix = Path(task_dir).as_posix()
    assert "# Source-edit directory" in system_prompt
    assert "Use `MERIDIAN_PROJECT_DIR` for project coordination files" in system_prompt
    assert task_dir_posix in system_prompt
    assert "MERIDIAN_TASK_DIR" in system_prompt
    assert "project root, NOT" in system_prompt
    assert "relative paths resolve against the project root" in system_prompt
    assert "absolute paths" in system_prompt
    assert "`cd`" in system_prompt
    assert "Never assume cwd is the task dir" in system_prompt
