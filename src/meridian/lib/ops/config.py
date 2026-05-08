"""Config file management operations."""

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, field_serializer

from meridian.lib.config.context_config import ContextSourceType
from meridian.lib.config.preserving_edit import reset_scalar_option, set_scalar_option
from meridian.lib.config.project_config_state import (
    ProjectConfigState,
    resolve_project_config_state,
)
from meridian.lib.config.schema import (
    ConfigOptionDescriptor,
    normalize_runtime_scalar,
    parse_cli_scalar,
    parse_toml_scalar,
)
from meridian.lib.config.settings import (
    DYNAMIC_SECTION_DESCRIPTORS,
    OPTION_CATALOG,
    MeridianConfig,
    PrimaryConfig,
    merge_dynamic_sections,
    normalize_dynamic_sections,
)
from meridian.lib.config.workspace import WorkspaceFinding
from meridian.lib.context import auto_migrate_contexts
from meridian.lib.core.util import FormatContext, to_jsonable
from meridian.lib.ops.config_surface import (
    ConfigSurface,
    ConfigSurfaceWorkspace,
    build_config_surface,
)
from meridian.lib.ops.runtime import (
    RuntimeAuthoritySnapshot,
    async_from_sync,
    resolve_runtime_authority_for_read,
)
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import (
    RuntimePaths,
    ensure_gitignore,
    load_context_config,
    resolve_project_paths_for_write,
    resolve_project_runtime_root_for_write,
)

_MISSING_PROJECT_CONFIG_MESSAGE = "no project config; run `meridian config init`"
_LOCAL_CONFIG_FILENAME = "meridian.local.toml"
logger = structlog.get_logger(__name__)





class ConfigInitInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None


class ConfigInitOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    created: bool

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        status = "created" if self.created else "exists"
        return f"{status}: {self.path}"


class ConfigShowInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None


class ConfigResolvedValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    value: object
    source: Literal["builtin", "file", "user-config", "env var"]
    env_var: str | None = None


class ConfigShowOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    project_root: str
    project_root_source: str
    runtime_root: str | None = None
    runtime_root_source: str | None = None
    workspace: ConfigSurfaceWorkspace
    values: tuple[ConfigResolvedValue, ...]
    workspace_findings: tuple[WorkspaceFinding, ...] = ()
    warning: str | None = None

    @field_serializer("workspace")
    def _serialize_workspace(self, value: ConfigSurfaceWorkspace) -> dict[str, object]:
        return value.model_dump(exclude={"roots_detail"} if not value.roots_detail else None)

    def format_text(self, ctx: FormatContext | None = None) -> str:
        verbosity = 0 if ctx is None else ctx.verbosity
        lines = [f"path: {self.path}"]
        lines.append(f"project_root: {self.project_root}")
        lines.append(f"project_root.source = {self.project_root_source}")
        if self.runtime_root is not None:
            lines.append(f"runtime_root: {self.runtime_root}")
        if self.runtime_root_source is not None:
            lines.append(f"runtime_root.source = {self.runtime_root_source}")
        lines.append(f"workspace.status = {self.workspace.status}")
        lines.append(
            "workspace.sources = "
            + json.dumps(list(self.workspace.sources), sort_keys=True)
        )
        lines.append(f"workspace.roots.count = {self.workspace.roots.count}")
        lines.append(f"workspace.roots.projected = {self.workspace.roots.projected}")
        lines.append(f"workspace.roots.skipped = {self.workspace.roots.skipped}")
        for harness in ("claude", "codex", "opencode"):
            applicability = self.workspace.applicability.get(harness)
            if applicability is None:
                continue
            lines.append(f"workspace.applicability.{harness} = {applicability}")
        if verbosity > 0:
            for index, root in enumerate(self.workspace.roots_detail):
                lines.append(f"workspace.roots[{index}].name = {root.name}")
                lines.append(f"workspace.roots[{index}].source = {root.source}")
                lines.append(f"workspace.roots[{index}].declared_path = {root.declared_path}")
                lines.append(f"workspace.roots[{index}].resolved_path = {root.resolved_path}")
                lines.append(f"workspace.roots[{index}].status = {root.status}")
        if self.warning is not None:
            lines.append(f"warning: {self.warning}")
        for finding in self.workspace_findings:
            lines.append(f"warning: {finding.code}: {finding.message}")
        for item in self.values:
            source_note = item.source
            if item.env_var is not None:
                source_note = f"{source_note} ({item.env_var})"
            lines.append(
                f"{item.key}: {_format_value_for_text(item.value)} [source: {source_note}]"
            )
        return "\n".join(lines)


class ConfigSetInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    value: str
    project_root: str | None = None


class ConfigSetOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    key: str
    value: object

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return f"set {self.key} = {_format_value_for_text(self.value)} in {self.path}"


class ConfigGetInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    project_root: str | None = None


class ConfigGetOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    value: object
    source: Literal["builtin", "file", "user-config", "env var"]
    env_var: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        source_note = self.source if self.env_var is None else f"{self.source} ({self.env_var})"
        return f"{self.key}: {_format_value_for_text(self.value)} [source: {source_note}]"


class ConfigResetInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    project_root: str | None = None


class ConfigResetOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    key: str
    removed: bool

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        status = "removed" if self.removed else "already-default"
        return f"reset {self.key} ({status}) in {self.path}"


@dataclass(frozen=True)
class _ConfigInspectionState:
    surface: ConfigSurface
    project_overrides: dict[str, object]
    user_overrides: dict[str, object]
    resolved_values: dict[str, object]
    project_dynamic_overrides: dict[str, object]
    user_dynamic_overrides: dict[str, object]
    resolved_dynamic_overrides: dict[str, object]


def _resolve_project_config_state(project_root: Path) -> ProjectConfigState:
    return resolve_project_config_state(project_root)


def _require_project_config_path(state: ProjectConfigState) -> Path:
    if state.path is None:
        raise ValueError(_MISSING_PROJECT_CONFIG_MESSAGE)
    return state.path


def _resolve_project_authority(project_root: str | None) -> RuntimeAuthoritySnapshot:
    explicit = Path(project_root).expanduser().resolve() if project_root else None
    return resolve_runtime_authority_for_read(explicit)


def _resolve_project_root(project_root: str | None) -> Path:
    return _resolve_project_authority(project_root).project_root


def _resolve_option(key: str) -> ConfigOptionDescriptor:
    return OPTION_CATALOG.resolve_key(key)


def _get_field_value(config: MeridianConfig, field_path: tuple[str, ...]) -> object:
    current: object = config
    for part in field_path:
        current = getattr(current, part)
    return current


def _default_values() -> dict[str, object]:
    defaults = MeridianConfig()
    return {
        option.canonical_key: normalize_runtime_scalar(
            _get_field_value(defaults, option.field_path)
        )
        for option in OPTION_CATALOG.visible_options
    }


def _resolved_values(config: MeridianConfig) -> dict[str, object]:
    return {
        option.canonical_key: normalize_runtime_scalar(
            _get_field_value(config, option.field_path)
        )
        for option in OPTION_CATALOG.visible_options
    }


def _read_file_payload(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    payload_obj = tomllib.loads(path.read_text(encoding="utf-8"))
    return cast("dict[str, object]", payload_obj)


def _extract_file_overrides(payload: dict[str, object]) -> dict[str, object]:
    overrides: dict[str, object] = {}

    def visit(table_path: tuple[str, ...], table: dict[str, object]) -> None:
        for key, raw_value in table.items():
            if isinstance(raw_value, dict):
                visit((*table_path, key), cast("dict[str, object]", raw_value))
                continue

            option = OPTION_CATALOG.find_file_alias(table_path=table_path, key=key)
            if option is None or not option.command_visible:
                continue

            source = ".".join((*table_path, key)) if table_path else key
            overrides[option.canonical_key] = parse_toml_scalar(
                value_kind=option.value_kind,
                raw_value=raw_value,
                source=source,
            )

    visit((), payload)
    return overrides


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list | tuple):
        items = cast("list[object] | tuple[object, ...]", value)
        return "[" + ", ".join(_toml_literal(item) for item in items) + "]"
    raise ValueError(f"Unsupported config value type: {type(value).__name__}")


