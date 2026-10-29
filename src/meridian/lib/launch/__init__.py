"""Public launch API."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.lib.launch.launch_types import summarize_composition_warnings
from meridian.lib.state.paths import LaunchPaths, resolve_project_paths

if TYPE_CHECKING:
    from meridian.lib.harness.registry import HarnessRegistry
    from meridian.lib.launch.command import (
        normalize_system_prompt_passthrough_args,
    )
    from meridian.lib.launch.context import (
        PreparedLaunchSurface,
        PreparedPolicySurface,
        RuntimeBindings,
        bind_launch_context,
        compile_prepared_policy_surface,
        prepare_launch_surface,
    )
    from meridian.lib.launch.continue_launch import ContinueHold
    from meridian.lib.launch.materialize import MaterializedLaunch, materialize_harness
    from meridian.lib.launch.policies import (
        ResolvedLaunchPolicy,
        ResolvedPolicies,
        SurfacePolicyInput,
        resolve_launch_policy,
    )
    from meridian.lib.launch.process import ProcessOutcome, run_harness_process
    from meridian.lib.launch.resolve import (
        ResolvedSkills,
        load_agent_profile_with_fallback,
        resolve_harness,
    )
    from meridian.lib.launch.types import (
        LaunchRequest,
        LaunchResult,
        PrimarySessionMetadata,
        SessionIntent,
        SessionMode,
        build_primary_prompt,
    )


def _explicit_work_id_for_launch(request: LaunchRequest) -> str | None:
    """Resolve a primary's ``--work``; ``current`` names the session's item."""

    from meridian.lib.ops.work_attachment import resolve_current_work_alias

    return resolve_current_work_alias(request.work_id, usage="meridian --work <work_id>")


def _resolve_work_id_for_launch(project_root: Path, request: LaunchRequest) -> str | None:
    """Materialize a primary's explicit ``--work`` before entering the launch layer."""

    from meridian.lib.ops.work_attachment import materialize_launch_work_id

    project_local_root = resolve_project_paths(project_root).root_dir
    return materialize_launch_work_id(
        project_local_root,
        explicit_work_id=_explicit_work_id_for_launch(request),
    )


def _preview_work_id_for_launch(
    request: LaunchRequest,
    *,
    context_work_id: str | None,
) -> str | None:
    """Normalize explicit or inherited work for dry-run preview without creating it."""

    from meridian.lib.ops.work_attachment import preview_launch_work_id

    return preview_launch_work_id(
        explicit_work_id=_explicit_work_id_for_launch(request),
        context_work_id=context_work_id,
    )


def launch_primary(
    *,
    project_root: Path,
    request: LaunchRequest,
    harness_registry: HarnessRegistry,
    hold: ContinueHold | None = None,
) -> LaunchResult:
    """Launch the primary agent process and wait for exit.

    ``hold``: the finished spawn this launch reopens, its lock held; the runner commits
    the next launch to it instead of creating a spawn.
    """

    from meridian.lib.catalog.catalog_session import CatalogSession
    from meridian.lib.config.project_root import resolve_project_root_resolution
    from meridian.lib.core.context import resolve_runtime_context
    from meridian.lib.ops.runtime import resolve_runtime_root_for_read

    from .context import (
        RuntimeBindings,
        bind_launch_context,
        compile_prepared_policy_surface,
        prepare_launch_surface,
    )
    from .cwd import inheritable_task_dir
    from .plan import build_primary_launch_runtime, build_primary_spawn_request
    from .process import run_harness_process
    from .types import LaunchResult

    resolved_project_root = resolve_project_root_resolution(project_root).project_root
    explicit_work_id = _explicit_work_id_for_launch(request)
    runtime_root_for_context = resolve_runtime_root_for_read(resolved_project_root)
    runtime_context = resolve_runtime_context(
        project_root=resolved_project_root,
        runtime_root=runtime_root_for_context,
    )
    inherit_ambient_work = request.session_mode.value != "resume"
    inherited_task_dir = (
        None
        if request.session_mode.value == "resume"
        else inheritable_task_dir(
            resolved_project_root,
            str(runtime_context.spawn_id) if runtime_context.spawn_id is not None else None,
        )
    )
    ambient_work_id = (
        None
        if explicit_work_id is not None or not inherit_ambient_work
        else runtime_context.work_id
    )
    resolved_project_paths = resolve_project_paths(resolved_project_root)
    project_state_dir = resolved_project_paths.root_dir
    from meridian.lib.launch.resolution import resolve_launch_inputs

    launch_resolution = resolve_launch_inputs(
        authority_root=resolved_project_root,
        project_state_dir=project_state_dir,
        context_refs=request.context_refs,
        reference_files=request.reference_files,
        explicit_task_dir=request.task_dir,
        explicit_work_id=explicit_work_id,
        inherited_task_dir=inherited_task_dir,
        ambient_work_id=ambient_work_id,
        caller_cwd=Path.cwd(),
    )
    preview_work_id = _preview_work_id_for_launch(
        request,
        context_work_id=launch_resolution.context_work_id,
    )

    runtime = build_primary_launch_runtime(
        project_root=resolved_project_root,
        execution_cwd=resolved_project_root,
        dry_run=request.dry_run,
    )
    if request.include_bootstrap_documents:
        from meridian.lib.catalog.bootstrap import BootstrapRegistry
        from meridian.lib.catalog.mars_roots import resolve_mars_roots

        mars_root = resolve_mars_roots(resolved_project_root).primary
        request = request.model_copy(
            update={
                "supplemental_prompt_documents": (
                    *request.supplemental_prompt_documents,
                    *BootstrapRegistry(mars_root / ".mars").load_all(),
                )
            }
        )
    resolved_work_id = (
        preview_work_id
        if request.dry_run
        else _resolve_work_id_for_launch(resolved_project_root, request)
    )

    runtime = runtime.model_copy(update=launch_resolution.runtime_updates)
    effective_work_id = resolved_work_id or launch_resolution.effective_work_id
    request_updates = dict(launch_resolution.request_updates)
    request_updates["work_id_hint"] = effective_work_id
    spawn_request = build_primary_spawn_request(request=request)
    request_updates["warning"] = (
        "\n".join(
            warning
            for warning in (
                spawn_request.warning,
                launch_resolution.task_cwd_resolution.warning,
            )
            if warning
        )
        or None
    )
    spawn_request = spawn_request.model_copy(update=request_updates)
    prepared_policy = compile_prepared_policy_surface(
        request=spawn_request,
        runtime=runtime,
        project_root=resolved_project_root,
        harness_registry=harness_registry,
        catalog=CatalogSession(resolved_project_root),
        dry_run=request.dry_run,
    )
    prepared = prepare_launch_surface(
        request=spawn_request,
        runtime=runtime,
        prepared_policy=prepared_policy,
    )
    preview_context = bind_launch_context(
        prepared=prepared,
        bindings=RuntimeBindings(
            launch=LaunchPaths.preview(Path(runtime.runtime_root)),
            runtime_work_id=effective_work_id,
            dry_run=True,
        ),
        runtime=runtime,
        project_root=resolved_project_root,
        harness_registry=harness_registry,
    )
    warning = summarize_composition_warnings(preview_context.warnings)

    if request.dry_run:
        from meridian.lib.harness.bundle import project_managed_primary_preview

        launch_plan = project_managed_primary_preview(
            preview_context.harness.id,
            preview_context.binding.spec,
            project_root=resolved_project_root,
        )
        if launch_plan is not None:
            launch_plan = launch_plan.model_copy(update={"requested_model": request.model})
        return LaunchResult(
            launch_plan=launch_plan,
            command=() if launch_plan is not None else preview_context.binding.argv,
            permission_report=preview_context.enforcement_report(),
            exit_code=0,
            continue_ref=None,
            continue_chat_id=None,
            warning=warning,
            terminal_surface_mode=(
                preview_context.harness.contract.capabilities.terminal_surface_mode.value
            ),
        )

    outcome = run_harness_process(
        preview_context,
        harness_registry,
        prepared=prepared,
        hold=hold,
    )
    continue_ref = outcome.resolved_harness_session_id.strip() or None

    return LaunchResult(
        command=outcome.command,
        exit_code=outcome.exit_code,
        continue_ref=continue_ref,
        continue_chat_id=outcome.chat_id,
        primary_spawn_id=outcome.primary_spawn_id,
        warning=warning,
        launcher_diagnostics_path=outcome.launcher_diagnostics_path,
    )


