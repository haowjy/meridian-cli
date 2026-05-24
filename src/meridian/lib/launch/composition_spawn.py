"""Spawn launch composition: prepare once, bind per spawn_id/env pass."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import MarsResultCache
from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.context import (
    LaunchContext,
    PreparedLaunchSurface,
    RuntimeBindings,
    bind_launch_context,
    compile_prepared_policy_surface,
    prepare_launch_surface,
)
from meridian.lib.launch.context import _normalize_explicit_work_id as normalize_explicit_work_id
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest


def compose_spawn_launch_surface(
    *,
    request: SpawnRequest,
    runtime: LaunchRuntime,
    harness_registry: HarnessRegistry,
    dry_run: bool,
    runtime_work_id: str | None = None,
    cache: MarsResultCache | None = None,
) -> PreparedLaunchSurface:
    """Policy + Mars + surface. No bind."""

    resolved_config_root = Path(runtime.resolved_config_root).expanduser().resolve()
    resolved_requested_task_cwd = (
        Path(runtime.resolved_requested_task_cwd).expanduser().resolve()
        if runtime.resolved_requested_task_cwd is not None
        else Path(runtime.resolved_control_root).expanduser().resolve()
    )
    project_paths = ProjectConfigPaths(
        project_root=resolved_config_root,
        execution_cwd=resolved_requested_task_cwd,
    )
    catalog = CatalogSession(project_paths.project_root, cache=cache)
    explicit_work_id = normalize_explicit_work_id(
        request_work_id_hint=request.work_id_hint,
        runtime_work_id=runtime_work_id,
    )
    prepared_policy = compile_prepared_policy_surface(
        request=request,
        runtime=runtime,
        project_root=project_paths.project_root,
        harness_registry=harness_registry,
        catalog=catalog,
        explicit_work_id=explicit_work_id,
        dry_run=dry_run,
    )
    return prepare_launch_surface(
        request=request,
        runtime=runtime,
        prepared_policy=prepared_policy,
    )


def bind_spawn_launch_context(
    *,
    prepared: PreparedLaunchSurface,
    bindings: RuntimeBindings,
    runtime: LaunchRuntime,
    harness_registry: HarnessRegistry,
    plan_overrides: Mapping[str, str] | None = None,
) -> LaunchContext:
    """Thin wrapper over bind_launch_context."""

    resolved_config_root = Path(runtime.resolved_config_root).expanduser().resolve()
    merged_bindings = RuntimeBindings(
        spawn_id=bindings.spawn_id,
        report_output_path=bindings.report_output_path,
        runtime_work_id=bindings.runtime_work_id,
        chat_id=bindings.chat_id,
        forked_harness_session_id=bindings.forked_harness_session_id,
        continue_fork_override=bindings.continue_fork_override,
        plan_overrides=dict(plan_overrides or bindings.plan_overrides),
        dry_run=bindings.dry_run,
    )
    return bind_launch_context(
        prepared=prepared,
        bindings=merged_bindings,
        runtime=runtime,
        project_root=resolved_config_root,
        harness_registry=harness_registry,
    )


__all__ = [
    "bind_spawn_launch_context",
    "compose_spawn_launch_surface",
]