def _source_for_key(
    option: ConfigOptionDescriptor,
    *,
    project_overrides: dict[str, object],
    user_overrides: dict[str, object],
) -> tuple[Literal["builtin", "file", "user-config", "env var"], str | None]:
    for env_var in option.env_vars:
        if os.getenv(env_var) is not None:
            return "env var", env_var
    if option.canonical_key in project_overrides:
        return "file", None
    if option.canonical_key in user_overrides:
        return "user-config", None
    return "builtin", None


def _build_config_inspection_state(
    authority: RuntimeAuthoritySnapshot | Path,
) -> _ConfigInspectionState:
    surface = build_config_surface(authority)
    project_root = surface.project_root
    project_payload = _read_file_payload(surface.project_config.write_path)
    project_overrides = _extract_file_overrides(project_payload)
    project_dynamic_overrides = normalize_dynamic_sections(
        payload=project_payload,
        project_root=project_root,
    )
    local_config_path = project_root / _LOCAL_CONFIG_FILENAME
    if local_config_path.is_file():
        local_payload = _read_file_payload(local_config_path)
        local_overrides = _extract_file_overrides(local_payload)
        project_overrides = {**project_overrides, **local_overrides}
        project_dynamic_overrides = merge_dynamic_sections(
            project_dynamic_overrides,
            normalize_dynamic_sections(payload=local_payload, project_root=project_root),
        )
    if surface.user_config_path is not None:
        user_payload = _read_file_payload(surface.user_config_path)
        user_overrides = _extract_file_overrides(user_payload)
        user_dynamic_overrides = normalize_dynamic_sections(
            payload=user_payload,
            project_root=project_root,
        )
    else:
        user_overrides = {}
        user_dynamic_overrides = {}
    resolved_values = _resolved_values(surface.resolved_config)
    resolved_dynamic_overrides = merge_dynamic_sections(
        user_dynamic_overrides,
        project_dynamic_overrides,
    )
    return _ConfigInspectionState(
        surface=surface,
        project_overrides=project_overrides,
        user_overrides=user_overrides,
        resolved_values=resolved_values,
        project_dynamic_overrides=project_dynamic_overrides,
        user_dynamic_overrides=user_dynamic_overrides,
        resolved_dynamic_overrides=resolved_dynamic_overrides,
    )


def _format_value_for_text(value: object) -> str:
    payload = to_jsonable(value)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, sort_keys=True)


