"""Shared launch-context assembly used by subprocess and streaming runners."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog
from pydantic import ValidationError

from meridian.lib.catalog.agent import AgentMode
from meridian.lib.catalog.agent_copy import agent_copy_enabled
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.context_config import (
    ArbitraryContextConfig,
    ContextConfig,
    ContextSourceType,
)
from meridian.lib.config.harness_models import PiHarnessProfileConfig
from meridian.lib.config.loader import (
    load_config,
    resolve_allow_builtin_agents_for_launch,
    resolve_opencode_version_for_launch,
    resolve_pi_harness_profile_for_launch,
)
from meridian.lib.config.models import MeridianConfig
from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.config.workspace import get_projectable_roots
from meridian.lib.context.resolver import resolve_context_paths
from meridian.lib.core.caller_boundary import (
    CALLER_BOUNDARY_ENV,
    CallerBoundary,
    current_pid_namespace,
)
from meridian.lib.core.child_env import validate_child_env_keys
from meridian.lib.core.context_ref import ContextRef, render_context_refs
from meridian.lib.core.execution_policy import NativeConfig
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.adapter import SpawnParams, SubprocessHarness
from meridian.lib.harness.bundle import (
    bind_launch_spec,
    get_harness_bundle,
    materialize_launch_spec,
    project_effective_policy,
)
from meridian.lib.launch.launch_types import (
    CompositionWarning,
    PreparedLaunchContent,
    PreparedLaunchRuntimeSeeds,
    PreparedPromptPayload,
    ResolvedLaunchBinding,
    ResolvedLaunchEnvironment,
    ResolvedLaunchSpec,
    summarize_composition_warnings,
)
from meridian.lib.launch.request import cross_harness_continue_error
from meridian.lib.launch.workspace_projection import (
    project_workspace_roots,
)
from meridian.lib.platform import IS_POSIX
from meridian.lib.safety.policy import (
    EnforcementReport,
    PermissionPolicy,
    build_permission_policy,
)
from meridian.lib.state import work_store
from meridian.lib.state.paths import (
    load_context_config,
    resolve_ambient_work_dir,
    resolve_kb_dir,
    resolve_project_paths,
    resolve_spawn_log_dir,
)
from meridian.lib.state.spawn.model import LaunchMode
from meridian.lib.state.user_paths import ensure_project_lifetime_lock
from meridian.lib.state.work_scope import resolve_bound_work_scope
from meridian.lib.streaming.control_socket import socket_directory
from meridian.lib.telemetry import emit_telemetry
from meridian.lib.tools import ToolRule
from meridian.plugin_api.git import resolve_clone_path

from .command import (
    build_launch_argv,
    normalize_system_prompt_passthrough_args,
    resolve_launch_spec_stage,
)
from .composition import (
    ComposedLaunchContent,
    CompositionBlock,
    ProjectedContent,
    build_inline_file_contributions,
    build_reference_routing,
    composed_content_for_bind_refresh,
    has_capabilities_block,
    replace_capabilities_block,
)
from .constants import DRY_RUN_REPORT_PATH, REPORT_FILENAME, SYSTEM_PROMPT_FILENAME
from .cwd import LaunchDirectoryContext
from .env import build_env_plan, merge_env_overrides, undo_direnv
from .permissions import launch_permission_rules
from .policies import (
    ModelSelectionContext,
    ResolvedLaunchPolicy,
    SurfacePolicyInput,
    resolve_launch_policy,
)
from .policy_snapshot import build_launch_policy_snapshot, overlay_continue_model_selection
from .prompt import (
    build_goal_instruction,
    build_primary_preamble,
    build_report_instruction,
    build_spawn_preamble,
    build_work_goal_instruction,
)
from .prompt_context import compose_skill_prompt_documents
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
    is_exact_continue_session,
)
from .resolve import (
    format_missing_skills_warning,
    resolve_profile_path,
    resolve_skill_paths,
)
from .spawn_guidance import (
    build_capabilities_prompt,
    build_guidance_blocks,
    build_spawn_usage_contract,
    has_spawn_capability,
)
from .text_utils import sanitize_prior_output, strip_stale_report_paths
from .workspace import resolve_workspace_snapshot_for_launch

if TYPE_CHECKING:
    from meridian.lib.harness.registry import HarnessRegistry


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ChildEnvContext:
    """Sole producer for child `MERIDIAN_*` environment overrides.

    Thin projection of ``ResolvedContext``; child-specific rule: ambient work
    dirs bind to the concrete ``child_spawn_id`` at emit time, not the parent's
    ambient ``MERIDIAN_ACTIVE_WORK_DIR``.
    """

    resolved: ResolvedContext

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
        resolved = ResolvedContext.from_environment(
            explicit_work_id=normalized_explicit_work_id,
            explicit_project_root=resolved_project_root,
            explicit_runtime_root=resolved_runtime_root,
            context_config=context_config,
        )
        return cls(resolved=resolved)

    @property
    def parent_spawn_id(self) -> str | None:
        return str(self.resolved.spawn_id) if self.resolved.spawn_id else None

    @property
    def project_root(self) -> Path:
        assert self.resolved.project_root is not None
        return self.resolved.project_root

    @property
    def runtime_root(self) -> Path:
        assert self.resolved.runtime_root is not None
        return self.resolved.runtime_root

    @property
    def parent_chat_id(self) -> str | None:
        return self.resolved.chat_id.strip() or None

    @property
    def parent_depth(self) -> int:
        return self.resolved.depth

    @property
    def work_id(self) -> str | None:
        return self.resolved.work_id

    @property
    def work_dir(self) -> Path | None:
        if self.resolved.work_id is None:
            return None
        return self.resolved.work_dir

    @property
    def context_dirs(self) -> tuple[tuple[str, Path], ...]:
        return self.resolved.context_dirs

    def child_context(
        self,
        *,
        child_spawn_id: str | None = None,
        increment_depth: bool = True,
    ) -> dict[str, str]:
        work_dir = self.resolved.work_dir
        if self.resolved.work_id is None and child_spawn_id is not None:
            work_dir = resolve_ambient_work_dir(
                self.project_root,
                child_spawn_id,
                runtime_root=self.runtime_root,
            )
        elif self.resolved.work_id is None:
            work_dir = None

        ctx = replace(self.resolved, work_dir=work_dir)
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

    def enforcement_report(self) -> EnforcementReport:
        """What this launch's harness enforces from the permission policy."""

        return get_harness_bundle(self.harness.id).projections.permission_report(self.binding.spec)


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
    profile_tool_rules: tuple[ToolRule, ...]
    has_spawn_capability: bool
    profile_mode: AgentMode
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
    def seed_harness_session_id(self) -> str | None:
        return self.runtime_seeds.seed_harness_session_id

    @property
    def seed_session_args(self) -> tuple[str, ...]:
        return self.runtime_seeds.seed_session_args


