"""Shared launch-context assembly used by subprocess and streaming runners."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry, MarsResultCache
from meridian.lib.config.context_config import (
    ArbitraryContextConfig,
    ContextConfig,
    ContextSourceType,
)
from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.config.settings import MeridianConfig, load_config
from meridian.lib.config.workspace import get_projectable_roots
from meridian.lib.context.resolver import resolve_context_paths
from meridian.lib.core.child_env import validate_child_env_keys
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.diagnostics import capture_library_diagnostics
from meridian.lib.harness.adapter import SpawnParams, SubprocessHarness
from meridian.lib.launch.launch_types import (
    CompositionWarning,
    PermissionResolver,
    PreflightResult,
    PreparedLaunchContent,
    PreparedLaunchRuntimeSeeds,
    PreparedPromptPayload,
    ResolvedLaunchBinding,
    ResolvedLaunchEnvironment,
    ResolvedLaunchSpec,
    summarize_composition_warnings,
)
from meridian.lib.launch.workspace_projection import (
    OPENCODE_CONFIG_CONTENT_ENV,
    project_workspace_roots,
)
from meridian.lib.safety.permissions import PermissionConfig
from meridian.lib.state import work_store
from meridian.lib.state.paths import (
    load_context_config,
    resolve_project_paths,
    resolve_spawn_log_dir,
    resolve_work_scratch_dir_for_project,
)
from meridian.lib.state.session_store import get_session_active_work_id
from meridian.lib.telemetry import emit_telemetry
from meridian.lib.tools import ToolsField
from meridian.plugin_api.git import resolve_clone_path

from .command import (
    build_launch_argv,
    normalize_system_prompt_passthrough_args,
    resolve_launch_spec_stage,
)
from .composition import ComposedLaunchContent, ProjectedContent, PromptDocument
from .cwd import resolve_child_execution_cwd
from .env import build_env_plan
from .env import merge_env_overrides as _merge_env_overrides
from .permissions import (
    CLAUDE_NATIVE_DELEGATION_TOOLS,
    resolve_nested_claude_permission_request,
    resolve_permission_pipeline,
)
from .policies import (
    ModelSelectionContext,
    ResolvedLaunchPolicy,
    SurfacePolicyInput,
    resolve_launch_policy,
)
from .prompt import (
    build_goal_instruction,
    build_launch_context_documents,
    build_report_instruction,
    build_work_goal_instruction,
    compose_skill_injections,
    compose_skill_prompt_documents,
    sanitize_prior_output,
    strip_stale_report_paths,
)
from .reference import (
    ReferenceItem,
    load_reference_items,
    resolve_template_variables,
    substitute_template_variables,
)
from .request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    RequestPromptPayload,
    SpawnRequest,
)
from .resolve import (
    format_missing_skills_warning,
    resolve_profile_path,
    resolve_skill_paths,
)
from .workspace import resolve_workspace_snapshot_for_launch

if TYPE_CHECKING:
    from meridian.lib.harness.registry import HarnessRegistry


@dataclass(frozen=True)
class ChildEnvContext:
    """Sole producer for child `MERIDIAN_*` environment overrides."""

    parent_spawn_id: str | None
    project_root: Path
    runtime_root: Path
    parent_chat_id: str | None
    parent_depth: int
    work_id: str | None = None
    work_dir: Path | None = None
    context_dirs: tuple[tuple[str, Path], ...] = ()

    @classmethod
    def from_environment(
        cls,
        *,
        project_paths: ProjectConfigPaths,
        runtime_root: Path,
        explicit_work_id: str | None = None,
    ) -> ChildEnvContext:
        resolved_project_root = project_paths.project_root.resolve()
        resolved_runtime_root = runtime_root.resolve()
        normalized_explicit_work_id = (explicit_work_id or "").strip() or None
        context_config = load_context_config(resolved_project_root) or ContextConfig()
        parent_ctx = ResolvedContext.from_environment(
            explicit_work_id=normalized_explicit_work_id,
            explicit_project_root=resolved_project_root,
            explicit_runtime_root=resolved_runtime_root,
            context_config=context_config,
        )
        parent_spawn_id = str(parent_ctx.spawn_id) if parent_ctx.spawn_id else None
        parent_chat_id = parent_ctx.chat_id.strip() or None
        parent_depth = parent_ctx.depth

        work_id = (
            normalized_explicit_work_id
            or parent_ctx.work_id
            or os.getenv("MERIDIAN_ACTIVE_WORK_ID", "").strip()
            or None
        )
        if work_id is None and parent_chat_id:
            # Keep launch semantics: runtime_root decides active work lookup.
            try:
                work_id = get_session_active_work_id(resolved_runtime_root, parent_chat_id)
            except Exception:
                work_id = None

        repo_paths = resolve_project_paths(resolved_project_root)
        work_dir = (
            parent_ctx.work_dir
            if (
                work_id is not None
                and parent_ctx.work_dir is not None
                and parent_ctx.work_id == work_id
            )
            else resolve_work_scratch_dir_for_project(
                resolved_project_root,
                work_id,
                project_paths=repo_paths,
            )
            if work_id is not None
            else None
        )

        resolved_context_paths = resolve_context_paths(
            resolved_project_root,
            context_config,
        )
        context_dirs = (
            ("work", resolved_context_paths.work_root),
            ("work_archive", resolved_context_paths.work_archive),
            ("kb", resolved_context_paths.kb_root),
            *tuple(
                sorted((name, path) for name, (path, _) in resolved_context_paths.extra.items())
            ),
        )

        return cls(
            # Keep MERIDIAN_PROJECT_DIR anchored to the project/config root so
            # nested meridian commands resolve repo-local profiles, skills,
            # and config from the same place as the parent launch.
            parent_spawn_id=parent_spawn_id,
            project_root=resolved_project_root,
            runtime_root=resolved_runtime_root,
            parent_chat_id=parent_chat_id,
            parent_depth=parent_depth,
            work_id=work_id,
            work_dir=work_dir,
            context_dirs=context_dirs,
        )

    def child_context(
        self,
        *,
        child_spawn_id: str | None = None,
        increment_depth: bool = True,
    ) -> dict[str, str]:
        ctx = ResolvedContext(
            spawn_id=SpawnId(self.parent_spawn_id) if self.parent_spawn_id else None,
            depth=self.parent_depth,
            project_root=self.project_root,
            runtime_root=self.runtime_root,
            chat_id=self.parent_chat_id or "",
            work_id=self.work_id,
            work_dir=self.work_dir,
            context_dirs=self.context_dirs,
        )
        overrides = ctx.child_env_overrides(
            increment_depth=increment_depth,
            child_spawn_id=child_spawn_id,
        )
        validate_child_env_keys(overrides)
        return overrides


@dataclass(frozen=True)
class LaunchContext:
    request: SpawnRequest
    runtime: LaunchRuntime
    # Compatibility alias for project/config operations only.
    project_root: Path
    # Legacy alias for requested task cwd (or control root when unset).
    execution_cwd: Path
    control_root: Path
    task_cwd: Path | None
    runtime_root: Path
    work_id: str | None
    binding: ResolvedLaunchBinding
    harness: SubprocessHarness
    resolved_request: SpawnRequest
    projected_content: ProjectedContent | None = None
    seed_harness_session_id: str | None = None
    seed_harness_session_args: tuple[str, ...] = ()
    # I-13: adapter input transformations surface here instead of silently mutating.
    warnings: tuple[CompositionWarning, ...] = ()
    model_selection: ModelSelectionContext | None = None


@dataclass(frozen=True)
class PreparedLaunchSurface:
    """Expensive, spawn-stable launch resolution results.

    This is the public in-memory boundary between launch preparation and
    runtime binding. It deliberately excludes runtime-only values such as
    spawn IDs, report paths, environment, argv, and permission outputs.
    """

    request: SpawnRequest
    harness: SubprocessHarness
    composition_warnings: tuple[CompositionWarning, ...]
    content: PreparedLaunchContent
    runtime_seeds: PreparedLaunchRuntimeSeeds
    profile_tools_for_nested_deny: ToolsField | None
    has_profile_for_nested_deny: bool
    model_selection: ModelSelectionContext | None
    alias_catalog: dict[str, AliasEntry] | None = None
    # Original launch request preserved for LaunchContext.request compatibility.
    # `request` carries the resolved request used by bind.
    launch_request: SpawnRequest | None = None

    @property
    def prompt_payload(self) -> PreparedPromptPayload:
        return self.content.prompt_payload

    @property
    def loaded_references(self) -> tuple[ReferenceItem, ...]:
        return self.content.loaded_references

    @property
    def projected_content(self) -> ProjectedContent | None:
        return self.content.projected_content

    @property
    def agent_inventory_prompt(self) -> str | None:
        return self.content.agent_inventory_prompt

    @property
    def context_prompt(self) -> str | None:
        return self.content.context_prompt

    @property
    def seed_harness_session_id(self) -> str | None:
        return self.runtime_seeds.seed_harness_session_id

    @property
    def seed_session_args(self) -> tuple[str, ...]:
        return self.runtime_seeds.seed_session_args


@dataclass(frozen=True)
class MaterializedLaunchArtifacts:
    """Shared launch materialization below policy resolution."""

    run_params: SpawnParams
    permission_config: PermissionConfig
    perms: PermissionResolver
    spec: ResolvedLaunchSpec


@dataclass(frozen=True)
class RuntimeBindings:
    """Runtime-only values for the bind phase."""

    spawn_id: str
    report_output_path: Path
    runtime_work_id: str | None = None
    chat_id: str | None = None
    forked_harness_session_id: str | None = None
    continue_fork_override: bool | None = None
    plan_overrides: dict[str, str] = field(default_factory=lambda: {})
    dry_run: bool = False


@dataclass(frozen=True)
class PreparedPolicySurface:
    """Shared policy boundary plus stable launch-surface inputs."""

    project_paths: ProjectConfigPaths
    runtime_root: Path
    resolved_policy: ResolvedLaunchPolicy
    active_work_dir: Path | None


@dataclass(frozen=True)
class ResolvedContinuation:
    """Harness continuation/fork state after capability checks."""

    harness_session_id: str | None
    continue_fork: bool
    warning: str | None = None


@dataclass(frozen=True)
class TaskContextInputs:
    """Resolved user-turn context fields for launch content composition."""

    reference_items: tuple[ReferenceItem, ...]
    prior_output: str
    resolved_context_from: tuple[str, ...]


def resolve_task_context_inputs(
    *,
    context_from: tuple[str, ...],
    reference_files: tuple[str, ...],
    project_root: Path,
) -> TaskContextInputs:
    """Resolve ``--from`` refs and ``-f`` files into user-turn context blocks."""

    loaded_references = load_reference_items(
        reference_files,
        base_dir=project_root,
    )
    resolved_context_from = context_from
    prior_output = ""
    if context_from:
        from meridian.lib.ops.spawn.context_ref import (
            render_context_refs,
            resolve_context_ref,
            resolved_context_ref_value,
        )

        resolved_context_refs = tuple(
            resolve_context_ref(project_root, ref) for ref in context_from
        )
        resolved_context_from = tuple(
            resolved_context_ref_value(ref) for ref in resolved_context_refs
        )
        prior_output = render_context_refs(resolved_context_refs)

    return TaskContextInputs(
        reference_items=loaded_references,
        prior_output=sanitize_prior_output(prior_output) if prior_output.strip() else "",
        resolved_context_from=resolved_context_from,
    )


def merge_env_overrides(
    *,
    plan_overrides: Mapping[str, str],
    runtime_overrides: Mapping[str, str],
    preflight_overrides: Mapping[str, str],
) -> dict[str, str]:
    """Merge launch env overrides with `MERIDIAN_*` leak checks."""

    return _merge_env_overrides(
        plan_overrides=plan_overrides,
        runtime_overrides=runtime_overrides,
        preflight_overrides=preflight_overrides,
    )


def _build_composition_warnings(
    *,
    request_warning: str | None,
    policy_warnings: tuple[CompositionWarning, ...] = (),
    route_warning: str | None,
    missing_skills_warning: str | None,
    continuation_warning: str | None,
) -> tuple[CompositionWarning, ...]:
    warnings: list[CompositionWarning] = []
    for code, message in (("request_warning", request_warning),):
        normalized = (message or "").strip()
        if not normalized:
            continue
        warnings.append(CompositionWarning(code=code, message=normalized))
    warnings.extend(policy_warnings)
    for code, message in (
        ("route_warning", route_warning),
        ("missing_skills", missing_skills_warning),
        ("continuation_warning", continuation_warning),
    ):
        normalized = (message or "").strip()
        if not normalized:
            continue
        warnings.append(CompositionWarning(code=code, message=normalized))
    return tuple(warnings)


def prepare_prompt_payload(
    *,
    adhoc_agent_payload: str = "",
    projected_content: ProjectedContent | None = None,
    appended_system_prompt: str | None = None,
    user_turn_content: str | None = None,
) -> PreparedPromptPayload:
    """Normalize managed prompt payload for later launch binding."""

    projected_system_prompt = (
        projected_content.system_prompt.strip() if projected_content is not None else ""
    )
    projected_user_turn = (
        projected_content.user_turn_content.strip() if projected_content is not None else ""
    )
    normalized_system_prompt = (
        projected_system_prompt or (appended_system_prompt or "").strip() or None
    )
    normalized_user_turn = projected_user_turn or (user_turn_content or "").strip() or None
    return PreparedPromptPayload(
        adhoc_agent_payload=adhoc_agent_payload.strip(),
        appended_system_prompt=normalized_system_prompt,
        user_turn_content=normalized_user_turn,
    )


def _request_prompt_payload(prompt_payload: PreparedPromptPayload) -> RequestPromptPayload:
    """Convert internal prompt payload to the persisted request carrier."""

    return RequestPromptPayload(
        adhoc_agent_payload=prompt_payload.adhoc_agent_payload,
        appended_system_prompt=prompt_payload.appended_system_prompt,
        user_turn_content=prompt_payload.user_turn_content,
    )


def materialize_launch_artifacts(
    *,
    harness: SubprocessHarness,
    prompt: str,
    model: str | None,
    effort: str | None,
    skills: tuple[str, ...],
    agent: str | None,
    prompt_payload: PreparedPromptPayload,
    extra_args: tuple[str, ...] = (),
    control_root: str | None = None,
    task_cwd: str | None = None,
    mcp_tools: tuple[str, ...] = (),
    projected_roots: tuple[Path, ...] = (),
    interactive: bool = False,
    continue_harness_session_id: str | None = None,
    continue_fork: bool = False,
    report_output_path: str | None = None,
    context_from_payload: tuple[str, ...] = (),
    reference_items: tuple[ReferenceItem, ...] = (),
    sandbox: str | None = None,
    tools: ToolsField | None = None,
    approval: str | None = None,
    unsafe_no_permissions: bool = False,
) -> MaterializedLaunchArtifacts:
    """Build shared run/spec/permission launch artifacts."""

    task_cwd_instruction = ""
    if task_cwd:
        task_cwd_instruction = (
            "\n\n# Task Working Directory\n"
            f"Your process cwd is NOT your task directory. "
            f"Your task working directory is: {task_cwd}\n"
            f"Use absolute paths or `cd {task_cwd}` before filesystem operations. "
            f"The `MERIDIAN_TASK_CWD` environment variable contains this path.\n"
        )
    effective_appended_system = prompt_payload.appended_system_prompt or ""
    if task_cwd_instruction:
        effective_appended_system = (
            effective_appended_system + task_cwd_instruction
            if effective_appended_system
            else task_cwd_instruction.lstrip()
        )

    run_params = SpawnParams(
        prompt=prompt,
        model=ModelId(model) if model else None,
        effort=effort,
        skills=skills,
        agent=agent,
        adhoc_agent_payload=prompt_payload.adhoc_agent_payload,
        extra_args=extra_args,
        control_root=control_root,
        task_cwd=task_cwd,
        mcp_tools=mcp_tools,
        projected_roots=projected_roots,
        interactive=interactive,
        continue_harness_session_id=continue_harness_session_id,
        continue_fork=continue_fork,
        report_output_path=report_output_path,
        appended_system_prompt=effective_appended_system or None,
        context_from_payload=context_from_payload,
        reference_items=reference_items,
        user_turn_content=prompt_payload.user_turn_content,
    )
    permission_config, perms = resolve_permission_pipeline(
        sandbox=sandbox,
        tools=tools,
        approval=approval or "default",
        unsafe_no_permissions=unsafe_no_permissions,
    )
    spec = resolve_launch_spec_stage(adapter=harness, run_inputs=run_params, perms=perms)
    return MaterializedLaunchArtifacts(
        run_params=run_params,
        permission_config=permission_config,
        perms=perms,
        spec=spec,
    )


def _missing_continue_session_error(source_ref: str | None) -> str:
    normalized_source = (source_ref or "").strip()
    if normalized_source:
        if normalized_source.startswith("p") and normalized_source[1:].isdigit():
            return f"Spawn '{normalized_source}' has no recorded session - cannot continue/fork."
        return (
            f"Session '{normalized_source}' has no recorded harness session - cannot continue/fork."
        )
    return "Source reference has no recorded harness session - cannot continue/fork."


def _collect_git_context_clone_roots(config: ContextConfig | None) -> tuple[Path, ...]:
    """Return configured clone roots for git-backed context entries."""

    if config is None:
        return ()

    roots: list[Path] = []

    def add_root(*, source: ContextSourceType, remote: str | None) -> None:
        if source != ContextSourceType.GIT:
            return
        normalized_remote = (remote or "").strip()
        if not normalized_remote:
            return
        roots.append(resolve_clone_path(normalized_remote))

    add_root(source=config.work.source, remote=config.work.remote)
    add_root(source=config.kb.source, remote=config.kb.remote)

    extras_raw = getattr(config, "__pydantic_extra__", None)
    extras = cast("dict[str, object]", extras_raw) if isinstance(extras_raw, dict) else {}
    for value in extras.values():
        try:
            parsed = (
                value
                if isinstance(value, ArbitraryContextConfig)
                else ArbitraryContextConfig.model_validate(value)
            )
        except ValidationError:
            continue
        add_root(source=parsed.source, remote=parsed.remote)

    return tuple(roots)


def _collect_context_projection_roots(
    project_root: Path, config: ContextConfig | None
) -> tuple[Path, ...]:
    """Return all meridian context paths for workspace projection.

    Includes work_root, work_archive, kb_root, and any extra context dirs.
    These are the paths that meridian exports as MERIDIAN_CONTEXT_*_DIR env vars.
    """
    effective = config or ContextConfig()
    resolved = resolve_context_paths(project_root, effective)
    roots: list[Path] = [
        resolved.work_root,
        resolved.work_archive,
        resolved.kb_root,
    ]
    for path, _source in resolved.extra.values():
        roots.append(path)

    return tuple(roots)


def _dedupe_roots_in_order(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """Deduplicate root paths while preserving the first-seen order."""

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return tuple(deduped)


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_task_cwd_covered_by_projection(
    *,
    task_cwd: Path,
    control_root: Path,
    projected_roots: tuple[Path, ...],
) -> bool:
    if _path_within(task_cwd, control_root):
        return True
    return any(_path_within(task_cwd, root) for root in projected_roots)


def _resolve_harness_id(
    *,
    request: SpawnRequest,
) -> HarnessId:
    explicit_harness = (request.harness or "").strip()
    if explicit_harness:
        try:
            return HarnessId(explicit_harness)
        except ValueError as exc:
            raise ValueError(f"Unknown harness '{explicit_harness}'.") from exc

    raise ValueError("SpawnRequest.harness is required.")


_KNOWN_MODEL_FAMILIES: dict[str, str] = {
    "gpt-5": "gpt-5",
    "gpt-5.3": "gpt-5.3",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.5": "gpt-5.5",
    "gpt-55": "gpt-5.5",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-5.3-mini": "gpt-5.3-mini",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "claude-opus": "claude-opus",
    "claude-sonnet": "claude-sonnet",
    "claude-haiku": "claude-haiku",
    "codex": "codex",
    "codex-mini": "codex-mini",
    "o3": "openai-o",
    "o4-mini": "openai-o",
}


def normalize_usage_model_family(model: str | None) -> str:
    """Map provider model identifiers to a finite usage aggregation vocabulary."""

    normalized = (model or "").strip().lower()
    if not normalized:
        return "other"

    normalized = "-".join(part for part in normalized.replace("_", "-").split("-") if part)
    if not normalized:
        return "other"

    parts = normalized.split("-")
    for end in range(len(parts), 0, -1):
        candidate = "-".join(parts[:end])
        if candidate in _KNOWN_MODEL_FAMILIES:
            return _KNOWN_MODEL_FAMILIES[candidate]

    if "codex" in parts:
        return "codex"
    if parts[0].startswith("o") and any(char.isdigit() for char in parts[0]):
        return "openai-o"
    return "other"


def _resolve_report_output_path(
    *,
    runtime: LaunchRuntime,
    project_paths: ProjectConfigPaths,
    spawn_id: str,
) -> Path:
    report_path_raw = (runtime.report_output_path or "").strip()
    if report_path_raw:
        return Path(report_path_raw).expanduser()
    return resolve_spawn_log_dir(project_paths.project_root, spawn_id) / "report.md"


def _normalize_explicit_work_id(
    *,
    request_work_id_hint: str | None = None,
    runtime_work_id: str | None = None,
) -> str | None:
    return (runtime_work_id or "").strip() or (request_work_id_hint or "").strip() or None


def _resolve_active_work_dir(
    *,
    project_paths: ProjectConfigPaths,
    runtime_root: Path,
    explicit_work_id: str | None,
) -> Path | None:
    context_config = load_context_config(project_paths.project_root) or ContextConfig()
    return ResolvedContext.from_environment(
        explicit_work_id=explicit_work_id,
        explicit_project_root=project_paths.project_root,
        explicit_runtime_root=runtime_root,
        context_config=context_config,
    ).work_dir


def build_child_runtime_env_overrides(
    *,
    project_paths: ProjectConfigPaths,
    runtime_root: Path,
    child_spawn_id: str,
    work_id: str | None = None,
    increment_depth: bool = True,
    additional_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build child runtime env overrides shared by launch/chat callers."""

    runtime_ctx = ChildEnvContext.from_environment(
        project_paths=project_paths,
        runtime_root=runtime_root,
        explicit_work_id=work_id,
    )
    runtime_overrides = runtime_ctx.child_context(
        child_spawn_id=child_spawn_id,
        increment_depth=increment_depth,
    )
    if additional_overrides:
        runtime_overrides.update(additional_overrides)
    return runtime_overrides


