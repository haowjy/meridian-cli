"""Primary-launch input builders."""

from pathlib import Path

from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.config.settings import MeridianConfig, load_config
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.ops.runtime import OperationRuntime
from meridian.lib.state.paths import resolve_project_runtime_root_for_write

from .request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from .types import LaunchRequest, SessionMode, build_primary_prompt

_DRY_RUN_REPORT_PATH = "<spawn-report-path>"


def _requires_primary_synthetic_prompt(request: LaunchRequest) -> bool:
    """Return whether the resolved harness requires a synthetic first prompt.

    If harness is not explicit, fall back to False — Claude is the default
    primary harness and does not require an initial prompt.
    """

    explicit_harness = (request.harness or "").strip()
    if explicit_harness:
        try:
            harness_id = HarnessId(explicit_harness)
        except ValueError:
            # Harness validation happens downstream; be conservative here.
            return False
        harness = get_default_harness_registry().get_subprocess_harness(harness_id)
        return harness.capabilities.requires_initial_prompt

    # Cannot determine harness. Claude is the default primary harness and
    # does not need an initial prompt.
    return False


def _normalize_primary_session(request: LaunchRequest) -> SessionRequest:
    return request.session.model_copy(
        update={
            "requested_harness_session_id": (
                (request.session.requested_harness_session_id or "").strip() or None
            ),
            "continue_harness": (request.session.continue_harness or "").strip() or None,
            "continue_chat_id": (request.session.continue_chat_id or "").strip() or None,
            "continue_fork": (
                request.session.continue_fork or request.session_mode == SessionMode.FORK
            ),
            "primary_session_mode": request.session_mode.value,
        }
    )


def build_primary_spawn_request(
    *,
    request: LaunchRequest,
    prompt: str | None = None,
) -> SpawnRequest:
    """Translate primary launch inputs into the factory request shape."""

    normalized_session = _normalize_primary_session(request)
    if prompt is not None:
        base_prompt = prompt
        primary_prompt_is_synthetic = False
    elif request.prompt is not None:
        base_prompt = request.prompt
        primary_prompt_is_synthetic = False
    elif request.context_from or request.reference_files:
        base_prompt = ""
        primary_prompt_is_synthetic = False
    elif _requires_primary_synthetic_prompt(request):
        base_prompt = build_primary_prompt(request)
        primary_prompt_is_synthetic = True
    else:
        base_prompt = ""
        primary_prompt_is_synthetic = False

    return SpawnRequest(
        prompt=base_prompt,
        prompt_is_composed=False,
        primary_prompt_is_synthetic=primary_prompt_is_synthetic,
        model=(request.model or "").strip() or None,
        harness=(request.harness or "").strip() or None,
        agent=request.agent,
        agent_opt_out=request.agent_opt_out,
        skills=request.skills,
        extra_args=request.passthrough_args,
        supplemental_prompt_documents=request.supplemental_prompt_documents,
        context_from=request.context_from,
        reference_files=request.reference_files,
        execution_policy=request.execution_policy,
        session=normalized_session,
        goal=request.goal,
        work_id_hint=(request.work_id or "").strip() or None,
    )


def build_primary_launch_runtime(
    *,
    project_root: Path,
    execution_cwd: Path | None = None,
    config: MeridianConfig | None = None,
) -> LaunchRuntime:
    """Build primary-launch runtime inputs for the shared launch factory."""

    resolved_root = resolve_project_root_resolution(project_root).project_root
    resolved_cwd = (execution_cwd or project_root).resolve()
    resolved_config = config if config is not None else load_config(resolved_root)
    runtime_root = resolve_project_runtime_root_for_write(resolved_root)

    return LaunchRuntime(
        argv_intent=LaunchArgvIntent.REQUIRED,
        composition_surface=LaunchCompositionSurface.PRIMARY,
        config_snapshot=resolved_config.model_dump(mode="json", exclude_none=True),
        runtime_override_snapshot=RuntimeOverrides.from_env().model_dump(
            mode="json",
            exclude_none=True,
        ),
        report_artifact_path=_DRY_RUN_REPORT_PATH,
        runtime_root=runtime_root.as_posix(),
        config_root=resolved_root.as_posix(),
        control_root=resolved_root.as_posix(),
        requested_task_cwd=resolved_cwd.as_posix(),
        # Legacy aliases for older consumers.
        project_paths_project_root=resolved_root.as_posix(),
        project_paths_execution_cwd=resolved_cwd.as_posix(),
    )


def build_spawn_mars_runtime(
    *,
    runtime: OperationRuntime,
    runtime_root: Path,
    control_root: Path,
    execution_cwd: str,
    argv_intent: LaunchArgvIntent,
    report_artifact_path: str | None = None,
    debug: bool = False,
) -> LaunchRuntime:
    """Launch runtime for spawn paths that need Mars harness_model routing."""

    control_root_str = control_root.as_posix()
    return LaunchRuntime(
        argv_intent=argv_intent,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        config_snapshot=runtime.config.model_dump(mode="json", exclude_none=True),
        debug=debug,
        runtime_override_snapshot=RuntimeOverrides.from_env().model_dump(
            mode="json",
            exclude_none=True,
        ),
        report_artifact_path=report_artifact_path,
        runtime_root=runtime_root.as_posix(),
        config_root=control_root_str,
        control_root=control_root_str,
        requested_task_cwd=execution_cwd,
        # Legacy aliases.
        project_paths_project_root=control_root_str,
        project_paths_execution_cwd=execution_cwd,
    )


__all__ = [
    "build_primary_launch_runtime",
    "build_primary_spawn_request",
    "build_spawn_mars_runtime",
]