@dataclass(frozen=True)
class MaterializedLaunchArtifacts:
    """Shared launch materialization below policy resolution."""

    run_params: SpawnParams
    spec: ResolvedLaunchSpec
    warnings: tuple[CompositionWarning, ...] = ()


@dataclass(frozen=True)
class RuntimeBindings:
    """Runtime-only values for the bind phase."""

    spawn_id: str
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


def resolve_task_context_inputs(
    *,
    context_refs: tuple[ContextRef, ...],
    reference_files: tuple[str, ...],
    authority_root: Path,
    reference_anchor: Path,
) -> TaskContextInputs:
    """Render resolved ``--from`` refs and load ``-f`` files into user-turn context blocks."""

    resolved_authority_root = authority_root.expanduser().resolve()
    resolved_reference_anchor = reference_anchor.expanduser().resolve()
    loaded_references = load_reference_items(
        reference_files,
        reference_anchor=resolved_reference_anchor,
        kb_dir=resolve_kb_dir(resolved_authority_root),
    )
    prior_output = render_context_refs(context_refs)
    return TaskContextInputs(
        reference_items=loaded_references,
        prior_output=sanitize_prior_output(prior_output) if prior_output.strip() else "",
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


_INLINE_FILE_REFS_WARNING_THRESHOLD = 5

_INLINE_FILE_REFS_WARNING_MESSAGE = (
    "Many file refs will be inlined and may drain context; "
    "prefer folder refs or a design/context artifact."
)


def _build_inline_file_refs_warning(content: PreparedLaunchContent) -> CompositionWarning | None:
    """Warn when multiple file references with actual content route inline into prompt context.

    Warning-only refs (binary, oversized, etc.) are excluded to avoid false positives
    since they contribute metadata, not file content.
    """

    reference_items = content.loaded_references
    if not reference_items:
        return None

    reference_routing = (
        content.projected_content.reference_routing
        if content.projected_content is not None
        else build_reference_routing(reference_items)
    )
    contributions = build_inline_file_contributions(
        reference_items, reference_routing, exclude_warning_only=True
    )
    if len(contributions) < _INLINE_FILE_REFS_WARNING_THRESHOLD:
        return None

    return CompositionWarning(
        code="inline_file_refs_context_risk",
        message=_INLINE_FILE_REFS_WARNING_MESSAGE,
        detail={
            "inline_file_reference_count": str(len(contributions)),
            "total_inline_file_bytes": str(
                sum(contribution.byte_count for contribution in contributions)
            ),
        },
    )


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
    autocompact: int | None,
    autocompact_pct: int | None,
    skills: tuple[str, ...],
    agent: str | None,
    prompt_payload: PreparedPromptPayload,
    extra_args: tuple[str, ...] = (),
    control_root: str | None = None,
    task_cwd: str | None = None,
    projected_roots: tuple[Path, ...] = (),
    interactive: bool = False,
    continue_harness_session_id: str | None = None,
    continue_fork: bool = False,
    model_override_explicit: bool = False,
    reference_items: tuple[ReferenceItem, ...] = (),
    permission_policy: PermissionPolicy,
    native_config: NativeConfig,
    pi_harness_profile: PiHarnessProfileConfig | None = None,
) -> MaterializedLaunchArtifacts:
    """Build shared run/spec/permission launch artifacts."""

    run_params = SpawnParams(
        prompt=prompt,
        model=ModelId(model) if model else None,
        model_override_explicit=model_override_explicit,
        effort=effort,
        autocompact=autocompact,
        autocompact_pct=autocompact_pct,
        skills=skills,
        agent=agent,
        adhoc_agent_payload=prompt_payload.adhoc_agent_payload,
        extra_args=extra_args,
        control_root=control_root,
        task_cwd=task_cwd,
        projected_roots=projected_roots,
        interactive=interactive,
        continue_harness_session_id=continue_harness_session_id,
        continue_fork=continue_fork,
        appended_system_prompt=prompt_payload.appended_system_prompt,
        reference_items=reference_items,
        user_turn_content=prompt_payload.user_turn_content,
        pi_harness_profile=pi_harness_profile,
    )
    spec = resolve_launch_spec_stage(
        adapter=harness,
        run_inputs=run_params,
        permission_policy=permission_policy,
        native_config=native_config,
    )
    return MaterializedLaunchArtifacts(
        run_params=run_params,
        spec=spec,
        warnings=harness.launch_spec_warnings(spec),
    )