def _resolve_session_continuation(
    *,
    request: SpawnRequest,
    harness: SubprocessHarness,
) -> ResolvedContinuation:
    requested_harness_session_id = (
        request.session.requested_harness_session_id or ""
    ).strip() or None
    requested_continue_fork = request.session.continue_fork
    requested_harness = (request.session.continue_harness or "").strip()
    if request.session.continue_source_tracked and requested_harness_session_id is None:
        raise ValueError(_missing_continue_session_error(request.session.continue_source_ref))

    if not requested_harness_session_id:
        return ResolvedContinuation(harness_session_id=None, continue_fork=False)
    if requested_harness and requested_harness != str(harness.id):
        return ResolvedContinuation(
            harness_session_id=None,
            continue_fork=False,
            warning="Continuation session ignored because target harness differs from source run.",
        )
    if not harness.capabilities.supports_session_resume:
        return ResolvedContinuation(
            harness_session_id=None,
            continue_fork=False,
            warning=f"Harness '{harness.id}' does not support session resume; starting fresh.",
        )
    if requested_continue_fork and not harness.capabilities.supports_session_fork:
        return ResolvedContinuation(
            harness_session_id=requested_harness_session_id,
            continue_fork=False,
            warning=f"Harness '{harness.id}' does not support session fork; resuming in-place.",
        )
    return ResolvedContinuation(
        harness_session_id=requested_harness_session_id,
        continue_fork=requested_continue_fork,
    )


