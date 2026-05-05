"""Spawn create-input validation and payload preparation helpers."""

from difflib import get_close_matches
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.catalog.model_aliases import run_mars_models_list_all
from meridian.lib.catalog.models import load_merged_aliases, resolve_model
from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.config.settings import MeridianConfig, load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.diagnostics import capture_library_diagnostics
from meridian.lib.harness.registry import HarnessRegistry, get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.reference import parse_template_assignments, validate_reference_paths
from meridian.lib.launch.request import (
    ExecutionBudget,
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    RetryPolicy,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.utils.time import minutes_to_seconds

from ..runtime import (
    OperationRuntime,
    build_runtime,
    resolve_runtime_root,
    resolve_runtime_root_for_read,
)
from .models import SpawnCreateInput

logger = structlog.get_logger(__name__)
_MODEL_CONTEXT_LIMIT = 12
_DRY_RUN_REPORT_PATH = "<spawn-report-path>"


class _CreateRuntimeView(BaseModel):
    model_config = ConfigDict(frozen=True)

    """Subset of runtime dependencies needed for payload composition."""

    project_root: Path
    config: MeridianConfig
    harness_registry: HarnessRegistry


def _model_validation_context(
    requested_model: str,
    *,
    project_root: Path | None,
) -> str:
    aliases = load_merged_aliases(project_root=project_root)
    mars_models = run_mars_models_list_all(project_root=project_root) or []
    mars_model_ids = sorted({
        model_id
        for model in mars_models
        if isinstance((model_id := model.get("id")), str) and model_id.strip()
    })
    if not aliases and not mars_model_ids:
        return ""

    available_aliases = ", ".join(
        f"{entry.alias} -> {entry.model_id} [{entry.harness}]" for entry in aliases
    )

    if len(mars_model_ids) > _MODEL_CONTEXT_LIMIT:
        preview = ", ".join(mars_model_ids[:_MODEL_CONTEXT_LIMIT])
        remaining = len(mars_model_ids) - _MODEL_CONTEXT_LIMIT
        available_models = f"{preview}, ... (+{remaining} more)"
    else:
        available_models = ", ".join(mars_model_ids)

    candidates: list[str] = mars_model_ids.copy()
    for entry in aliases:
        candidates.append(entry.alias)
        candidates.append(str(entry.model_id))

    suggestion: str | None = None
    close = get_close_matches(requested_model, candidates, n=1, cutoff=0.5)
    if close:
        suggestion = close[0]
    else:
        for candidate in candidates:
            lowered_candidate = candidate.lower()
            lowered_requested = requested_model.lower()
            if lowered_candidate.startswith(lowered_requested) or lowered_requested.startswith(
                lowered_candidate
            ):
                suggestion = candidate
                break

    context_lines: list[str] = []
    if available_aliases:
        context_lines.append(f"Available aliases: {available_aliases}")
    if available_models:
        context_lines.append(f"Models: {available_models}")
    if suggestion is not None:
        context_lines.append(f"Did you mean: {suggestion}?")
    return "\n".join(context_lines)


def _validate_requested_model(
    requested_model: str,
    *,
    project_root: str | None,
    explicit_harness: str | None = None,
) -> str | None:
    normalized = requested_model.strip()
    if not normalized:
        return None

    explicit_root = Path(project_root).expanduser().resolve() if project_root else None
    try:
        # Validation-only path: launch policy resolution remains the source of
        # truth for canonical model identity/provenance. A future cleanup can
        # collapse this preflight check into the resolve-once policy pipeline.
        resolve_model(normalized, project_root=explicit_root)
    except ValueError:
        if explicit_harness:
            # Harness is explicitly specified; allow raw provider/model IDs
            # that may not match any catalog pattern.
            return None
        validation_context = _model_validation_context(normalized, project_root=explicit_root)
        message = (
            f"Unknown model '{normalized}'. Run `meridian mars models list` "
            "to inspect supported models."
        )
        if validation_context:
            message = f"{message}\n{validation_context}"
        raise ValueError(message) from None

    return None


def _validate_create_input_impl(payload: SpawnCreateInput) -> tuple[SpawnCreateInput, str | None]:
    if not payload.prompt.strip() and not payload.files:
        raise ValueError("prompt required: use --prompt/-p or attach at least one --file/-f.")

    model_warning = _validate_requested_model(
        payload.model,
        project_root=payload.project_root,
        explicit_harness=payload.harness,
    )
    return payload, model_warning


def validate_create_input(payload: SpawnCreateInput) -> tuple[SpawnCreateInput, str | None]:
    """Validate spawn input without leaking library warnings to stderr."""

    with capture_library_diagnostics():
        return _validate_create_input_impl(payload)


def _build_create_payload_impl(
    payload: SpawnCreateInput,
    *,
    runtime: OperationRuntime | None = None,
    preflight_warning: str | None = None,
    ctx: RuntimeContext | None = None,
) -> SpawnRequest:
    _ = ctx
    runtime_view: _CreateRuntimeView
    if runtime is not None:
        runtime_view = _CreateRuntimeView(
            project_root=runtime.project_root,
            config=runtime.config,
            harness_registry=runtime.harness_registry,
        )
        runtime_root = resolve_runtime_root(runtime_view.project_root)
    elif payload.dry_run:
        explicit_project_root = (
            Path(payload.project_root).expanduser().resolve() if payload.project_root else None
        )
        project_root = resolve_project_root(explicit_project_root)
        config = load_config(project_root)
        runtime_view = _CreateRuntimeView(
            project_root=project_root,
            config=config,
            harness_registry=get_default_harness_registry(),
        )
        runtime_root = resolve_runtime_root_for_read(runtime_view.project_root)
    else:
        runtime_bundle = build_runtime(payload.project_root)
        runtime_view = _CreateRuntimeView(
            project_root=runtime_bundle.project_root,
            config=runtime_bundle.config,
            harness_registry=runtime_bundle.harness_registry,
        )
        runtime_root = resolve_runtime_root(runtime_view.project_root)
    validated_paths = validate_reference_paths(
        payload.files,
        base_dir=runtime_view.project_root,
    )
    parsed_template_vars = parse_template_assignments(payload.template_vars)
    timeout_secs = minutes_to_seconds(payload.timeout)
    kill_grace_secs = minutes_to_seconds(runtime_view.config.kill_grace_minutes) or 0.0

    raw_request = SpawnRequest(
        prompt=payload.prompt,
        prompt_is_composed=False,
        model=payload.model or None,
        harness=payload.harness,
        agent=payload.agent,
        skills=payload.skills,
        extra_args=payload.passthrough_args,
        sandbox=payload.sandbox,
        approval=payload.approval,
        autocompact=payload.autocompact,
        effort=payload.effort,
        retry=RetryPolicy(
            max_attempts=max(1, runtime_view.config.max_retries + 1),
            backoff_secs=runtime_view.config.retry_backoff_seconds,
        ),
        budget=ExecutionBudget(
            timeout_secs=int(timeout_secs) if timeout_secs is not None else None,
            kill_grace_secs=int(kill_grace_secs),
        ),
        session=SessionRequest(
            continue_chat_id=payload.session.continue_chat_id,
            requested_harness_session_id=(
                (payload.session.requested_harness_session_id or "").strip() or None
            ),
            continue_fork=payload.session.continue_fork,
            source_execution_cwd=payload.session.source_execution_cwd,
            source_claude_config_dir=payload.session.source_claude_config_dir,
            forked_from_chat_id=payload.session.forked_from_chat_id,
            continue_harness=payload.session.continue_harness,
            continue_source_tracked=payload.session.continue_source_tracked,
            continue_source_ref=payload.session.continue_source_ref,
        ),
        context_from=payload.context_from,
        reference_files=tuple(str(p) for p in validated_paths),
        template_vars=parsed_template_vars,
        work_id_hint=payload.work.strip() or None,
        warning=preflight_warning,
    )

    preview_context = build_launch_context(
        spawn_id="dry-run",
        request=raw_request,
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            config_snapshot=runtime_view.config.model_dump(mode="json", exclude_none=True),
            report_output_path=_DRY_RUN_REPORT_PATH,
            runtime_root=runtime_root.as_posix(),
            project_paths_project_root=runtime_view.project_root.as_posix(),
            project_paths_execution_cwd=runtime_view.project_root.as_posix(),
        ),
        harness_registry=runtime_view.harness_registry,
        dry_run=True,
    )
    return preview_context.resolved_request.model_copy(
        update={"cli_command": preview_context.argv}
    )


def build_create_payload(
    payload: SpawnCreateInput,
    *,
    runtime: OperationRuntime | None = None,
    preflight_warning: str | None = None,
    ctx: RuntimeContext | None = None,
) -> SpawnRequest:
    """Build a spawn request without leaking library warnings to stderr."""

    with capture_library_diagnostics():
        return _build_create_payload_impl(
            payload,
            runtime=runtime,
            preflight_warning=preflight_warning,
            ctx=ctx,
        )


__all__ = ["build_create_payload", "validate_create_input"]
