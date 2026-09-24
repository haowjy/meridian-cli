"""Public launch API."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from meridian.lib.launch.launch_types import summarize_composition_warnings
from meridian.lib.launch.resolution import resolve_launch_inputs
from meridian.lib.state.paths import resolve_project_paths, resolve_work_scratch_dir_for_project

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
        build_launch_context,
        compile_prepared_policy_surface,
        prepare_launch_surface,
    )
    from meridian.lib.launch.materialize import MaterializedLaunch, materialize_harness
    from meridian.lib.launch.policies import (
        ResolvedLaunchPolicy,
        ResolvedPolicies,
        SurfacePolicyInput,
        resolve_launch_policy,
        resolve_policies,
    )
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
    from meridian.lib.ops.reference import UntrackedSourceUse


def _explicit_work_id_for_launch(request: LaunchRequest) -> str | None:
    return (request.work_id or "").strip() or None


def _resolve_work_id_for_launch(
    project_root: Path,
    request: LaunchRequest,
    *,
    context_work_id: str | None,
) -> str | None:
    """Resolve work item before entering the launch layer (policy, not mechanism)."""

    from meridian.lib.ops.work_attachment import materialize_launch_work_id

    project_local_root = resolve_project_paths(project_root).root_dir
    return materialize_launch_work_id(
        project_local_root,
        explicit_work_id=_explicit_work_id_for_launch(request),
        context_work_id=context_work_id,
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
) -> LaunchResult:
    """Launch the primary agent process and wait for exit."""

    from meridian.lib.catalog.catalog_session import CatalogSession
    from meridian.lib.config.project_root import resolve_project_root_resolution
    from meridian.lib.core.context import resolve_runtime_context
    from meridian.lib.ops.runtime import resolve_runtime_root_for_read

    from .context import (
        RuntimeBindings,
        _bind_launch_context_impl,
        compile_prepared_policy_surface,
        prepare_launch_surface,
    )
    from .plan import build_primary_launch_runtime, build_primary_spawn_request
    from .process import run_harness_process
    from .types import LaunchResult

    resolved_project_root = resolve_project_root_resolution(project_root).project_root
    from meridian.lib.launch.source_selection import (
        PrimarySourceSelection,
        reconcile_primary_source_selection,
        session_operation_facts,
        validate_primary_source_use,
    )

    original_session = request.session
    operation = _primary_source_operation(request)
    source_runtime_root = (
        resolve_runtime_root_for_read(resolved_project_root)
        or resolve_project_paths(resolved_project_root).root_dir
    )
    operation_facts = _primary_source_operation_facts(request)
    source_selection = PrimarySourceSelection(
        source_ref=original_session.continue_source_ref,
        native_id=original_session.requested_harness_session_id,
        operation=operation,
        harness=request.harness,
        runtime_root=source_runtime_root,
        other_harnesses=(original_session.continue_harness,),
        tracked_claim=(
            original_session.continue_source_tracked
            or original_session.recorded_native_source is not None
        ),
        operation_facts=operation_facts,
    )
    reconcile_primary_source_selection(source_selection)
    authorization_operation: Literal["resume", "fork"] = (
        "fork" if operation == "fork" else "resume"
    )
    authorized_untracked_source = validate_primary_source_use(
        runtime_root=source_runtime_root,
        source_ref=original_session.continue_source_ref,
        native_selector=original_session.requested_harness_session_id,
        tracked_claim=(
            original_session.continue_source_tracked
            or original_session.recorded_native_source is not None
        ),
        recorded_source=original_session.recorded_native_source,
        harness=request.harness,
        operation=authorization_operation,
        extra_args=request.passthrough_args,
    )
    reconcile_primary_source_selection(
        source_selection, authorized_source=authorized_untracked_source
    )
    # Keep the source-dependent part of the primary CLI adapter here: this is
    # the first safe point for native session discovery and replay/model reads.
    if request.session.continue_source_ref is not None:
        request = _resolve_primary_source_request(
            request=request,
            project_root=resolved_project_root,
            harness_registry=harness_registry,
            authorized_source=authorized_untracked_source,
        )
    explicit_work_id = _explicit_work_id_for_launch(request)
    runtime_root_for_context = resolve_runtime_root_for_read(resolved_project_root)
    runtime_context = resolve_runtime_context(
        project_root=resolved_project_root,
        runtime_root=runtime_root_for_context,
    )
    inherit_ambient_work = request.session_mode.value != "resume"
    inherited_task_dir = (
        None if request.session_mode.value == "resume" else runtime_context.inherited_task_dir
    )
    ambient_work_id = (
        None
        if explicit_work_id is not None or not inherit_ambient_work
        else runtime_context.work_id
    )
    project_state_dir = resolve_project_paths(resolved_project_root).root_dir
    launch_resolution = resolve_launch_inputs(
        authority_root=resolved_project_root,
        project_state_dir=project_state_dir,
        context_from=request.context_from,
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
    )
    if request.include_bootstrap_documents:
        from meridian.lib.catalog.bootstrap import BootstrapRegistry

        request = request.model_copy(
            update={
                "supplemental_prompt_documents": (
                    *request.supplemental_prompt_documents,
                    *BootstrapRegistry(resolved_project_root / ".mars").load_all(),
                )
            }
        )
    resolved_work_id = (
        preview_work_id
        if request.dry_run
        else _resolve_work_id_for_launch(
            resolved_project_root,
            request,
            context_work_id=launch_resolution.context_work_id,
        )
    )

    runtime = runtime.model_copy(update=launch_resolution.runtime_updates)
    effective_work_id = resolved_work_id or launch_resolution.effective_work_id
    active_work_dir = (
        resolve_work_scratch_dir_for_project(
            resolved_project_root,
            effective_work_id,
        )
        if effective_work_id is not None
        else (runtime_context.work_dir if inherit_ambient_work else None)
    )
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
        active_work_dir=active_work_dir,
        dry_run=request.dry_run,
    )
    if prepared_policy.resolved_policy.adapter.id.value == "pi":
        from meridian.lib.harness.pi_native_source import reject_pi_native_source_options

        # Check caller/replay-originated raw syntax once routing has identified Pi,
        # before seed_session and system-argument normalization can consume it.
        reject_pi_native_source_options(request.passthrough_args)
    prepared = prepare_launch_surface(
        request=spawn_request,
        runtime=runtime,
        prepared_policy=prepared_policy,
    )
    final_session = prepared.request.session
    reconcile_primary_source_selection(
        PrimarySourceSelection(
            source_ref=original_session.continue_source_ref,
            native_id=original_session.requested_harness_session_id,
            operation=source_selection.operation,
            harness=request.harness,
            runtime_root=source_runtime_root,
            seed_id=prepared.seed_harness_session_id,
            other_harnesses=(
                original_session.continue_harness,
                prepared.request.harness,
                prepared.harness.id.value,
            ),
        ),
        authorized_source=authorized_untracked_source,
        resolved_id=final_session.requested_harness_session_id,
        resolved_id_supplied=original_session.continue_source_ref is not None,
        resolved_harness=prepared.request.session.continue_harness,
        resolved_tracked=(
            final_session.continue_source_tracked
            or final_session.recorded_native_source is not None
        ),
        resolved_source_ref=final_session.continue_source_ref,
        resolved_source_ref_supplied=original_session.continue_source_ref is not None,
        resolved_operation_facts=session_operation_facts(final_session),
    )
    preview_context = _bind_launch_context_impl(
        prepared=prepared,
        bindings=RuntimeBindings(
            spawn_id="dry-run-primary",
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
            env=preview_context.binding.environment.final_env,
        )
        if launch_plan is not None:
            launch_plan = launch_plan.model_copy(update={"requested_model": request.model})
        return LaunchResult(
            launch_plan=launch_plan,
            command=() if launch_plan is not None else preview_context.binding.argv,
            exit_code=0,
            continue_ref=None,
            continue_chat_id=None,
            warning=warning,
            primary_source_warning=request.primary_source_warning,
            primary_source_chat_id=request.primary_source_chat_id,
            terminal_surface_mode=(
                preview_context.resolved_request.terminal_surface_mode.value
                if preview_context.resolved_request.terminal_surface_mode is not None
                else None
            ),
        )

    outcome = run_harness_process(preview_context, harness_registry, prepared=prepared)
    continue_ref = outcome.resolved_harness_session_id.strip() or None

    return LaunchResult(
        command=outcome.command,
        exit_code=outcome.exit_code,
        continue_ref=continue_ref,
        continue_chat_id=outcome.chat_id,
        primary_spawn_id=outcome.primary_spawn_id,
        warning=warning,
        primary_source_warning=request.primary_source_warning,
        primary_source_chat_id=request.primary_source_chat_id,
    )


def _primary_source_operation(request: LaunchRequest) -> Literal["fresh", "resume", "fork"]:
    """Choose an operation only after the shared source comparator checks facts."""
    primary_mode = (request.session.primary_session_mode or "").strip().lower() or None
    if primary_mode not in (None, "resume", "fork"):
        raise ValueError(f"Unsupported primary session mode: {primary_mode!r}")
    if primary_mode is not None:
        return primary_mode
    if request.session.continue_fork:
        return "fork"
    return request.session_mode.value


def _primary_source_operation_facts(request: LaunchRequest) -> tuple[str, ...]:
    """Return each non-default operation assertion without collapsing them."""
    facts: list[str] = []
    primary_mode = (request.session.primary_session_mode or "").strip().lower()
    if primary_mode:
        facts.append(primary_mode)
    if request.session.continue_fork:
        facts.append("fork")
    if request.session_mode.value != "fresh" or "session_mode" in request.model_fields_set:
        facts.append(request.session_mode.value)
    return tuple(facts)



def _resolve_primary_source_request(
    *,
    request: LaunchRequest,
    project_root: Path,
    harness_registry: HarnessRegistry,
    authorized_source: UntrackedSourceUse | None = None,
) -> LaunchRequest:
    """Resolve native source details only after launch_primary's authority gate."""
    from meridian.lib.launch.continue_replay import (
        build_continue_replay_contract,
        continue_replay_source_from_reference,
    )
    from meridian.lib.launch.source_selection import session_operation_facts
    from meridian.lib.ops.reference import (
        missing_fork_session_error_with_discovery,
        resolve_session_reference,
    )
    from meridian.lib.state.paths import resolve_project_runtime_root

    _ = harness_registry
    source_ref = request.session.continue_source_ref
    assert source_ref is not None
    operation = _primary_source_operation(request)
    if operation == "fresh":
        raise ValueError("Primary source selection conflict (fresh operation has a source).")
    resolved = resolve_session_reference(
        project_root,
        source_ref,
        harness_hint=request.harness if operation == "resume" else None,
    )
    # The legacy resolver may discover native state, but a contradictory result
    # must stop here, before replay reads native model history or writes an
    # observation. The strict negative lookup above remains the sole authority
    # lookup for this owner call.
    from meridian.lib.launch.source_selection import (
        PrimarySourceSelection,
        reconcile_primary_source_selection,
    )

    runtime_root = (
        authorized_source.lookup_scope
        if authorized_source is not None
        else resolve_project_runtime_root(project_root)
    )
    resolved_snapshot = getattr(resolved, "source_launch_policy_snapshot", None)
    checked_native_id = reconcile_primary_source_selection(
        PrimarySourceSelection(
            source_ref=request.session.continue_source_ref,
            native_id=request.session.requested_harness_session_id,
            operation=operation,
            harness=request.harness,
            runtime_root=runtime_root,
            other_harnesses=(request.session.continue_harness,),
            operation_facts=_primary_source_operation_facts(request),
            tracked_claim=(
                request.session.continue_source_tracked
                or request.session.recorded_native_source is not None
            ),
        ),
        authorized_source=authorized_source,
        resolved_id=resolved.authoritative_harness_session_id,
        resolved_id_supplied=True,
        resolved_harness=resolved.harness,
        resolved_snapshot_harness=(
            resolved_snapshot.harness
            if resolved_snapshot is not None
            else None
        ),
        resolved_tracked=resolved.tracked and authorized_source is not None,
    )
    if resolved.missing_harness_session_id:
        raise ValueError(
            missing_fork_session_error_with_discovery(
                source_ref=source_ref,
                project_root=project_root,
                source_harness=resolved.harness,
                source_chat_id=resolved.source_chat_id,
            )
        )
    session = request.session
    if operation == "resume":
        contract = build_continue_replay_contract(
            source=continue_replay_source_from_reference(
                source_ref=source_ref,
                resolved_reference=resolved,
                harness_session_id=checked_native_id,
            ),
            explicit_harness=request.harness,
            requested_agent=request.agent,
            agent_opt_out=request.agent_opt_out,
            requested_model_override=(request.model or "").strip() or None,
            runtime_root=runtime_root,
        )
        reconcile_primary_source_selection(
            PrimarySourceSelection(
                source_ref=request.session.continue_source_ref,
                native_id=request.session.requested_harness_session_id,
                operation=operation,
                harness=request.harness,
                runtime_root=runtime_root,
                other_harnesses=(
                    request.session.continue_harness,
                    contract.session.continue_harness,
                    contract.harness,
                    (
                        contract.launch_policy_snapshot.harness
                        if contract.launch_policy_snapshot is not None
                        else None
                    ),
                ),
                operation_facts=_primary_source_operation_facts(request),
                tracked_claim=(
                    request.session.continue_source_tracked
                    or request.session.recorded_native_source is not None
                ),
            ),
            authorized_source=authorized_source,
            resolved_id=contract.session.requested_harness_session_id,
            resolved_id_supplied=True,
            resolved_harness=contract.session.continue_harness,
            resolved_source_ref=contract.session.continue_source_ref,
            resolved_source_ref_supplied=True,
            resolved_tracked=(
                contract.session.continue_source_tracked
                or contract.session.recorded_native_source is not None
            ),
            resolved_operation_facts=session_operation_facts(contract.session),
        )
        task_dir = contract.task_dir
        source_warning = resolved.warning
        if task_dir is not None and not Path(task_dir).is_dir():
            task_dir = project_root.as_posix()
            warning = (
                "Continued session's task_dir is unavailable or not a directory: "
                f"{contract.task_dir}; falling back to the normal launch directory."
            )
            source_warning = f"{source_warning}; {warning}" if source_warning else warning
        return request.model_copy(
            update={
                "model": contract.model,
                "harness": contract.harness,
                "agent": contract.agent,
                "agent_opt_out": contract.agent_opt_out,
                "skills": contract.skills,
                "task_dir": task_dir,
                "work_id": request.work_id or contract.work_id,
                "passthrough_args": contract.passthrough_args,
                "launch_policy_snapshot": contract.launch_policy_snapshot,
                "primary_source_warning": source_warning,
                "primary_source_chat_id": resolved.source_chat_id,
                "session": contract.session,
            }
        )

    source_harness = (
        resolved.harness.strip() if resolved.harness and resolved.harness.strip() else None
    )
    explicit_harness = (request.harness or "").strip() or None
    if explicit_harness and source_harness and explicit_harness != source_harness:
        raise ValueError(
            "Cannot fork across harnesses: "
            f"source is '{source_harness}', target is '{explicit_harness}'."
        )
    harness = explicit_harness or source_harness
    if harness is None:
        missing_ref = resolved.authoritative_harness_session_id or source_ref
        raise ValueError(
            f"Session '{missing_ref}' not recognized by any harness. "
            "Use --harness to specify which harness owns this session."
        )
    return request.model_copy(
        update={
            "model": request.model or resolved.source_model,
            "agent": request.agent
            if request.primary_explicit_agent or request.agent_opt_out
            else (request.agent or resolved.source_agent),
            "work_id": request.work_id or resolved.source_work_id,
            "harness": harness,
            "primary_source_warning": resolved.warning,
            "primary_source_chat_id": resolved.source_chat_id,
            "session": session.model_copy(
                update={
                    "requested_harness_session_id": checked_native_id,
                    "continue_harness": harness,
                    "continue_fork": True,
                    "forked_from_chat_id": resolved.source_chat_id,
                    "forked_from_history_id": resolved.source_history_id,
                    "source_control_root": resolved.source_control_root,
                    "source_execution_cwd": resolved.source_execution_cwd,
                    "source_claude_config_dir": resolved.source_claude_config_dir,
                    "source_pi_session_dir": resolved.source_pi_session_dir,
                    "continue_source_tracked": resolved.tracked,
                    "continue_source_ref": source_ref,
                }
            ),
        }
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
        "build_launch_context": (".context", "build_launch_context"),
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
    "build_launch_context",
    "build_primary_prompt",
    "compile_prepared_policy_surface",
    "launch_primary",
    "load_agent_profile_with_fallback",
    "materialize_harness",
    "normalize_system_prompt_passthrough_args",
    "prepare_launch_surface",
    "resolve_harness",
    "resolve_launch_policy",
    "resolve_policies",
    "resolve_skills_from_profile",
    "run_harness_process",
]