def compile_prepared_policy_surface(
    *,
    request: SpawnRequest,
    runtime: LaunchRuntime,
    project_root: Path,
    harness_registry: HarnessRegistry,
    catalog: CatalogSession,
    active_work_dir: Path | None = None,
    explicit_work_id: str | None = None,
    dry_run: bool = False,
) -> PreparedPolicySurface:
    """Compile the shared launch policy boundary before projector work."""

    requested_task_cwd = runtime.resolved_requested_task_cwd or runtime.resolved_control_root
    project_paths = ProjectConfigPaths(
        project_root=project_root.expanduser().resolve(),
        execution_cwd=Path(requested_task_cwd).expanduser().resolve(),
    )
    config = (
        MeridianConfig.model_validate(runtime.config_snapshot)
        if runtime.config_snapshot
        else load_config(project_paths.project_root)
    )
    cli_overrides = RuntimeOverrides(
        model=(request.model or "").strip() or None,
        harness=(request.harness or "").strip() or None,
        agent=(request.agent or "").strip() or None,
        effort=request.execution_policy.effort,
        sandbox=request.execution_policy.sandbox,
        approval=request.execution_policy.approval,
        autocompact=request.execution_policy.autocompact,
        autocompact_pct=request.execution_policy.autocompact_pct,
        timeout=request.execution_policy.timeout,
    )
    env_overrides = (
        runtime.resolved_runtime_overrides
        if runtime.has_runtime_override_snapshot
        else RuntimeOverrides.from_env()
    )
    if runtime.composition_surface == LaunchCompositionSurface.PRIMARY:
        config_overrides = RuntimeOverrides.from_config(config)
    else:
        config_overrides = RuntimeOverrides.from_spawn_config(config)

    resolved_policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=runtime.composition_surface,
            catalog=catalog,
            layers=(cli_overrides, env_overrides),
            config_overrides=config_overrides,
            config=config,
            harness_registry=harness_registry,
            skills_readonly=dry_run,
            requested_skills=request.skills,
        )
    )

    runtime_root = Path(runtime.runtime_root).expanduser().resolve()
    if active_work_dir is None:
        active_work_dir = _resolve_active_work_dir(
            project_paths=project_paths,
            runtime_root=runtime_root,
            explicit_work_id=explicit_work_id,
        )

    return PreparedPolicySurface(
        project_paths=project_paths,
        runtime_root=runtime_root,
        resolved_policy=resolved_policy,
        active_work_dir=active_work_dir,
    )