def _scaffold_template() -> str:
    defaults = _default_values()
    primary_defaults = PrimaryConfig()
    sections: tuple[tuple[str, ...], ...] = (
        (
            "# Meridian configuration.",
            "# All values shown are built-in defaults. Uncomment to override.",
            "# Environment variables (MERIDIAN_*) take precedence over file values.",
            "",
            "# -- Execution defaults -----------------------------------------------------",
            "[defaults]",
            "# Maximum agent nesting depth (int).",
            f"# max_depth = {defaults['defaults.max_depth']}",
            "# Retry attempts per failed spawn (int).",
            f"# max_retries = {defaults['defaults.max_retries']}",
            "# Delay multiplier between retries in seconds (float).",
            f"# retry_backoff_seconds = {defaults['defaults.retry_backoff_seconds']}",
            "# Default model for spawns when --model and profile model are both unset.",
            f"# model = {_toml_literal(cast('str', defaults['defaults.model']))}",
            "# Default harness for spawns when higher-precedence values are unset.",
            f"# harness = {_toml_literal(cast('str', defaults['defaults.harness']))}",
            "",
            "# -- Timeout behavior -------------------------------------------------------",
            "[timeouts]",
            "# Grace period before force-killing processes (float minutes).",
            f"# kill_grace_minutes = {defaults['timeouts.kill_grace_minutes']}",
            "# Max minutes to wait for guardrail checks.",
            f"# guardrail_minutes = {defaults['timeouts.guardrail_minutes']}",
            "# Max minutes to wait on run completion operations.",
            f"# wait_minutes = {defaults['timeouts.wait_minutes']}",
            "",
            "# -- Spawn behavior ---------------------------------------------------------",
            "[spawn]",
            "# Per-spawn overrides for wait yield (also accepted under [timeouts]).",
            "# default_wait_yield_seconds = 3000.0",
            "# min_wait_yield_seconds = 30.0",
            "",
            "# -- Harness default models -------------------------------------------------",
            "[harness]",
            "# Default model for Claude harness (empty = harness picks its own).",
            f"# claude = {_toml_literal(cast('str', defaults['harness.claude']))}",
            "# Default model for Codex harness (empty = harness picks its own).",
            f"# codex = {_toml_literal(cast('str', defaults['harness.codex']))}",
            "# Default model for OpenCode harness.",
            f"# opencode = {_toml_literal(cast('str', defaults['harness.opencode']))}",
            "",
            "# -- Primary agent defaults -------------------------------------------------",
            "[primary]",
            "# Default agent profile launched when running `meridian` without `-a`.",
            "# Without this, meridian launches with no profile.",
            '# agent = ""',
            "# Model override for the primary agent (unset = use defaults.model).",
            '# model = ""',
            "# Harness override for the primary agent (unset = use defaults.harness).",
            '# harness = ""',
            "# Context compaction threshold for the primary agent (int 1-100).",
            f"# autocompact = {primary_defaults.autocompact or 65}",
            "# Effort level for the primary agent.",
            '# effort = ""',
            "# Sandbox policy for the primary agent.",
            '# sandbox = ""',
            "# Approval mode for the primary agent.",
            '# approval = ""',
            "# Timeout for the primary agent (minutes).",
            f"# timeout = {primary_defaults.timeout or 30.0}",
            "",
            "# -- Output streaming -------------------------------------------------------",
            "[output]",
            "# Event categories shown while streaming output.",
            f"# show = {_toml_literal(cast('tuple[str, ...]', defaults['output.show']))}",
            "# Output verbosity preset (quiet, normal, verbose, debug).",
            '# verbosity = ""',
            "# Output format (text, json).",
            '# format = "text"',
            "",
            "# -- State retention --------------------------------------------------------",
            "[state]",
            "# Days to retain spawn artifacts and session data (-1 = keep forever).",
            f"# retention_days = {defaults['state.retention_days']}",
            "",
        ),
        *(
            descriptor.scaffold_lines
            for descriptor in DYNAMIC_SECTION_DESCRIPTORS.values()
            if descriptor.scaffold_lines
        ),
    )
    return "\n".join(line for section in sections for line in section)


def _dynamic_value_source(
    *,
    path: tuple[str, ...],
    project_dynamic_overrides: dict[str, object],
    user_dynamic_overrides: dict[str, object],
) -> Literal["builtin", "file", "user-config", "env var"]:
    current: object = project_dynamic_overrides
    for part in path:
        if not isinstance(current, dict) or part not in current:
            break
        current = cast("dict[str, object]", current)[part]
    else:
        return "file"

    current = user_dynamic_overrides
    for part in path:
        if not isinstance(current, dict) or part not in current:
            break
        current = cast("dict[str, object]", current)[part]
    else:
        return "user-config"

    return "builtin"


