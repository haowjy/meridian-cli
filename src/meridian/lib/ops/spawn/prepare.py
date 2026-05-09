"""Spawn create-input validation and payload preparation helpers."""

import json
from difflib import get_close_matches
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.config.settings import load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.diagnostics import capture_library_diagnostics
from meridian.lib.harness.registry import get_default_harness_registry
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
    resolve_runtime_authority_for_read,
)
from .models import SpawnCreateInput

logger = structlog.get_logger(__name__)
_DRY_RUN_REPORT_PATH = "<spawn-report-path>"
# Backward-compatible monkeypatch target for tests/consumers that asserted the
# old preflight hook. Validation no longer calls this; definitive resolution
# happens in build_launch_context().
resolve_model: object | None = None


def _read_local_merged_models(project_root: Path | None) -> dict[str, object]:
    """Read local mars merged model data without invoking mars."""
    if project_root is None:
        return {}

    merged_path = project_root / ".mars" / "models-merged.json"
    if not merged_path.is_file():
        return {}

    try:
        raw = json.loads(merged_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return cast("dict[str, object]", raw)


def _model_validation_context(
    requested_model: str,
    *,
    project_root: Path | None,
) -> str:
    """Build advisory model context from local merged mars data only."""
    merged = _read_local_merged_models(project_root)
    if not merged:
        return ""

    aliases: list[str] = []
    candidates: list[str] = []
    for alias_name, alias_data in merged.items():
        if not alias_name.strip():
            continue
        candidates.append(alias_name.strip())
        if not isinstance(alias_data, dict):
            continue
        typed_alias_data = cast("dict[str, object]", alias_data)
        model_id = typed_alias_data.get("model")
        if isinstance(model_id, str) and model_id.strip():
            normalized_model_id = model_id.strip()
            aliases.append(f"{alias_name.strip()} -> {normalized_model_id}")
            candidates.append(normalized_model_id)
    if not aliases:
        return ""

    context_lines: list[str] = []
    context_lines.append(f"Available aliases: {', '.join(sorted(aliases))}")

    suggestion: str | None = None
    close = get_close_matches(requested_model, candidates, n=1, cutoff=0.5)
    if close:
        suggestion = close[0]
    if suggestion:
        context_lines.append(f"Did you mean: {suggestion}?")
    return "\n".join(context_lines)


def _validate_requested_model(
    requested_model: str,
    *,
    project_root: str | None,
    explicit_harness: str | None = None,
) -> str | None:
    """Advisory model validation without mars subprocesses.

    Definitive model validation happens later in build_launch_context().
    This preflight check only uses local .mars/models-merged.json data to
    provide useful early feedback when available.
    """
    normalized = requested_model.strip()
    if not normalized:
        return None

    if explicit_harness:
        # Harness is explicitly specified; allow raw provider/model IDs that
        # may not match local alias data.
        return None

    explicit_root = Path(project_root).expanduser().resolve() if project_root else None
    if explicit_root is None:
        return None

    merged = _read_local_merged_models(explicit_root)
    if not merged:
        return None

    if normalized in merged:
        return None

    for alias_data in merged.values():
        if isinstance(alias_data, dict):
            typed_alias_data = cast("dict[str, object]", alias_data)
            model_id = typed_alias_data.get("model")
            if isinstance(model_id, str) and model_id.strip() == normalized:
                return None

    validation_context = _model_validation_context(normalized, project_root=explicit_root)
    if validation_context:
        return f"Model '{normalized}' not found in local configuration.\n{validation_context}"

    return None


def validate_create_input(payload: SpawnCreateInput) -> tuple[SpawnCreateInput, str | None]:
    """Validate spawn input without leaking library warnings to stderr."""

    with capture_library_diagnostics():
        if not payload.prompt.strip() and not payload.files:
            raise ValueError("prompt required: use --prompt/-p or attach at least one --file/-f.")

        model_warning = _validate_requested_model(
            payload.model,
            project_root=payload.project_root,
            explicit_harness=payload.harness,
        )
        return payload, model_warning


def build_create_payload(
    payload: SpawnCreateInput,
    *,
    runtime: OperationRuntime | None = None,
    preflight_warning: str | None = None,
    ctx: RuntimeContext | None = None,
) -> SpawnRequest:
    """Build a spawn request without leaking library warnings to stderr."""

    with capture_library_diagnostics():
        _ = ctx
        if runtime is not None:
            if runtime.authority.runtime_root is None:
                raise ValueError("Operation runtime is missing runtime authority root.")
            project_root = runtime.project_root
            execution_cwd = runtime.authority.execution_cwd
            runtime_root = runtime.authority.runtime_root
            config = runtime.config
            harness_registry = runtime.harness_registry
        elif payload.dry_run:
            authority = resolve_runtime_authority_for_read(payload.project_root)
            project_root = authority.project_root
            config = load_config(project_root, authority=authority)
            execution_cwd = authority.execution_cwd
            runtime_root = authority.runtime_root or authority.project_state_dir
            harness_registry = get_default_harness_registry()
        else:
            runtime_bundle = build_runtime(payload.project_root)
            if runtime_bundle.authority.runtime_root is None:
                raise ValueError("Built operation runtime is missing runtime authority root.")
            project_root = runtime_bundle.project_root
            execution_cwd = runtime_bundle.authority.execution_cwd
            runtime_root = runtime_bundle.authority.runtime_root
            config = runtime_bundle.config
            harness_registry = runtime_bundle.harness_registry

        validated_paths = validate_reference_paths(
            payload.files,
            base_dir=project_root,
        )
        parsed_template_vars = parse_template_assignments(payload.template_vars)
        timeout_secs = minutes_to_seconds(payload.timeout)
        kill_grace_secs = minutes_to_seconds(config.kill_grace_minutes) or 0.0

        raw_request = SpawnRequest(
            prompt=payload.prompt,
            prompt_is_composed=False,
            model=payload.model or None,
            harness=payload.harness,
            agent=payload.agent,
            skills=payload.skills,
            extra_args=payload.passthrough_args,
            execution_policy=ResolvedExecutionPolicy(
                sandbox=payload.sandbox,
                approval=payload.approval,
                autocompact=payload.autocompact,
                autocompact_pct=payload.autocompact_pct,
                effort=payload.effort,
            ),
            retry=RetryPolicy(
                max_attempts=max(1, config.max_retries + 1),
                backoff_secs=config.retry_backoff_seconds,
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
            goal=payload.goal,
            work_id_hint=payload.work.strip() or None,
            warning=preflight_warning,
        )

        preview_context = build_launch_context(
            spawn_id="dry-run",
            request=raw_request,
            runtime=LaunchRuntime(
                argv_intent=LaunchArgvIntent.REQUIRED,
                composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
                config_snapshot=config.model_dump(mode="json", exclude_none=True),
                runtime_override_snapshot=RuntimeOverrides.from_env().model_dump(
                    mode="json",
                    exclude_none=True,
                ),
                report_output_path=_DRY_RUN_REPORT_PATH,
                runtime_root=runtime_root.as_posix(),
                project_paths_project_root=project_root.as_posix(),
                project_paths_execution_cwd=execution_cwd.as_posix(),
            ),
            harness_registry=harness_registry,
            dry_run=True,
        )
        return preview_context.resolved_request.model_copy(
            update={"cli_command": preview_context.argv}
        )


__all__ = ["build_create_payload", "validate_create_input"]