def _resolve_spawn_prepare_projection(
    *,
    request: SpawnRequest,
    project_paths: ProjectConfigPaths,
    active_work_dir: Path | None,
    policy: ResolvedLaunchPolicy,
) -> PreparedLaunchContent:
    profile = policy.profile
    harness = policy.adapter
    resolved_skills = policy.resolved_skills

    task_ctx = resolve_task_context_inputs(
        context_from=request.context_from,
        reference_files=request.reference_files,
        project_root=project_paths.project_root,
    )

    resolved_template_variables = resolve_template_variables(request.template_vars)
    cleaned_user_prompt = substitute_template_variables(
        strip_stale_report_paths(request.prompt),
        resolved_template_variables,
    )

    if harness.capabilities.supports_native_skills:
        supplemental_documents: tuple[PromptDocument, ...] = ()
    else:
        supplemental_documents = compose_skill_prompt_documents(resolved_skills.loaded_skills)

    agent_profile_body = ""
    if (
        profile is not None
        and profile.body.strip()
        and not harness.capabilities.supports_native_agents
    ):
        rendered_agent_body = substitute_template_variables(
            profile.body.strip(),
            resolved_template_variables,
        )
        agent_profile_body = f"# Agent Profile\n\n{rendered_agent_body}"

    agent_inventory_prompt, context_prompt = build_launch_context_documents(
        project_root=project_paths.project_root,
        alias_catalog=policy.alias_catalog,
        active_work_dir=active_work_dir,
    )

    resolved_work_id = (request.work_id_hint or "").strip() or (
        active_work_dir.name if active_work_dir is not None else ""
    )
    work_goal: str | None = None
    if resolved_work_id:
        project_state_dir = resolve_project_paths(project_paths.project_root).root_dir
        work_item = work_store.get_work_item(project_state_dir, resolved_work_id)
        if work_item is not None:
            work_goal = work_item.goal
    completion_contract = build_goal_instruction(request.goal)
    work_goal_instruction = build_work_goal_instruction(work_goal)
    if completion_contract and work_goal_instruction:
        completion_contract = f"{work_goal_instruction}\n\n{completion_contract}"
    elif work_goal_instruction:
        completion_contract = work_goal_instruction

    projected = harness.project_content(
        ComposedLaunchContent(
            supplemental_documents=supplemental_documents,
            agent_profile_body=agent_profile_body,
            report_instruction=build_report_instruction(),
            inventory_prompt=agent_inventory_prompt or "",
            context_prompt=context_prompt or "",
            completion_contract=completion_contract,
            passthrough_system_fragments=(),
            user_task_prompt=cleaned_user_prompt,
            reference_items=task_ctx.reference_items,
            prior_output=task_ctx.prior_output,
        )
    )

    return PreparedLaunchContent(
        final_prompt=projected.user_turn_content.strip() or cleaned_user_prompt,
        resolved_context_from=task_ctx.resolved_context_from,
        loaded_references=task_ctx.reference_items,
        prompt_payload=prepare_prompt_payload(projected_content=projected),
        projected_content=projected,
        agent_inventory_prompt=agent_inventory_prompt,
        context_prompt=context_prompt,
    )