def _dynamic_config_values(inspection: _ConfigInspectionState) -> list[ConfigResolvedValue]:
    values: list[ConfigResolvedValue] = []
    resolved = inspection.resolved_dynamic_overrides

    context_section = resolved.get("context")
    if isinstance(context_section, dict):
        context_items = sorted(cast("dict[str, object]", context_section).items())
        for context_name, context_value in context_items:
            if not isinstance(context_value, dict):
                continue
            for field_name, field_value in sorted(cast("dict[str, object]", context_value).items()):
                values.append(
                    ConfigResolvedValue(
                        key=f"context.{context_name}.{field_name}",
                        value=field_value,
                        source=_dynamic_value_source(
                            path=("context", context_name, field_name),
                            project_dynamic_overrides=inspection.project_dynamic_overrides,
                            user_dynamic_overrides=inspection.user_dynamic_overrides,
                        ),
                    )
                )

    work_section = resolved.get("work")
    if isinstance(work_section, dict):
        artifacts = cast("dict[str, object]", work_section).get("artifacts")
        if isinstance(artifacts, dict):
            for field_name, field_value in sorted(cast("dict[str, object]", artifacts).items()):
                values.append(
                    ConfigResolvedValue(
                        key=f"work.artifacts.{field_name}",
                        value=field_value,
                        source=_dynamic_value_source(
                            path=("work", "artifacts", field_name),
                            project_dynamic_overrides=inspection.project_dynamic_overrides,
                            user_dynamic_overrides=inspection.user_dynamic_overrides,
                        ),
                    )
                )

    agents_section = resolved.get("agents")
    if isinstance(agents_section, dict):
        for agent_name, overlay_value in sorted(cast("dict[str, object]", agents_section).items()):
            if not isinstance(overlay_value, dict):
                continue
            overlay = cast("dict[str, object]", overlay_value)
            for field_name in (
                "model",
                "harness",
                "effort",
                "approval",
                "sandbox",
                "autocompact",
            ):
                if field_name not in overlay:
                    continue
                values.append(
                    ConfigResolvedValue(
                        key=f"agents.{agent_name}.{field_name}",
                        value=overlay[field_name],
                        source=_dynamic_value_source(
                            path=("agents", agent_name, field_name),
                            project_dynamic_overrides=inspection.project_dynamic_overrides,
                            user_dynamic_overrides=inspection.user_dynamic_overrides,
                        ),
                    )
                )

            policies = overlay.get("model_policies")
            if policies is None:
                continue
            if not isinstance(policies, list | tuple):
                continue
            if len(policies) == 0:
                rendered_policy_value = "[] (suppressed)"
            else:
                rules_desc: list[str] = []
                for policy_value in cast("list[object] | tuple[object, ...]", policies):
                    if not isinstance(policy_value, dict):
                        continue
                    policy = cast("dict[str, object]", policy_value)
                    overrides = policy.get("overrides")
                    override_items = (
                        sorted(cast("dict[str, object]", overrides).items())
                        if isinstance(overrides, dict)
                        else []
                    )
                    override_keys = ", ".join(f"{key}={value}" for key, value in override_items)
                    rules_desc.append(
                        f'match: {policy.get("match_type")} "{policy.get("match_value")}"'
                        + (f" → {override_keys}" if override_keys else "")
                    )
                rendered_policy_value = (
                    f"{len(policies)} rules ({'; '.join(rules_desc)})"
                    if rules_desc
                    else f"{len(policies)} rules"
                )
            values.append(
                ConfigResolvedValue(
                    key=f"agents.{agent_name}.model-policies",
                    value=rendered_policy_value,
                    source=_dynamic_value_source(
                        path=("agents", agent_name, "model_policies"),
                        project_dynamic_overrides=inspection.project_dynamic_overrides,
                        user_dynamic_overrides=inspection.user_dynamic_overrides,
                    ),
                )
            )

    if "hooks" in resolved:
        hooks_section = resolved.get("hooks")
        if isinstance(hooks_section, list | tuple):
            hook_rows = cast("list[object] | tuple[object, ...]", hooks_section)
            if len(hook_rows) == 0:
                rendered_hook_value: object = "[] (suppressed)"
            else:
                names: list[str] = []
                for row_value in hook_rows:
                    if not isinstance(row_value, dict):
                        continue
                    row = cast("dict[str, object]", row_value)
                    label = row.get("name") or row.get("event") or row.get("builtin") or "hook"
                    names.append(str(label))
                rendered_hook_value = (
                    f"{len(hook_rows)} hooks ({', '.join(names)})"
                    if names
                    else len(hook_rows)
                )
            values.append(
                ConfigResolvedValue(
                    key="hooks",
                    value=rendered_hook_value,
                    source=_dynamic_value_source(
                        path=("hooks",),
                        project_dynamic_overrides=inspection.project_dynamic_overrides,
                        user_dynamic_overrides=inspection.user_dynamic_overrides,
                    ),
                )
            )

    return values