def _resolve_deny_headless_harnesses(
    runtime: LaunchRuntime,
    project_paths: ProjectConfigPaths,
) -> tuple[str, ...]:
    """Resolve ``[spawn].deny_headless_harnesses`` from config."""
    if runtime.config_snapshot:
        try:
            return tuple(
                MeridianConfig.model_validate(runtime.config_snapshot).deny_headless_harnesses
            )
        except Exception:
            pass
    return tuple(load_config(project_paths.project_root).deny_headless_harnesses)


def _enforce_headless_harness_policy(
    *,
    request: SpawnRequest,
    harness: SubprocessHarness,
    runtime: LaunchRuntime,
    project_paths: ProjectConfigPaths,
) -> None:
    """Reject denied headless harnesses before a spawn can be persisted."""

    if (
        runtime.composition_surface != LaunchCompositionSurface.SPAWN_PREPARE
        or harness.id.value not in _resolve_deny_headless_harnesses(runtime, project_paths)
    ):
        return

    parent_harness = os.getenv("_MERIDIAN_HARNESS", "")
    if parent_harness == harness.id.value:
        agent_hint = (
            f' Use your native Agent(subagent_type="{request.agent}") tool to delegate instead.'
            if request.agent
            else " Use your native Agent() tool to delegate instead."
        )
    else:
        agent_hint = (
            " Use a different model with '-m <model>' to route to an allowed harness,"
            " or override with '--harness <harness>'."
        )
    raise ValueError(
        f"Headless spawns on the '{harness.id.value}' harness are denied by "
        f"'[spawn] deny_headless_harnesses' in meridian.toml."
        f"{agent_hint}"
    )


def _missing_continue_session_error(source_ref: str | None) -> str:
    normalized_source = (source_ref or "").strip()
    return (
        f"{normalized_source or 'Source reference'} has no verified native session; "
        "cannot continue/fork."
    )


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
    builtins = (
        ("work", resolved.work_root),
        ("work_archive", resolved.work_archive),
        ("kb", resolved.kb_root),
    )
    candidates: list[tuple[str, Path]] = [
        (name, path) for name, path in builtins if path is not None
    ]
    candidates.extend((name, path) for name, (path, _source) in resolved.extra.items())

    # Only project context roots that exist on disk. Sandboxed harnesses (codex
    # bubblewrap) bind-mount every projected root at exec time and abort the whole
    # namespace if a source path is missing — so a not-yet-created context dir
    # (commonly `work_archive` before the first archive) would silently break all
    # command execution in the worker. Skipping the missing root is correct (there
    # is nothing to mount). Logged at debug, not warning: a missing context dir is
    # the normal pre-archive state, so it must not pollute stderr on every launch.
    roots: list[Path] = []
    for name, path in candidates:
        if path.exists():
            roots.append(path)
        else:
            logger.debug(
                "context_projection_root_skipped_missing",
                context_root=name,
                path=path.as_posix(),
            )

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


def normalize_explicit_work_id(
    *,
    request_work_id_hint: str | None = None,
    runtime_work_id: str | None = None,
) -> str | None:
    return (runtime_work_id or "").strip() or (request_work_id_hint or "").strip() or None


def _suppresses_ambient_work(request: SpawnRequest) -> bool:
    return is_exact_continue_session(request.session)


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
        validate_child_env_keys(runtime_overrides)
    return runtime_overrides


def _resolve_bound_active_work_dir(
    *,
    project_root: Path,
    requested_work_id: str | None,
    child_spawn_id: str,
    runtime_root: Path,
) -> Path | None:
    """Pure bind-time work-dir: named repo scratch or child ambient spawn dir."""

    scope = resolve_bound_work_scope(
        project_root=project_root,
        requested_work_id=requested_work_id,
        child_spawn_id=child_spawn_id,
        runtime_root=runtime_root,
    )
    return scope.root if scope is not None else None