def _resolve_primary_projection(
    *,
    request: SpawnRequest,
    project_paths: ProjectConfigPaths,
    active_work_dir: Path | None,
    policy: ResolvedLaunchPolicy,
    resolved_continue_harness_session_id: str | None,
) -> tuple[PreparedLaunchContent, tuple[str, ...], str | None, tuple[str, ...]]:
    profile = policy.profile
    harness = policy.adapter
    resolved_skills = policy.resolved_skills
    session_mode = ((request.session.primary_session_mode or "fresh").strip().lower()) or "fresh"

    agent_inventory_prompt: str | None = None
    context_prompt: str | None = None
    if session_mode != "resume":
        agent_inventory_prompt, context_prompt = build_launch_context_documents(
            project_root=project_paths.project_root,
            alias_catalog=policy.alias_catalog,
            active_work_dir=active_work_dir,
        )

    seed = harness.seed_session(
        is_resume=session_mode == "resume",
        harness_session_id=resolved_continue_harness_session_id or "",
        passthrough_args=request.extra_args,
    )
    seeded_passthrough_args = (*request.extra_args, *seed.session_args)
    passthrough_args, passthrough_prompt_fragments = normalize_system_prompt_passthrough_args(
        seeded_passthrough_args
    )
    passthrough_system_fragments = tuple(
        frag.strip() for frag in passthrough_prompt_fragments if frag.strip()
    )

    if harness.capabilities.supports_native_skills:
        supplemental_documents = tuple(request.supplemental_prompt_documents)
    else:
        supplemental_documents = (
            *compose_skill_prompt_documents(resolved_skills.loaded_skills),
            *request.supplemental_prompt_documents,
        )
    agent_profile_body = ""
    if (
        profile is not None
        and profile.body.strip()
        and not harness.capabilities.supports_native_agents
    ):
        agent_profile_body = profile.body.strip()

    task_ctx = resolve_task_context_inputs(
        context_from=request.context_from,
        reference_files=request.reference_files,
        project_root=project_paths.project_root,
    )

    projected = harness.project_content(
        ComposedLaunchContent(
            supplemental_documents=supplemental_documents,
            agent_profile_body=agent_profile_body,
            report_instruction="",
            inventory_prompt=agent_inventory_prompt or "",
            context_prompt=context_prompt or "",
            completion_contract="",
            passthrough_system_fragments=passthrough_system_fragments,
            user_task_prompt=request.prompt,
            reference_items=task_ctx.reference_items,
            prior_output=task_ctx.prior_output,
        )
    )

    return (
        PreparedLaunchContent(
            final_prompt=projected.user_turn_content.strip() or request.prompt,
            resolved_context_from=task_ctx.resolved_context_from,
            loaded_references=task_ctx.reference_items,
            prompt_payload=prepare_prompt_payload(projected_content=projected),
            projected_content=projected,
            agent_inventory_prompt=agent_inventory_prompt,
            context_prompt=context_prompt,
        ),
        passthrough_args,
        seed.session_id or resolved_continue_harness_session_id,
        seed.session_args,
    )


def _prepare_spawn_surface(
    *,
    request: SpawnRequest,
    project_paths: ProjectConfigPaths,
    active_work_dir: Path | None,
    policy: ResolvedLaunchPolicy,
) -> tuple[PreparedLaunchContent, ResolvedContinuation]:
    content = PreparedLaunchContent(
        final_prompt=request.prompt,
        resolved_context_from=request.context_from,
    )

    if not request.prompt_is_composed:
        content = _resolve_spawn_prepare_projection(
            request=request,
            project_paths=project_paths,
            active_work_dir=active_work_dir,
            policy=policy,
        )

    prompt_policy = policy.adapter.run_prompt_policy()
    if prompt_policy.skill_injection_mode == "append-system-prompt":
        existing_appended = content.prompt_payload.appended_system_prompt
        # When skills were suppressed from supplemental_documents (native-skill
        # harness), compose_skill_injections() delivers them via the append
        # channel instead.  When no projection produced system content, this is
        # the sole source of appended content.
        needs_skill_injection = (
            existing_appended is None
            or policy.adapter.capabilities.supports_native_skills
        )
        if needs_skill_injection:
            skill_content = compose_skill_injections(
                policy.resolved_skills.loaded_skills
            )
            if existing_appended is None:
                merged_appended = skill_content or None
            elif skill_content:
                merged_appended = f"{existing_appended}\n\n{skill_content}"
            else:
                merged_appended = existing_appended
            content = PreparedLaunchContent(
                final_prompt=content.final_prompt,
                resolved_context_from=content.resolved_context_from,
                loaded_references=content.loaded_references,
                prompt_payload=PreparedPromptPayload(
                    adhoc_agent_payload=content.prompt_payload.adhoc_agent_payload,
                    appended_system_prompt=merged_appended,
                    user_turn_content=content.prompt_payload.user_turn_content,
                ),
                projected_content=content.projected_content,
                agent_inventory_prompt=content.agent_inventory_prompt,
                context_prompt=content.context_prompt,
            )

    return (
        content,
        _resolve_session_continuation(request=request, harness=policy.adapter),
    )