def _has_non_empty_remote(remote: str | None) -> bool:
    return isinstance(remote, str) and bool(remote.strip())


def ensure_runtime_state_bootstrap_sync(project_root: Path) -> None:
    """Ensure first-run runtime state exists without creating project-root files.

    For git-backed contexts with configured remotes, we skip directory creation
    here since git-autosync hooks handle cloning and directory setup. This
    avoids creating non-git directories at clone paths before the actual clone
    happens.
    """
    context_config = load_context_config(project_root)

    repo_state = resolve_project_paths_for_write(project_root)
    auto_migrate_contexts(repo_state.root_dir)

    # Always create the root .meridian directory
    repo_state.root_dir.mkdir(parents=True, exist_ok=True)

    # For context directories, skip only git-backed contexts with remotes.
    if context_config is None:
        # No context config = all local, create everything
        repo_state.kb_dir.mkdir(parents=True, exist_ok=True)
        repo_state.work_dir.mkdir(parents=True, exist_ok=True)
        repo_state.work_archive_dir.mkdir(parents=True, exist_ok=True)
    else:
        kb_git_with_remote = (
            context_config.kb.source == ContextSourceType.GIT
            and _has_non_empty_remote(context_config.kb.remote)
        )
        work_git_with_remote = (
            context_config.work.source == ContextSourceType.GIT
            and _has_non_empty_remote(context_config.work.remote)
        )
        if context_config.kb.source == ContextSourceType.GIT and not kb_git_with_remote:
            logger.warning(
                "context_source_git_missing_remote_fallback_local",
                context="kb",
                configured_remote=context_config.kb.remote,
            )
        if context_config.work.source == ContextSourceType.GIT and not work_git_with_remote:
            logger.warning(
                "context_source_git_missing_remote_fallback_local",
                context="work",
                configured_remote=context_config.work.remote,
            )

        if not kb_git_with_remote:
            repo_state.kb_dir.mkdir(parents=True, exist_ok=True)
        if not work_git_with_remote:
            repo_state.work_dir.mkdir(parents=True, exist_ok=True)
            repo_state.work_archive_dir.mkdir(parents=True, exist_ok=True)

    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_state = RuntimePaths.from_root_dir(runtime_root)
    runtime_dirs = (
        runtime_state.root_dir,
        runtime_state.spawns_dir,
    )
    for dir_path in runtime_dirs:
        dir_path.mkdir(parents=True, exist_ok=True)
    ensure_gitignore(project_root)


def ensure_state_bootstrap_sync(project_root: Path) -> ConfigInitOutput:
    """Ensure runtime state exists and scaffold project config when missing."""

    ensure_runtime_state_bootstrap_sync(project_root)
    state = _resolve_project_config_state(project_root)
    if state.path is not None:
        return ConfigInitOutput(path=state.path.as_posix(), created=False)

    atomic_write_text(state.write_path, _scaffold_template())
    return ConfigInitOutput(path=state.write_path.as_posix(), created=True)


