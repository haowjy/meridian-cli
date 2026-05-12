"""Primary-launch input builders."""

from pathlib import Path

from meridian.lib.catalog.model_policy import pattern_fallback_harness
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.config.settings import MeridianConfig, load_config
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
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

    Resolution order when harness is not explicit:
    1. Infer harness from model via pattern matching (no I/O).
    2. Fall back to False — Claude is the default primary harness and does
       not require an initial prompt.
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

    # Harness not set — try to infer from model via pattern matching.
    # Covers raw model IDs (e.g. "gpt-5.4" → codex) and aliases that match
    # a pattern directly (e.g. "codex" matches "codex*").
    model = (request.model or "").strip()
    if model:
        try:
            inferred_harness_id = pattern_fallback_harness(model)
            harness = get_default_harness_registry().get_subprocess_harness(inferred_harness_id)
            return harness.capabilities.requires_initial_prompt
        except (ValueError, KeyError):
            pass

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
    elif _requires_primary_synthetic_prompt(request):
        base_prompt = build_primary_prompt(request)
    else:
        base_prompt = ""

    return SpawnRequest(
        prompt=base_prompt,
        prompt_is_composed=False,
        model=(request.model or "").strip() or None,
        harness=(request.harness or "").strip() or None,
        agent=(request.agent or "").strip() or None,
        extra_args=request.passthrough_args,
        supplemental_prompt_documents=request.supplemental_prompt_documents,
        execution_policy=request.execution_policy,
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
        report_output_path=_DRY_RUN_REPORT_PATH,
        runtime_root=runtime_root.as_posix(),
        project_paths_project_root=resolved_root.as_posix(),
        project_paths_execution_cwd=resolved_cwd.as_posix(),
    )


__all__ = [
    "build_primary_launch_runtime",
    "build_primary_spawn_request",
]