def _reproject_capabilities_at_bind(
    *,
    composed_prompt: str,
    prompt_payload: PreparedPromptPayload,
    projected_content: ProjectedContent | None,
    composed_content: ComposedLaunchContent | None,
    harness: SubprocessHarness,
    project_root: Path,
    control_root: Path,
    task_dir: Path,
    bound_work_dir: Path | None,
    spawn_id: str,
    chat_id: str | None,
    work_id: str | None,
    spawn_capable: bool,
    profile_mode: AgentMode,
    primary_prompt_is_synthetic: bool = False,
) -> tuple[str, PreparedPromptPayload, ProjectedContent | None]:
    """Render the capability block with bound paths and re-project prompt channels."""

    if composed_content is None or not has_capabilities_block(composed_content.guidance_blocks):
        return composed_prompt, prompt_payload, projected_content

    capabilities = build_capabilities_prompt(
        spawn_capable=spawn_capable,
        profile_mode=profile_mode,
        project_root=project_root,
        control_root=control_root,
        task_dir=task_dir,
        active_work_dir=bound_work_dir,
        spawn_id=spawn_id,
        chat_id=chat_id,
        work_id=work_id,
    )
    refreshed_guidance = replace_capabilities_block(
        composed_content.guidance_blocks,
        new_content=capabilities,
    )
    refreshed_content = replace(composed_content, guidance_blocks=refreshed_guidance)
    updated_projected = harness.project_content(refreshed_content)
    updated_payload = prepare_prompt_payload(
        adhoc_agent_payload=prompt_payload.adhoc_agent_payload,
        projected_content=updated_projected,
    )
    if primary_prompt_is_synthetic:
        updated_payload = replace(updated_payload, user_turn_content=None)

    refreshed_prompt = (
        updated_projected.user_turn_content.strip() or refreshed_content.user_task_prompt
    )
    return refreshed_prompt, updated_payload, updated_projected


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
        chat_ref = (
            request.session.continue_chat_id
            or request.session.continue_source_ref
            or "the source chat"
        )
        raise ValueError(cross_harness_continue_error(chat_ref, requested_harness, str(harness.id)))
    if not harness.capabilities.supports_session_resume:
        raise ValueError(f"{harness.id} cannot resume sessions")
    if requested_continue_fork and not harness.capabilities.supports_session_fork:
        raise ValueError(
            f"{harness.id} cannot fork sessions; use --continue with the source chat "
            "or start fresh."
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
        resident_rearm_budget=request.execution_policy.resident_rearm_budget,
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
            models_readonly=dry_run,
            requested_skills=request.skills,
            network_overrides=request.network_overrides,
            policy_snapshot=request.launch_policy_snapshot,
            continuation=request.session if is_exact_continue_session(request.session) else None,
            agent_opt_out=request.agent_opt_out,
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


@dataclass(frozen=True)
class _SharedComposition:
    completion_contract: str
    guidance_blocks: tuple[CompositionBlock, ...]


def _build_shared_composition(
    *,
    request: SpawnRequest,
    project_paths: ProjectConfigPaths,
    policy: ResolvedLaunchPolicy,
) -> _SharedComposition:
    spawn_usage_contract = build_spawn_usage_contract(
        policy.adapter.run_prompt_policy().spawn_usage_contract_variants
    )
    network = policy.execution_policy.network
    assert network is not None
    guidance_blocks = build_guidance_blocks(
        profile=policy.profile,
        spawn_usage_contract=spawn_usage_contract,
        bundle_inventory_prompt=policy.bundle_inventory_prompt,
        harness_guidance=policy.adapter.shell_network_guidance(
            sandbox=policy.execution_policy.sandbox,
            network=network,
            native_config=policy.execution_policy.native_config,
        ),
    )
    resolved_work_id = (request.work_id_hint or "").strip()
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
    return _SharedComposition(
        completion_contract=completion_contract,
        guidance_blocks=guidance_blocks,
    )


def _resolve_spawn_prepare_projection(
    *,
    request: SpawnRequest,
    project_paths: ProjectConfigPaths,
    policy: ResolvedLaunchPolicy,
    runtime: LaunchRuntime,
    launch_mode: LaunchMode | None = None,
) -> PreparedLaunchContent:
    """Compose the spawn's semantic content from the request inputs and project it."""

    profile = policy.profile
    harness = policy.adapter
    resolved_skills = policy.resolved_skills

    task_ctx = resolve_task_context_inputs(
        context_refs=request.context_refs,
        reference_files=request.reference_files,
        authority_root=project_paths.project_root,
        reference_anchor=project_paths.execution_cwd,
    )

    resolved_template_variables = resolve_template_variables(request.template_vars)
    cleaned_user_prompt = substitute_template_variables(
        strip_stale_report_paths(request.prompt),
        resolved_template_variables,
    )

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

    shared = _build_shared_composition(
        request=request,
        project_paths=project_paths,
        policy=policy,
    )

    composed = ComposedLaunchContent(
        supplemental_documents=supplemental_documents,
        available_skills=policy.bundle_available_skills,
        agent_profile_body=agent_profile_body,
        report_instruction=build_report_instruction(),
        guidance_blocks=shared.guidance_blocks,
        completion_contract=shared.completion_contract,
        launch_preamble=build_spawn_preamble(launch_mode),
        passthrough_system_fragments=(),
        user_task_prompt=cleaned_user_prompt,
        reference_items=task_ctx.reference_items,
        prior_output=task_ctx.prior_output,
        skill_delivery=policy.skill_delivery,
    )

    projected = harness.project_content(composed)

    return PreparedLaunchContent(
        final_prompt=projected.user_turn_content.strip() or composed.user_task_prompt,
        loaded_references=composed.reference_items,
        prompt_payload=prepare_prompt_payload(projected_content=projected),
        projected_content=projected,
        composed_content=composed_content_for_bind_refresh(composed),
    )


def _resolve_primary_projection(
    *,
    request: SpawnRequest,
    project_paths: ProjectConfigPaths,
    policy: ResolvedLaunchPolicy,
    runtime: LaunchRuntime,
    resolved_continue_harness_session_id: str | None,
) -> tuple[PreparedLaunchContent, tuple[str, ...], str | None, tuple[str, ...]]:
    profile = policy.profile
    harness = policy.adapter
    resolved_skills = policy.resolved_skills
    session_mode = ((request.session.primary_session_mode or "fresh").strip().lower()) or "fresh"

    shared = _build_shared_composition(
        request=request,
        project_paths=project_paths,
        policy=policy,
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
        context_refs=request.context_refs,
        reference_files=request.reference_files,
        authority_root=project_paths.project_root,
        reference_anchor=project_paths.execution_cwd,
    )

    composed = ComposedLaunchContent(
        supplemental_documents=supplemental_documents,
        available_skills=policy.bundle_available_skills,
        agent_profile_body=agent_profile_body,
        report_instruction="",
        guidance_blocks=shared.guidance_blocks,
        completion_contract=shared.completion_contract,
        launch_preamble=build_primary_preamble(),
        passthrough_system_fragments=passthrough_system_fragments,
        user_task_prompt=request.prompt,
        reference_items=task_ctx.reference_items,
        prior_output=task_ctx.prior_output,
        skill_delivery=policy.skill_delivery,
    )

    projected = harness.project_content(composed)

    prompt_payload = prepare_prompt_payload(projected_content=projected)
    if request.primary_prompt_is_synthetic:
        prompt_payload = replace(prompt_payload, user_turn_content=None)

    return (
        PreparedLaunchContent(
            final_prompt=projected.user_turn_content.strip() or request.prompt,
            loaded_references=task_ctx.reference_items,
            prompt_payload=prompt_payload,
            projected_content=projected,
            composed_content=composed_content_for_bind_refresh(composed),
        ),
        passthrough_args,
        seed.session_id or resolved_continue_harness_session_id,
        seed.session_args,
    )


def _prepare_spawn_surface(
    *,
    request: SpawnRequest,
    project_paths: ProjectConfigPaths,
    policy: ResolvedLaunchPolicy,
    runtime: LaunchRuntime,
    launch_mode: LaunchMode | None = None,
) -> tuple[PreparedLaunchContent, ResolvedContinuation]:
    content = _resolve_spawn_prepare_projection(
        request=request,
        project_paths=project_paths,
        policy=policy,
        runtime=runtime,
        launch_mode=launch_mode,
    )

    return (
        content,
        _resolve_session_continuation(request=request, harness=policy.adapter),
    )


def _prepare_primary_surface(
    *,
    request: SpawnRequest,
    project_paths: ProjectConfigPaths,
    policy: ResolvedLaunchPolicy,
    runtime: LaunchRuntime,
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
    if (
        is_exact_continue_session(request.session)
        and policy.model
        and not policy.adapter.capabilities.supports_named_primary_resume
    ):
        raise ValueError(
            f"Harness '{policy.harness}' cannot preserve a named model on primary continuation. "
            "The conversation and recorded selection were not changed."
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
        policy=policy,
        runtime=runtime,
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
    launch_mode: LaunchMode | None = None,
) -> PreparedLaunchSurface:
    """Resolve the expensive, spawn-stable launch surface."""
    project_paths = prepared_policy.project_paths
    policies = prepared_policy.resolved_policy
    if request.agent_opt_out:
        policies = replace(
            policies,
            profile=None,
            routing=replace(policies.routing, agent=None),
        )
    profile = policies.profile
    using_policy_snapshot = request.launch_policy_snapshot is not None
    execution_policy = policies.execution_policy
    model_selection = policies.model_selection
    harness = policies.adapter
    resolved_skills = policies.resolved_skills

    route_warning = None
    content = PreparedLaunchContent(final_prompt=request.prompt)
    final_passthrough_args = request.extra_args
    continuation = ResolvedContinuation(harness_session_id=None, continue_fork=False)
    seed_harness_session_id: str | None = None
    seed_session_args: tuple[str, ...] = ()
    if runtime.composition_surface == LaunchCompositionSurface.SPAWN_PREPARE:
        content, continuation = _prepare_spawn_surface(
            request=request,
            project_paths=project_paths,
            policy=policies,
            runtime=runtime,
            launch_mode=launch_mode,
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
            policy=policies,
            runtime=runtime,
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
    inline_file_refs_warning = _build_inline_file_refs_warning(content)
    if inline_file_refs_warning is not None:
        composition_warnings = (*composition_warnings, inline_file_refs_warning)
    warning = summarize_composition_warnings(composition_warnings)

    agent_metadata = dict(request.agent_metadata)
    if request.agent_opt_out:
        for metadata_key in (
            "session_agent",
            "session_agent_description",
            "session_agent_profile_body",
            "session_agent_path",
        ):
            agent_metadata.pop(metadata_key, None)
    model_selection_update: dict[str, str | None] = {
        "model_selection_requested_token": None,
        "model_selection_canonical_id": None,
        "model_selection_harness_provenance": None,
    }
    if model_selection is not None:
        model_selection_update = {
            "model_selection_requested_token": model_selection.requested_token,
            "model_selection_canonical_id": model_selection.canonical_model_id,
            "model_selection_provider_constraint": model_selection.provider_constraint,
            "model_selection_harness_provenance": model_selection.harness_provenance,
        }
    if request.agent_opt_out:
        resolved_agent_name = None
    elif profile is not None:
        resolved_agent_name = profile.name
    else:
        resolved_agent_name = policies.routing.agent or request.agent
    session_agent_path = resolve_profile_path(profile)
    if resolved_agent_name is not None:
        agent_metadata["session_agent"] = resolved_agent_name
    if profile is not None and profile.description.strip():
        agent_metadata["session_agent_description"] = profile.description.strip()
    if profile is not None and profile.body.strip():
        agent_metadata["session_agent_profile_body"] = profile.body
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
        loaded_references=content.loaded_references,
        prompt_payload=PreparedPromptPayload(
            adhoc_agent_payload=adhoc_agent_payload,
            appended_system_prompt=content.prompt_payload.appended_system_prompt,
            user_turn_content=content.prompt_payload.user_turn_content,
        ),
        projected_content=content.projected_content,
        composed_content=content.composed_content,
    )
    profile_tool_rules = (
        policies.resolved_tool_rules if (profile is not None or using_policy_snapshot) else ()
    )

    resolved_request = request.model_copy(
        update={
            "model": policies.model,
            "harness": str(policies.harness),
            "agent": resolved_agent_name,
            "skills": resolved_skills.skill_names,
            "extra_args": final_passthrough_args,
            "execution_policy": execution_policy,
            # Prefer the bundle's authoritative resolved tool rules whenever they exist
            # (covers spawned subagents, where profile/snapshot aren't set at bind but the
            # resolved rules — e.g. a Codex web grant — must still propagate). Falls back
            # to request.tool_rules only when no rules were resolved.
            "tool_rules": policies.resolved_tool_rules or request.tool_rules,
            "platform_tool_rules": policies.platform_tool_rules,
            "session": request.session.model_copy(
                update={
                    "requested_harness_session_id": continuation.harness_session_id,
                    "continue_fork": continuation.continue_fork,
                }
            ),
            "warning": warning,
            "agent_metadata": agent_metadata,
            "prompt_payload": _request_prompt_payload(content.prompt_payload),
            "skill_paths": resolve_skill_paths(resolved_skills.loaded_skills),
            "selection_report": policies.selection_report,
            "terminal_surface_mode": policies.terminal_surface_mode,
            "matched_policy_rule": policies.matched_policy_rule,
            **model_selection_update,
        }
    )
    resolved_request = resolved_request.model_copy(
        update={
            "launch_policy_snapshot": (
                None if request.agent_opt_out else request.launch_policy_snapshot
            )
            or build_launch_policy_snapshot(
                resolved_request,
                model_selection=model_selection,
                loaded_skills=resolved_skills.loaded_skills,
                bundle_inventory_prompt=policies.bundle_inventory_prompt,
                profile=profile,
            )
        }
    )
    if is_exact_continue_session(request.session) and model_selection is not None:
        original = request.launch_policy_snapshot or resolved_request.launch_policy_snapshot
        assert original is not None
        resolved_request = resolved_request.model_copy(
            update={
                "launch_policy_snapshot": overlay_continue_model_selection(
                    original,
                    harness=str(policies.harness),
                    model_selection=model_selection,
                    selection_report=policies.selection_report,
                    field_provenance={
                        **original.field_provenance,
                        "model_source": policies.field_provenance.model_source.value,
                        "harness_source": policies.field_provenance.harness_source.value,
                    },
                ),
            }
        )
    elif (
        resolved_request.launch_policy_snapshot is not None
        and request.launch_policy_snapshot is None
    ):
        resolved_request = resolved_request.model_copy(
            update={
                "launch_policy_snapshot": resolved_request.launch_policy_snapshot.model_copy(
                    update={
                        "field_provenance": {
                            key: value.value
                            for key, value in asdict(policies.field_provenance).items()
                        },
                    }
                ),
            }
        )
    _enforce_headless_harness_policy(
        request=resolved_request,
        harness=harness,
        runtime=runtime,
        project_paths=project_paths,
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
        profile_tool_rules=profile_tool_rules,
        has_spawn_capability=has_spawn_capability(profile),
        profile_mode=profile.mode if profile is not None else "subagent",
        model_selection=model_selection,
        alias_catalog=policies.alias_catalog,
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

    _ = (harness_registry, bindings.chat_id)

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
    socket_roots = (socket_directory(),) if IS_POSIX else ()
    resolved_request = prepared.request
    ignore_direnv = resolved_request.ignore_direnv
    if ignore_direnv is None:
        config = (
            MeridianConfig.model_validate(runtime.config_snapshot)
            if runtime.config_snapshot
            else load_config(project_paths.project_root)
        )
        ignore_direnv = config.environment.ignore_direnv
    # Capture once before deriving any environment-dependent launch overlays.
    base_env = undo_direnv(os.environ) if ignore_direnv else dict(os.environ)
    harness = prepared.harness
    effective_session_id = bindings.forked_harness_session_id or prepared.seed_harness_session_id
    composition_warnings = prepared.composition_warnings
    prompt_payload = prepared.prompt_payload
    loaded_references = prepared.loaded_references
    profile_tool_rules = prepared.profile_tool_rules
    projected_content = prepared.projected_content
    seed_harness_session_args = prepared.seed_session_args
    model_selection = prepared.model_selection
    spawn_log_dir = resolve_spawn_log_dir(
        project_paths.project_root, bindings.spawn_id, runtime_root=runtime_root
    )
    report_artifact_path = spawn_log_dir / REPORT_FILENAME
    system_prompt_path = spawn_log_dir / SYSTEM_PROMPT_FILENAME
    logical_task_cwd = project_paths.execution_cwd.resolve()
    directory_context = LaunchDirectoryContext(
        authority_root=resolved_control_root,
        logical_task_cwd=logical_task_cwd,
        reference_anchor=logical_task_cwd,
        actual_process_cwd=logical_task_cwd,
        task_cwd_source=(resolved_request.task_cwd_source or "").strip() or "authority-root",
        work_item=(resolved_request.task_cwd_work_item or "").strip() or None,
    )
    task_cwd = (
        directory_context.logical_task_cwd if directory_context.has_distinct_task_cwd else None
    )
    # Invariant: harness subprocess cwd always stays at control_root.
    # Distinct task directories are exposed via task_cwd/env/instructions only.
    child_cwd = resolved_control_root

    workspace_authority_roots = _dedupe_roots_in_order(
        (
            *workspace_roots,
            *git_context_roots,
            *context_projection_roots,
        )
    )
    if task_cwd is not None and not _is_task_cwd_covered_by_projection(
        task_cwd=task_cwd,
        control_root=resolved_control_root,
        projected_roots=workspace_authority_roots,
    ):
        workspace_authority_roots = _dedupe_roots_in_order(
            (
                *workspace_authority_roots,
                task_cwd,
            )
        )
    projected_roots = _dedupe_roots_in_order(
        (
            *workspace_authority_roots,
            runtime_root,
            system_temp_root,
            *socket_roots,
        )
    )
    workspace_projection = project_workspace_roots(
        harness_id=harness.id,
        roots=projected_roots,
    )
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

    task_cwd_projection_missing = task_cwd is not None and (
        workspace_projection.applicability != "active"
        or not _is_task_cwd_covered_by_projection(
            task_cwd=task_cwd,
            control_root=resolved_control_root,
            projected_roots=workspace_authority_roots,
        )
    )
    if task_cwd_projection_missing:
        assert task_cwd is not None
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

    directory_context = directory_context.with_actual_process_cwd(child_cwd)

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

    allow_builtin_agents = resolve_allow_builtin_agents_for_launch(
        harness_id=harness.id.value,
        config_snapshot=runtime.config_snapshot,
        project_root=project_paths.project_root,
    )
    native_agent_copy = agent_copy_enabled(project_paths.project_root, harness.id)
    platform_tool_rules, platform_rules = launch_permission_rules(
        tool_rules=resolved_request.tool_rules,
        profile_tool_rules=profile_tool_rules,
        platform_tool_rules=resolved_request.platform_tool_rules,
        spawn=runtime.composition_surface == LaunchCompositionSurface.SPAWN_PREPARE,
        has_spawn_capability=prepared.has_spawn_capability,
        native_agent_copy=native_agent_copy,
        allow_builtin_agents=allow_builtin_agents,
    )
    network = resolved_request.execution_policy.network
    assert network is not None
    permission_policy = build_permission_policy(
        rules=(*resolved_request.tool_rules, *platform_tool_rules),
        sandbox=resolved_request.execution_policy.sandbox,
        network=network,
        sandbox_write_roots=tuple(
            root.as_posix()
            for root in _dedupe_roots_in_order((resolved_control_root, *workspace_authority_roots))
        ),
        approval=resolved_request.execution_policy.approval or "default",
        platform_rules=platform_rules,
    )

    # Defense in depth for persisted worker requests that bypassed preparation.
    _enforce_headless_harness_policy(
        request=resolved_request,
        harness=harness,
        runtime=runtime,
        project_paths=project_paths,
    )

    is_primary_launch = runtime.composition_surface == LaunchCompositionSurface.PRIMARY
    pi_harness_profile = (
        resolve_pi_harness_profile_for_launch(
            config_snapshot=runtime.config_snapshot,
            project_root=project_paths.project_root,
        )
        if harness.id == HarnessId.PI
        else None
    )
    requested_work_id = normalize_explicit_work_id(
        request_work_id_hint=resolved_request.work_id_hint,
        runtime_work_id=bindings.runtime_work_id,
    )
    bound_active_work_dir = _resolve_bound_active_work_dir(
        project_root=project_paths.project_root,
        requested_work_id=requested_work_id,
        child_spawn_id=bindings.spawn_id,
        runtime_root=runtime_root,
    )
    refreshed_prompt, prompt_payload, projected_content = _reproject_capabilities_at_bind(
        composed_prompt=resolved_request.prompt,
        prompt_payload=prompt_payload,
        projected_content=projected_content,
        composed_content=prepared.content.composed_content,
        harness=harness,
        project_root=project_paths.project_root,
        control_root=resolved_control_root,
        task_dir=directory_context.logical_task_cwd,
        bound_work_dir=bound_active_work_dir,
        spawn_id=bindings.spawn_id,
        chat_id=bindings.chat_id or base_env.get("MERIDIAN_CHAT_ID"),
        work_id=requested_work_id,
        spawn_capable=prepared.has_spawn_capability,
        profile_mode=prepared.profile_mode,
        primary_prompt_is_synthetic=resolved_request.primary_prompt_is_synthetic,
    )
    conversation_intent = resolved_request.session.conversation_intent
    model_override_explicit = (
        conversation_intent is None or conversation_intent.selection_source == "explicit_override"
    )
    materialized = materialize_launch_artifacts(
        harness=harness,
        prompt=refreshed_prompt,
        model=effective_model,
        model_override_explicit=model_override_explicit,
        effort=resolved_request.execution_policy.effort,
        autocompact=resolved_request.execution_policy.autocompact,
        autocompact_pct=resolved_request.execution_policy.autocompact_pct,
        skills=resolved_request.skills,
        agent=resolved_request.agent,
        prompt_payload=prompt_payload,
        extra_args=tuple(resolved_request.extra_args),
        control_root=resolved_control_root.as_posix(),
        task_cwd=(task_cwd.as_posix() if task_cwd is not None else None),
        projected_roots=projected_roots,
        interactive=is_primary_launch,
        continue_harness_session_id=effective_session_id,
        continue_fork=(
            bindings.continue_fork_override
            if bindings.continue_fork_override is not None
            else resolved_request.session.continue_fork
        ),
        reference_items=loaded_references,
        permission_policy=permission_policy,
        native_config=resolved_request.execution_policy.native_config,
        pi_harness_profile=pi_harness_profile,
    )
    run_params = materialized.run_params
    spec = materialized.spec
    composition_warnings = (*composition_warnings, *materialized.warnings)
    opencode_version_preference: str | None = None
    if harness.id == HarnessId.OPENCODE:
        # The config preference reaches the launch env below, where bind resolves it
        # against the installed binary (``bind_launch_spec``) and descendants inherit it.
        opencode_version_preference = resolve_opencode_version_for_launch(
            config_snapshot=runtime.config_snapshot,
            project_root=project_paths.project_root,
        )
    if harness.id == HarnessId.CODEX and not spec.interactive:
        spec = spec.model_copy(
            update={
                "report_output_path": (
                    DRY_RUN_REPORT_PATH if bindings.dry_run else report_artifact_path.as_posix()
                )
            }
        )
    elif harness.id == HarnessId.CLAUDE:
        spec = spec.model_copy(update={"prompt_file_path": system_prompt_path.as_posix()})
    launch_env_overrides: dict[str, str] = {}
    # Resolve before restoring the environment, then carry the decision to
    # descendants even when direnv originally supplied the setting itself.
    launch_env_overrides["MERIDIAN_IGNORE_DIRENV"] = str(ignore_direnv).lower()
    if opencode_version_preference is not None:
        launch_env_overrides["MERIDIAN_HARNESS_OPENCODE_VERSION"] = opencode_version_preference
    child_context_env = build_child_runtime_env_overrides(
        project_paths=project_paths,
        runtime_root=runtime_root,
        child_spawn_id=bindings.spawn_id,
        work_id=requested_work_id,
        increment_depth=not is_primary_launch,
        additional_overrides=launch_env_overrides or None,
    )
    if requested_work_id is None and _suppresses_ambient_work(resolved_request):
        child_context_env.pop("MERIDIAN_ACTIVE_WORK_ID", None)
        child_context_env.pop("MERIDIAN_ACTIVE_WORK_DIR", None)
    effective_work_id = (
        child_context_env.get("MERIDIAN_ACTIVE_WORK_ID", "").strip() or requested_work_id
    )
    # Informational: tells the child its own harness for yield timing.
    # Not a policy override — from_env() does not read it back.
    child_context_env["_MERIDIAN_HARNESS"] = harness.id.value
    child_context_env["MERIDIAN_SESSION_ROLE"] = "primary" if is_primary_launch else "spawn"
    resolved_agent = (resolved_request.agent or "").strip()
    if resolved_agent:
        child_context_env["MERIDIAN_SESSION_AGENT"] = resolved_agent
    child_context_env["MERIDIAN_PROJECT_DIR"] = resolved_control_root.as_posix()
    # Override inherited task-dir with the child's resolved task-dir.
    # child_env_overrides() carries the parent's MERIDIAN_TASK_DIR;
    # bind resolves the child's logical_task_cwd and overwrites.
    child_context_env["MERIDIAN_TASK_DIR"] = directory_context.logical_task_cwd.as_posix()
    if harness.id == HarnessId.PI:
        child_context_env["_MERIDIAN_PI_STATE_DIR"] = runtime_root.as_posix()
    runtime_override_env = (
        runtime.resolved_runtime_overrides.to_env()
        if runtime.has_runtime_override_snapshot
        else RuntimeOverrides.from_env().to_env()
    )
    bind_env_overrides = merge_env_overrides(
        plan_overrides=bindings.plan_overrides,
        runtime_overrides={**runtime_override_env, **child_context_env},
    )
    env = build_env_plan(
        base_env=base_env,
        adapter=harness,
        run_inputs=run_params,
        runtime_env_overrides=bind_env_overrides,
    )
    intent = harness.plan_native_identity(
        run_params, preforked_session_id=bindings.forked_harness_session_id
    )
    if intent is not None:
        spec = spec.model_copy(
            update={
                "native_identity": harness.finalize_native_identity(
                    intent,
                    child_env=env,
                    child_cwd=child_cwd,
                    session=resolved_request.session,
                    spawn_id=SpawnId(bindings.spawn_id),
                    interactive=run_params.interactive,
                )
            }
        )
    # Native resume/fork may pin a recorded harness home in ``env``. Resolve
    # environment-dependent adapter facts only after that final namespace is known.
    spec = bind_launch_spec(
        harness.id,
        spec,
        env=env,
        child_cwd=child_cwd,
    )
    environment_warnings = harness.launch_environment_warnings(
        child_env=env,
        child_cwd=child_cwd,
    )
    if environment_warnings:
        composition_warnings = (*composition_warnings, *environment_warnings)
        resolved_request = resolved_request.model_copy(
            update={"warning": summarize_composition_warnings(composition_warnings)}
        )
    caller_boundary = CallerBoundary.from_policy(
        permission_policy,
        effective=project_effective_policy(
            harness.id,
            permission_policy,
            native_config=resolved_request.execution_policy.native_config,
        ),
        pid_namespace=current_pid_namespace(),
    ).to_env_value()
    child_context_env[CALLER_BOUNDARY_ENV] = caller_boundary
    bind_env_overrides[CALLER_BOUNDARY_ENV] = caller_boundary
    env[CALLER_BOUNDARY_ENV] = caller_boundary
    validate_child_env_keys(child_context_env)
    env.update(
        harness.project_launch_env(
            spec=spec,
            system_prompt_path=system_prompt_path,
            env=env,
        )
    )
    environment = ResolvedLaunchEnvironment.build(
        child_context_env=child_context_env,
        plan_env=dict(bindings.plan_overrides),
        runtime_override_env=runtime_override_env,
        bind_env_overrides=bind_env_overrides,
        runner_overlay_env={},
        final_env=env,
    )
    argv: tuple[str, ...] = ()
    if runtime.argv_intent != LaunchArgvIntent.SPEC_ONLY:
        argv = build_launch_argv(adapter=harness, projected_spec=spec)

    binding = ResolvedLaunchBinding(
        work_id=effective_work_id,
        child_cwd=child_cwd,
        run_params=run_params,
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
        execution_cwd=directory_context.logical_task_cwd,
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


def materialize_launch_context(context: LaunchContext) -> LaunchContext:
    """Perform real-launch-only adapter writes after pure context binding."""

    ensure_project_lifetime_lock(context.runtime_root)
    binding = context.binding
    spec = materialize_launch_spec(
        context.harness.id,
        binding.spec,
        env=binding.environment.final_env,
        child_cwd=binding.child_cwd,
    )
    if spec is binding.spec:
        return context
    return replace(context, binding=replace(binding, spec=spec))


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
    "build_launch_policy_snapshot",
    "compile_prepared_policy_surface",
    "materialize_launch_artifacts",
    "materialize_launch_context",
    "normalize_usage_model_family",
    "prepare_launch_surface",
    "prepare_prompt_payload",
    "resolve_task_context_inputs",
]
