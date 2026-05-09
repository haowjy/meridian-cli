"""Primary-launch input builders."""
from pathlib import Path

from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.config.settings import MeridianConfig, load_config
from meridian.lib.core.overrides import RuntimeOverrides
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


def _normalize_primary_session(request: LaunchRequest) -> SessionRequest:
    return request.session.model_copy(
        update={
            "requested_harness_session_id": (
                (request.session.requested_harness_session_id or "").strip() or None
            ),
            "continue_harness": (request.session.continue_harness or "").strip() or None,
            "continue_chat_id": (request.session.continue_chat_id or "").strip() or None,
            "continue_fork": (
                request.session.continue_fork
                or request.session_mode == SessionMode.FORK
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
    base_prompt = prompt if prompt is not None else build_primary_prompt(request)

    return SpawnRequest(
        prompt=base_prompt,
        prompt_is_composed=False,
        model=(request.model or "").strip() or None,
        harness=(request.harness or "").strip() or None,
        agent=(request.agent or "").strip() or None,
        extra_args=request.passthrough_args,
        supplemental_prompt_documents=request.supplemental_prompt_documents,
        sandbox=request.sandbox,
        approval=request.approval,
        autocompact=request.autocompact,
        autocompact_pct=request.autocompact_pct,
        effort=request.effort,
        session=normalized_session,
        work_id_hint=(request.work_id or "").strip() or None,
    )


def build_primary_launch_runtime(
    *,
    project_root: Path,
    execution_cwd: Path | None = None,
    config: MeridianConfig | None = None,
) -> LaunchRuntime:
    """Build primary-launch runtime inputs for the shared launch factory."""

    resolved_root = resolve_project_root(project_root)
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
        report_output_path=_DRY_RUN_REPORT_PATH,
        runtime_root=runtime_root.as_posix(),
        project_paths_project_root=resolved_root.as_posix(),
        project_paths_execution_cwd=resolved_cwd.as_posix(),
    )


__all__ = [
    "build_primary_launch_runtime",
    "build_primary_spawn_request",
]