def _prepare_primary_surface(
    *,
    request: SpawnRequest,
    project_paths: ProjectConfigPaths,
    active_work_dir: Path | None,
    policy: ResolvedLaunchPolicy,
) -> tuple[
    PreparedLaunchContent,
    tuple[str, ...],
    str | None,
    tuple[str, ...],
    ResolvedContinuation,
]:
    # Validate harness supports primary (interactive) launch before any
    # harness-specific side effects (session seeding, etc.).
    if not policy.adapter.capabilities.supports_primary_launch:
        raise ValueError(
            f"Harness '{policy.harness}' does not support primary (interactive) launch."
        )
    continuation = _resolve_session_continuation(request=request, harness=policy.adapter)
    (
        content,
        final_passthrough_args,
        seed_harness_session_id,
        seed_session_args,
    ) = _resolve_primary_projection(
        request=request,
        project_paths=project_paths,
        active_work_dir=active_work_dir,
        policy=policy,
        resolved_continue_harness_session_id=continuation.harness_session_id,
    )
    return (
        content,
        final_passthrough_args,
        seed_harness_session_id,
        seed_session_args,
        continuation,
    )


def prepare_launch_surface(
    *,
    request: SpawnRequest,
    runtime: LaunchRuntime,
    prepared_policy: PreparedPolicySurface,
) -> PreparedLaunchSurface:
    """Resolve the expensive, spawn-stable launch surface."""
    project_paths = prepared_policy.project_paths
    active_work_dir = prepared_policy.active_work_dir
    policies = prepared_policy.resolved_policy
    profile = policies.profile
    has_profile = profile is not None
    execution_policy = policies.execution_policy
    model_selection = policies.model_selection
    harness = policies.adapter
    resolved_skills = policies.resolved_skills

    route_warning = None
    content = PreparedLaunchContent(
        final_prompt=request.prompt,
        resolved_context_from=request.context_from,
    )
    final_passthrough_args = request.extra_args
    continuation = ResolvedContinuation(harness_session_id=None, continue_fork=False)
    seed_harness_session_id: str | None = None
    seed_session_args: tuple[str, ...] = ()
    if runtime.composition_surface == LaunchCompositionSurface.SPAWN_PREPARE:
        content, continuation = _prepare_spawn_surface(
            request=request,
            project_paths=project_paths,
            active_work_dir=active_work_dir,
            policy=policies,
        )
        seed_harness_session_id = continuation.harness_session_id
    elif runtime.composition_surface == LaunchCompositionSurface.PRIMARY:
        (
            content,
            final_passthrough_args,
            seed_harness_session_id,
            seed_session_args,
            continuation,
        ) = _prepare_primary_surface(
            request=request,
            project_paths=project_paths,
            active_work_dir=active_work_dir,
            policy=policies,
        )

    missing_skills_warning = (
        format_missing_skills_warning(resolved_skills.missing_skills)
        if resolved_skills.missing_skills
        else None
    )
    composition_warnings = _build_composition_warnings(
        request_warning=request.warning,
        policy_warnings=policies.warnings,
        route_warning=route_warning,
        missing_skills_warning=missing_skills_warning,
        continuation_warning=continuation.warning,
    )
    warning = summarize_composition_warnings(composition_warnings)

    agent_metadata = dict(request.agent_metadata)
    model_selection_update: dict[str, str | None] = {
        "model_selection_requested_token": None,
        "model_selection_canonical_id": None,
        "model_selection_harness_provenance": None,
    }
    if model_selection is not None:
        model_selection_update = {
            "model_selection_requested_token": model_selection.requested_token,
            "model_selection_canonical_id": model_selection.canonical_model_id,
            "model_selection_harness_provenance": model_selection.harness_provenance,
        }
    resolved_agent_name = profile.name if profile is not None else request.agent
    session_agent_path = resolve_profile_path(profile)
    if resolved_agent_name is not None:
        agent_metadata["session_agent"] = resolved_agent_name
    if session_agent_path:
        agent_metadata["session_agent_path"] = session_agent_path
    adhoc_agent_payload = ""
    if profile is not None and harness.capabilities.supports_native_agents:
        adhoc_agent_payload = harness.build_adhoc_agent_payload(
            name=profile.name,
            description=profile.description,
            prompt=profile.body,
        )
    content = PreparedLaunchContent(
        final_prompt=content.final_prompt,
        resolved_context_from=content.resolved_context_from,
        loaded_references=content.loaded_references,
        prompt_payload=PreparedPromptPayload(
            adhoc_agent_payload=adhoc_agent_payload,
            appended_system_prompt=content.prompt_payload.appended_system_prompt,
            user_turn_content=content.prompt_payload.user_turn_content,
        ),
        projected_content=content.projected_content,
        agent_inventory_prompt=content.agent_inventory_prompt,
        context_prompt=content.context_prompt,
    )
    profile_tools_for_nested_deny = policies.resolved_tools if profile is not None else None

    resolved_request = request.model_copy(
        update={
            "prompt": content.final_prompt,
            "prompt_is_composed": True,
            "model": policies.model,
            "harness": str(policies.harness),
            "agent": resolved_agent_name,
            "skills": resolved_skills.skill_names,
            "extra_args": final_passthrough_args,
            "mcp_tools": (
                policies.resolved_mcp_tools if profile is not None else request.mcp_tools
            ),
            "execution_policy": execution_policy,
            "tools": policies.resolved_tools if profile is not None else request.tools,
            "session": request.session.model_copy(
                update={
                    "requested_harness_session_id": continuation.harness_session_id,
                    "continue_fork": continuation.continue_fork,
                }
            ),
            "context_from": content.resolved_context_from,
            "warning": warning,
            "agent_metadata": agent_metadata,
            "prompt_payload": _request_prompt_payload(content.prompt_payload),
            "skill_paths": resolve_skill_paths(resolved_skills.loaded_skills),
            "fallback_chain": policies.fallback_chain,
            "terminal_surface_mode": policies.terminal_surface_mode,
            "matched_policy_rule": policies.matched_policy_rule,
            **model_selection_update,
        }
    )
    return PreparedLaunchSurface(
        request=resolved_request,
        harness=harness,
        composition_warnings=composition_warnings,
        content=content,
        runtime_seeds=PreparedLaunchRuntimeSeeds(
            seed_harness_session_id=seed_harness_session_id,
            seed_session_args=seed_session_args,
        ),
        profile_tools_for_nested_deny=profile_tools_for_nested_deny,
        has_profile_for_nested_deny=has_profile,
        model_selection=model_selection,
        alias_catalog=policies.alias_catalog,
        launch_request=request,
    )


