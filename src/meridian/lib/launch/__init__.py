"""Public launch API."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.lib.launch.launch_types import summarize_composition_warnings
from meridian.lib.state.paths import resolve_project_paths, resolve_work_scratch_dir

if TYPE_CHECKING:
    from meridian.lib.harness.registry import HarnessRegistry
    from meridian.lib.launch.command import (
        normalize_system_prompt_passthrough_args,
    )
    from meridian.lib.launch.context import (
        PreparedLaunchSurface,
        RuntimeBindings,
        bind_launch_context,
        build_launch_context,
        prepare_launch_surface,
    )
    from meridian.lib.launch.materialize import MaterializedLaunch, materialize_harness
    from meridian.lib.launch.policies import ResolvedPolicies, resolve_policies
    from meridian.lib.launch.process import ProcessOutcome, run_harness_process
    from meridian.lib.launch.resolve import (
        ResolvedSkills,
        load_agent_profile_with_fallback,
        resolve_harness,
        resolve_skills_from_profile,
    )
    from meridian.lib.launch.types import (
        LaunchRequest,
        LaunchResult,
        PrimarySessionMetadata,
        SessionIntent,
        SessionMode,
        build_primary_prompt,
    )


def _resolve_work_id_for_launch(project_root: Path, request: LaunchRequest) -> str | None:
    """Resolve work item before entering the launch layer (policy, not mechanism)."""

    from meridian.lib.ops.work_attachment import ensure_explicit_work_item

    explicit_work_id = (request.work_id or "").strip() or None
    if explicit_work_id is not None:
        project_local_root = resolve_project_paths(project_root).root_dir
        return ensure_explicit_work_item(project_local_root, explicit_work_id)
    return None


def launch_primary(
    *,
    project_root: Path,
    request: LaunchRequest,
    harness_registry: HarnessRegistry,
) -> LaunchResult:
    """Launch the primary agent process and wait for exit."""

    from meridian.lib.catalog.catalog_session import CatalogSession
    from meridian.lib.core.context import resolve_runtime_context

    from .context import RuntimeBindings, bind_launch_context, prepare_launch_surface
    from .plan import build_primary_launch_runtime, build_primary_spawn_request
    from .process import run_harness_process
    from .types import LaunchResult

    runtime = build_primary_launch_runtime(project_root=project_root, execution_cwd=project_root)
    if request.include_bootstrap_documents:
        from meridian.lib.catalog.bootstrap import BootstrapRegistry

        resolved_project_root = Path(runtime.project_paths_project_root)
        request = request.model_copy(
            update={
                "supplemental_prompt_documents": (
                    *request.supplemental_prompt_documents,
                    *BootstrapRegistry(resolved_project_root / ".mars").load_all(),
                )
            }
        )
    resolved_work_id = None
    if not request.dry_run:
        resolved_work_id = _resolve_work_id_for_launch(project_root, request)

    resolved_project_root = Path(runtime.project_paths_project_root).expanduser().resolve()
    runtime_root = Path(runtime.runtime_root).expanduser().resolve()
    active_work_dir = (
        resolve_work_scratch_dir(
            resolve_project_paths(resolved_project_root).root_dir,
            resolved_work_id,
        )
        if resolved_work_id is not None
        else resolve_runtime_context(
            project_root=resolved_project_root,
            runtime_root=runtime_root,
        ).work_dir
    )
    prepared = prepare_launch_surface(
        request=build_primary_spawn_request(request=request),
        runtime=runtime,
        project_root=resolved_project_root,
        harness_registry=harness_registry,
        catalog=CatalogSession(resolved_project_root),
        active_work_dir=active_work_dir,
        dry_run=request.dry_run,
    )
    preview_context = bind_launch_context(
        prepared=prepared,
        bindings=RuntimeBindings(
            spawn_id="dry-run-primary",
            report_output_path=Path(runtime.report_output_path or "report.md"),
            runtime_work_id=resolved_work_id,
            dry_run=True,
        ),
        runtime=runtime,
        project_root=resolved_project_root,
        harness_registry=harness_registry,
    )
    warning = summarize_composition_warnings(preview_context.warnings)

    if request.dry_run:
        return LaunchResult(
            command=preview_context.argv,
            exit_code=0,
            continue_ref=None,
            warning=warning,
        )

    outcome = run_harness_process(preview_context, harness_registry, prepared=prepared)
    continue_ref = outcome.resolved_harness_session_id.strip() or None

    return LaunchResult(
        command=outcome.command,
        exit_code=outcome.exit_code,
        continue_ref=continue_ref,
        warning=warning,
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
        "ResolvedPolicies": (".policies", "ResolvedPolicies"),
        "ResolvedSkills": (".resolve", "ResolvedSkills"),
        "RuntimeBindings": (".context", "RuntimeBindings"),
        "SessionIntent": (".types", "SessionIntent"),
        "SessionMode": (".types", "SessionMode"),
        "bind_launch_context": (".context", "bind_launch_context"),
        "build_launch_context": (".context", "build_launch_context"),
        "build_primary_prompt": (".types", "build_primary_prompt"),
        "load_agent_profile_with_fallback": (".resolve", "load_agent_profile_with_fallback"),
        "normalize_system_prompt_passthrough_args": (
            ".command",
            "normalize_system_prompt_passthrough_args",
        ),
        "prepare_launch_surface": (".context", "prepare_launch_surface"),
        "materialize_harness": (".materialize", "materialize_harness"),
        "resolve_harness": (".resolve", "resolve_harness"),
        "resolve_policies": (".policies", "resolve_policies"),
        "resolve_skills_from_profile": (".resolve", "resolve_skills_from_profile"),
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
    "PrimarySessionMetadata",
    "ProcessOutcome",
    "ResolvedPolicies",
    "ResolvedSkills",
    "RuntimeBindings",
    "SessionIntent",
    "SessionMode",
    "bind_launch_context",
    "build_launch_context",
    "build_primary_prompt",
    "launch_primary",
    "load_agent_profile_with_fallback",
    "materialize_harness",
    "normalize_system_prompt_passthrough_args",
    "prepare_launch_surface",
    "resolve_harness",
    "resolve_policies",
    "resolve_skills_from_profile",
    "run_harness_process",
]