def __getattr__(name: str) -> Any:
    """Lazily load launch exports to avoid import-time cycles."""

    mapping: dict[str, tuple[str, str]] = {
        "LaunchRequest": (".types", "LaunchRequest"),
        "LaunchResult": (".types", "LaunchResult"),
        "MaterializedLaunch": (".materialize", "MaterializedLaunch"),
        "PrimarySessionMetadata": (".types", "PrimarySessionMetadata"),
        "ProcessOutcome": (".process", "ProcessOutcome"),
        "PreparedLaunchSurface": (".context", "PreparedLaunchSurface"),
        "PreparedPolicySurface": (".context", "PreparedPolicySurface"),
        "ResolvedLaunchPolicy": (".policies", "ResolvedLaunchPolicy"),
        "ResolvedPolicies": (".policies", "ResolvedPolicies"),
        "SurfacePolicyInput": (".policies", "SurfacePolicyInput"),
        "ResolvedSkills": (".resolve", "ResolvedSkills"),
        "RuntimeBindings": (".context", "RuntimeBindings"),
        "SessionIntent": (".types", "SessionIntent"),
        "SessionMode": (".types", "SessionMode"),
        "bind_launch_context": (".context", "bind_launch_context"),
        "compile_prepared_policy_surface": (".context", "compile_prepared_policy_surface"),
        "build_primary_prompt": (".types", "build_primary_prompt"),
        "load_agent_profile_with_fallback": (".resolve", "load_agent_profile_with_fallback"),
        "normalize_system_prompt_passthrough_args": (
            ".command",
            "normalize_system_prompt_passthrough_args",
        ),
        "prepare_launch_surface": (".context", "prepare_launch_surface"),
        "materialize_harness": (".materialize", "materialize_harness"),
        "resolve_harness": (".resolve", "resolve_harness"),
        "resolve_launch_policy": (".policies", "resolve_launch_policy"),
        "run_harness_process": (".process", "run_harness_process"),
    }
    try:
        module_name, attr_name = mapping[name]
    except KeyError as exc:
        raise AttributeError(name) from exc

    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


__all__ = [
    "LaunchRequest",
    "LaunchResult",
    "MaterializedLaunch",
    "PreparedLaunchSurface",
    "PreparedPolicySurface",
    "PrimarySessionMetadata",
    "ProcessOutcome",
    "ResolvedLaunchPolicy",
    "ResolvedPolicies",
    "ResolvedSkills",
    "RuntimeBindings",
    "SessionIntent",
    "SessionMode",
    "SurfacePolicyInput",
    "bind_launch_context",
    "build_primary_prompt",
    "compile_prepared_policy_surface",
    "launch_primary",
    "load_agent_profile_with_fallback",
    "materialize_harness",
    "normalize_system_prompt_passthrough_args",
    "prepare_launch_surface",
    "resolve_harness",
    "resolve_launch_policy",
    "run_harness_process",
]