def _build_direct_surface(
    *,
    request: SpawnRequest,
    project_root: Path,
    harness_registry: HarnessRegistry,
) -> PreparedLaunchSurface:
    """Build the lightweight prepared surface for already-resolved DIRECT launches."""

    resolved_project_root = project_root.expanduser().resolve()
    harness_id = _resolve_harness_id(request=request)
    harness = harness_registry.get_subprocess_harness(harness_id)
    composition_warnings = _build_composition_warnings(
        request_warning=request.warning,
        policy_warnings=(),
        route_warning=None,
        missing_skills_warning=None,
        continuation_warning=None,
    )
    loaded_references = load_reference_items(
        request.reference_files,
        base_dir=resolved_project_root,
    )

    return PreparedLaunchSurface(
        request=request,
        harness=harness,
        composition_warnings=composition_warnings,
        content=PreparedLaunchContent(
            final_prompt=request.prompt,
            resolved_context_from=request.context_from,
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
            ),
            seed_session_args=(),
        ),
        profile_tools_for_nested_deny=None,
        has_profile_for_nested_deny=False,
        model_selection=None,
        alias_catalog=None,
        launch_request=request,
    )


def bind_launch_context(
    *,
    prepared: PreparedLaunchSurface,
    bindings: RuntimeBindings,
    runtime: LaunchRuntime,
    project_root: Path,
    harness_registry: HarnessRegistry,
) -> LaunchContext:
    """Cheap materialization: env, cwd, spec, argv, permissions."""

    _ = (harness_registry, bindings.chat_id, bindings.dry_run)

    resolved_config_root = Path(runtime.resolved_config_root).expanduser().resolve()
    resolved_control_root = Path(runtime.resolved_control_root).expanduser().resolve()
    resolved_requested_task_cwd = (
        Path(runtime.resolved_requested_task_cwd).expanduser().resolve()
        if runtime.resolved_requested_task_cwd is not None
        else resolved_control_root
    )
    project_paths = ProjectConfigPaths(
        project_root=resolved_config_root,
        execution_cwd=resolved_requested_task_cwd,
    )
    workspace_snapshot = resolve_workspace_snapshot_for_launch(project_paths.project_root)
    workspace_roots = get_projectable_roots(workspace_snapshot)
    context_config = load_context_config(project_paths.project_root)
    git_context_roots = _collect_git_context_clone_roots(context_config)
    context_projection_roots = _collect_context_projection_roots(
        project_paths.project_root, context_config
    )
    runtime_root = Path(runtime.runtime_root).expanduser().resolve()
    system_temp_root = Path(tempfile.gettempdir()).resolve()
    resolved_request = prepared.request
    harness = prepared.harness
    effective_session_id = bindings.forked_harness_session_id or prepared.seed_harness_session_id
    composition_warnings = prepared.composition_warnings
    prompt_payload = prepared.prompt_payload
    loaded_references = prepared.loaded_references
    profile_tools_for_nested_deny = prepared.profile_tools_for_nested_deny
    has_profile_for_nested_deny = prepared.has_profile_for_nested_deny
    projected_content = prepared.projected_content
    seed_harness_session_args = prepared.seed_session_args
    model_selection = prepared.model_selection
    report_output_path = bindings.report_output_path
    execution_cwd = project_paths.execution_cwd
    _bind_work_id = _normalize_explicit_work_id(
        request_work_id_hint=resolved_request.work_id_hint,
        runtime_work_id=bindings.runtime_work_id,
    )
    child_cwd = resolve_child_execution_cwd(
        project_root=execution_cwd,
        project_state_dir=resolve_project_paths(project_paths.project_root).root_dir,
        work_id=_bind_work_id,
    )
    try:
        preflight = harness.preflight(
            execution_cwd=resolved_control_root,
            child_cwd=resolved_control_root,
            passthrough_args=tuple(resolved_request.extra_args),
        )
    except AttributeError:
        preflight = PreflightResult.build(
            expanded_passthrough_args=tuple(resolved_request.extra_args)
        )

    task_cwd = child_cwd if child_cwd.resolve() != resolved_control_root.resolve() else None
    workspace_authority_roots = _dedupe_roots_in_order(
        (
            *workspace_roots,
            *git_context_roots,
            *context_projection_roots,
        )
    )
    projected_roots = _dedupe_roots_in_order(
        (
            *workspace_authority_roots,
            runtime_root,
            system_temp_root,
        )
    )
    workspace_projection = project_workspace_roots(
        harness_id=harness.id,
        roots=projected_roots,
        parent_opencode_config_content=os.getenv(OPENCODE_CONFIG_CONTENT_ENV),
    )
    projected_extra_args = preflight.expanded_passthrough_args
    if workspace_projection.diagnostics:
        composition_warnings = (
            *composition_warnings,
            *(
                CompositionWarning(code=diag.code, message=diag.message)
                for diag in workspace_projection.diagnostics
            ),
        )
        resolved_request = resolved_request.model_copy(
            update={"warning": summarize_composition_warnings(composition_warnings)}
        )

    if task_cwd is not None and not _is_task_cwd_covered_by_projection(
        task_cwd=task_cwd,
        control_root=resolved_control_root,
        projected_roots=workspace_authority_roots,
    ):
        composition_warnings = (
            *composition_warnings,
            CompositionWarning(
                code="task_cwd_not_projected",
                message=(
                    "Task cwd is outside control root and projected workspace roots; "
                    "launch continues, but access is not granted automatically."
                ),
                detail={
                    "task_cwd": task_cwd.as_posix(),
                    "control_root": resolved_control_root.as_posix(),
                },
            ),
        )
        resolved_request = resolved_request.model_copy(
            update={"warning": summarize_composition_warnings(composition_warnings)}
        )

    model = (resolved_request.model or "").strip()
    model_family = normalize_usage_model_family(model)
    emit_telemetry(
        "usage",
        "usage.model.selected",
        scope="core.launch",
        ids={"spawn_id": bindings.spawn_id},
        data={"model_family": model_family, "harness": harness.id.value},
    )
    effective_model = model
    if model_selection is not None and model_selection.harness_model_id is not None:
        effective_model = model_selection.harness_model_id

    if (
        runtime.composition_surface == LaunchCompositionSurface.SPAWN_PREPARE
        and harness.id == HarnessId.CLAUDE
        and CLAUDE_NATIVE_DELEGATION_TOOLS
    ):
        tools = resolve_nested_claude_permission_request(
            tools=resolved_request.tools,
            profile_tools=profile_tools_for_nested_deny,
            has_profile=has_profile_for_nested_deny,
        )
        if tools != resolved_request.tools:
            resolved_request = resolved_request.model_copy(
                update={
                    "tools": tools,
                }
            )

    is_primary_launch = runtime.composition_surface == LaunchCompositionSurface.PRIMARY
    materialized = materialize_launch_artifacts(
        harness=harness,
        prompt=resolved_request.prompt,
        model=effective_model,
        effort=resolved_request.execution_policy.effort,
        skills=resolved_request.skills,
        agent=resolved_request.agent,
        prompt_payload=prompt_payload,
        extra_args=projected_extra_args,
        control_root=resolved_control_root.as_posix(),
        task_cwd=task_cwd.as_posix() if task_cwd is not None else None,
        mcp_tools=resolved_request.mcp_tools,
        projected_roots=projected_roots,
        interactive=is_primary_launch,
        continue_harness_session_id=effective_session_id,
        continue_fork=(
            bindings.continue_fork_override
            if bindings.continue_fork_override is not None
            else resolved_request.session.continue_fork
        ),
        report_output_path=report_output_path.as_posix(),
        context_from_payload=resolved_request.context_from,
        reference_items=loaded_references,
        sandbox=resolved_request.execution_policy.sandbox,
        tools=resolved_request.tools,
        approval=resolved_request.execution_policy.approval,
        unsafe_no_permissions=runtime.unsafe_no_permissions,
    )
    run_params = materialized.run_params

    permission_config = materialized.permission_config
    perms = materialized.perms
    spec = materialized.spec
    argv: tuple[str, ...] = ()
    if runtime.argv_intent != LaunchArgvIntent.SPEC_ONLY:
        argv = build_launch_argv(
            adapter=harness,
            run_inputs=run_params,
            perms=perms,
            projected_spec=spec,
        )

    is_primary_launch = runtime.composition_surface == LaunchCompositionSurface.PRIMARY
    requested_work_id = _normalize_explicit_work_id(
        request_work_id_hint=resolved_request.work_id_hint,
        runtime_work_id=bindings.runtime_work_id,
    )
    child_context_env = build_child_runtime_env_overrides(
        project_paths=project_paths,
        runtime_root=runtime_root,
        child_spawn_id=bindings.spawn_id,
        work_id=requested_work_id,
        increment_depth=not is_primary_launch,
    )
    effective_work_id = (
        child_context_env.get("MERIDIAN_ACTIVE_WORK_ID", "").strip() or requested_work_id
    )
    # Informational: tells the child its own harness for yield timing.
    # Not a policy override — from_env() does not read it back.
    child_context_env["MERIDIAN_HARNESS"] = harness.id.value
    if task_cwd is not None:
        child_context_env["MERIDIAN_TASK_CWD"] = task_cwd.as_posix()
    runtime_override_env = (
        runtime.resolved_runtime_overrides.to_env()
        if runtime.has_runtime_override_snapshot
        else RuntimeOverrides.from_env().to_env()
    )
    bind_env_overrides = merge_env_overrides(
        plan_overrides=bindings.plan_overrides,
        runtime_overrides={**runtime_override_env, **child_context_env},
        preflight_overrides=preflight.extra_env,
    )
    bind_env_overrides.update(workspace_projection.env_overrides)
    env = build_env_plan(
        base_env=os.environ,
        adapter=harness,
        run_inputs=run_params,
        permission_config=permission_config,
        runtime_env_overrides=bind_env_overrides,
    )
    environment = ResolvedLaunchEnvironment.build(
        child_context_env=child_context_env,
        plan_env=dict(bindings.plan_overrides),
        preflight_env=dict(preflight.extra_env),
        workspace_env=dict(workspace_projection.env_overrides),
        runtime_override_env=runtime_override_env,
        bind_env_overrides=bind_env_overrides,
        runner_overlay_env={},
        final_env=env,
    )
    binding = ResolvedLaunchBinding(
        work_id=effective_work_id,
        child_cwd=child_cwd,
        report_output_path=report_output_path,
        run_params=run_params,
        permission_config=permission_config,
        perms=perms,
        spec=spec,
        argv=argv,
        environment=environment,
        effective_harness_session_id=effective_session_id,
        seed_harness_session_args=seed_harness_session_args,
    )

    return LaunchContext(
        request=prepared.launch_request or prepared.request,
        runtime=runtime,
        project_root=project_paths.project_root,
        execution_cwd=execution_cwd,
        control_root=resolved_control_root,
        task_cwd=task_cwd,
        runtime_root=runtime_root,
        work_id=effective_work_id,
        binding=binding,
        harness=harness,
        resolved_request=resolved_request,
        projected_content=projected_content,
        seed_harness_session_id=effective_session_id,
        seed_harness_session_args=seed_harness_session_args,
        warnings=composition_warnings,
        model_selection=model_selection,
    )