def config_init_sync(payload: ConfigInitInput) -> ConfigInitOutput:
    # init targets explicit path, then MERIDIAN_PROJECT_DIR, then CWD.
    if payload.project_root:
        project_root = Path(payload.project_root).expanduser().resolve()
    else:
        env_root = os.getenv("MERIDIAN_PROJECT_DIR", "").strip()
        project_root = Path(env_root).expanduser().resolve() if env_root else Path.cwd().resolve()
    return ensure_state_bootstrap_sync(project_root)


def config_show_sync(payload: ConfigShowInput) -> ConfigShowOutput:
    authority = _resolve_project_authority(payload.project_root)
    inspection = _build_config_inspection_state(authority)

    values: list[ConfigResolvedValue] = []
    for option in OPTION_CATALOG.visible_options:
        source, env_var = _source_for_key(
            option,
            project_overrides=inspection.project_overrides,
            user_overrides=inspection.user_overrides,
        )
        values.append(
            ConfigResolvedValue(
                key=option.canonical_key,
                value=inspection.resolved_values[option.canonical_key],
                source=source,
                env_var=env_var,
            )
        )

    values.extend(_dynamic_config_values(inspection))

    return ConfigShowOutput(
        path=inspection.surface.project_config.write_path.as_posix(),
        project_root=inspection.surface.project_root.as_posix(),
        project_root_source=inspection.surface.authority.project_root_source,
        runtime_root=(
            inspection.surface.authority.runtime_root.as_posix()
            if inspection.surface.authority.runtime_root is not None
            else None
        ),
        runtime_root_source=inspection.surface.authority.runtime_root_source,
        workspace=inspection.surface.workspace,
        values=tuple(values),
        workspace_findings=inspection.surface.workspace_findings,
        warning=inspection.surface.warning,
    )


def config_set_sync(payload: ConfigSetInput) -> ConfigSetOutput:
    project_root = _resolve_project_root(payload.project_root)
    path = _require_project_config_path(_resolve_project_config_state(project_root))

    option = _resolve_option(payload.key)
    value = parse_cli_scalar(
        canonical_key=option.canonical_key,
        value_kind=option.value_kind,
        raw_value=payload.value,
    )

    edit_result = set_scalar_option(
        path.read_text(encoding="utf-8"),
        option=option,
        value=value,
    )
    atomic_write_text(path, edit_result.text)

    return ConfigSetOutput(
        path=path.as_posix(),
        key=option.canonical_key,
        value=normalize_runtime_scalar(value),
    )


def config_get_sync(payload: ConfigGetInput) -> ConfigGetOutput:
    project_root = _resolve_project_root(payload.project_root)
    option = _resolve_option(payload.key)
    inspection = _build_config_inspection_state(project_root)
    source, env_var = _source_for_key(
        option,
        project_overrides=inspection.project_overrides,
        user_overrides=inspection.user_overrides,
    )

    return ConfigGetOutput(
        key=option.canonical_key,
        value=inspection.resolved_values[option.canonical_key],
        source=source,
        env_var=env_var,
    )


def config_reset_sync(payload: ConfigResetInput) -> ConfigResetOutput:
    project_root = _resolve_project_root(payload.project_root)
    path = _require_project_config_path(_resolve_project_config_state(project_root))
    option = _resolve_option(payload.key)
    edit_result = reset_scalar_option(path.read_text(encoding="utf-8"), option=option)
    atomic_write_text(path, edit_result.text)

    return ConfigResetOutput(
        path=path.as_posix(),
        key=option.canonical_key,
        removed=edit_result.removed,
    )


config_init = async_from_sync(config_init_sync)
config_show = async_from_sync(config_show_sync)
config_set = async_from_sync(config_set_sync)
config_get = async_from_sync(config_get_sync)
config_reset = async_from_sync(config_reset_sync)