def _build_launch_context_impl(
    *,
    spawn_id: str,
    request: SpawnRequest,
    runtime: LaunchRuntime,
    harness_registry: HarnessRegistry,
    dry_run: bool = False,
    plan_overrides: Mapping[str, str] | None = None,
    runtime_work_id: str | None = None,
    cache: MarsResultCache | None = None,
) -> LaunchContext:
    """Build deterministic launch context from raw request/runtime inputs."""

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
    if runtime.composition_surface != LaunchCompositionSurface.DIRECT:
        catalog = CatalogSession(project_paths.project_root, cache=cache)
        explicit_work_id = _normalize_explicit_work_id(
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
        prepared = prepare_launch_surface(
            request=request,
            runtime=runtime,
            prepared_policy=prepared_policy,
        )
    else:
        prepared = _build_direct_surface(
            request=request,
            project_root=project_paths.project_root,
            harness_registry=harness_registry,
        )

    bindings = RuntimeBindings(
        spawn_id=spawn_id,
        report_output_path=_resolve_report_output_path(
            runtime=runtime,
            project_paths=project_paths,
            spawn_id=spawn_id,
        ),
        runtime_work_id=runtime_work_id,
        plan_overrides=dict(plan_overrides or {}),
        dry_run=dry_run,
    )

    return bind_launch_context(
        prepared=prepared,
        bindings=bindings,
        runtime=runtime,
        project_root=project_paths.project_root,
        harness_registry=harness_registry,
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
    cache: MarsResultCache | None = None,
) -> LaunchContext:
    """Build deterministic launch context without leaking library warnings."""

    with capture_library_diagnostics():
        return _build_launch_context_impl(
            spawn_id=spawn_id,
            request=request,
            runtime=runtime,
            harness_registry=harness_registry,
            dry_run=dry_run,
            plan_overrides=plan_overrides,
            runtime_work_id=runtime_work_id,
            cache=cache,
        )


__all__ = [
    "ChildEnvContext",
    "LaunchContext",
    "MaterializedLaunchArtifacts",
    "PreparedLaunchContent",
    "PreparedLaunchSurface",
    "PreparedPolicySurface",
    "PreparedPromptPayload",
    "RuntimeBindings",
    "TaskContextInputs",
    "bind_launch_context",
    "build_child_runtime_env_overrides",
    "build_launch_context",
    "compile_prepared_policy_surface",
    "materialize_launch_artifacts",
    "merge_env_overrides",
    "normalize_usage_model_family",
    "prepare_launch_surface",
    "prepare_prompt_payload",
    "resolve_task_context_inputs",
]
