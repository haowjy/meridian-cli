"""Config file management operations."""

import json
import os
from pathlib import Path
from typing import Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, field_serializer

from meridian.lib.catalog.mars_roots import mars_fallback_label
from meridian.lib.config.context_config import ContextSourceType
from meridian.lib.config.dynamic_sections import DYNAMIC_SECTION_DESCRIPTORS
from meridian.lib.config.loader import OPTION_CATALOG
from meridian.lib.config.models import MeridianConfig, PrimaryConfig
from meridian.lib.config.preserving_edit import (
    mutate_project_config,
    reset_scalar_option,
    set_scalar_option,
)
from meridian.lib.config.project_config_state import (
    ProjectConfigState,
    resolve_project_config_state,
)
from meridian.lib.config.schema import (
    ConfigOptionDescriptor,
    normalize_runtime_scalar,
    parse_cli_scalar,
)
from meridian.lib.config.workspace import WorkspaceFinding
from meridian.lib.core.util import FormatContext, to_jsonable
from meridian.lib.hooks.config import DEFAULT_SOURCE, load_hooks_config_report
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
from meridian.lib.state.paths import (
    RuntimePaths,
    load_context_config,
    resolve_project_paths_for_write,
    resolve_project_runtime_root_for_write,
)
from meridian.lib.state.project_identity import (
    AdHoc,
    mint_committed_identity,
    resolve_project_identity,
)
from meridian.lib.state.user_paths import get_project_home, get_user_home

_MISSING_PROJECT_CONFIG_MESSAGE = "no project config here; run 'meridian init'"
logger = structlog.get_logger(__name__)


class ConfigInitInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None


class ConfigInitOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    created: bool
    note: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        status = "created" if self.created else "exists"
        result = f"{status}: {self.path}"
        if self.note is not None:
            result += f"\nnote: {self.note}"
        return result


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
    project_identity: str | None
    project_identity_source: Literal["meridian.toml", "path", "unminted"]
    mars_root: str
    mars_scope: Literal["project", "user"]
    mars_fallback: Literal["active", "unsupported", "n/a"]
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
        lines.append(f"project_identity: {self.project_identity or '(unminted)'}")
        lines.append(f"project_identity_source: {self.project_identity_source}")
        lines.append(f"mars_root: {self.mars_root}")
        lines.append(f"mars_scope: {self.mars_scope}")
        lines.append(f"mars_fallback: {self.mars_fallback}")
        if self.runtime_root is not None:
            lines.append(f"runtime_root: {self.runtime_root}")
        if self.runtime_root_source is not None:
            lines.append(f"runtime_root.source = {self.runtime_root_source}")
        lines.append(f"workspace.status = {self.workspace.status}")
        lines.append(
            "workspace.sources = " + json.dumps(list(self.workspace.sources), sort_keys=True)
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


def _source_for_path(
    surface: ConfigSurface,
    path: tuple[str, ...],
) -> tuple[Literal["builtin", "file", "user-config", "env var"], str | None]:
    source = next((entry for entry in surface.config_sources if entry.path == path), None)
    if source is None:
        return "builtin", None
    return source.source, source.env_var


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
            "# -- Timeout behavior -------------------------------------------------------",
            "[timeouts]",
            "# Grace period before force-killing processes (float minutes).",
            f"# kill_grace_minutes = {defaults['timeouts.kill_grace_minutes']}",
            "# Max minutes to wait for guardrail checks.",
            f"# guardrail_minutes = {defaults['timeouts.guardrail_minutes']}",
            "# Max minutes to wait on run completion operations.",
            f"# wait_minutes = {defaults['timeouts.wait_minutes']}",
            "# Max minutes for backend boot, connection, and session handshake.",
            f"# startup_minutes = {defaults['timeouts.startup_minutes']}",
            "",
            "# -- Spawn behavior ---------------------------------------------------------",
            "[spawn]",
            "# Per-spawn overrides for wait yield (also accepted under [timeouts]).",
            "# default_wait_yield_seconds = 3000.0",
            "# min_wait_yield_seconds = 30.0",
            "# Harnesses blocked from headless `meridian spawn`. Empty allows every harness.",
            '# deny_headless_harnesses = ["claude"]',
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
            "# Model override for the primary agent.",
            '# model = ""',
            "# Harness override for the primary agent.",
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


_SECRET_ENV_KEY_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET")


def _looks_like_secret_env_key(key: str) -> bool:
    normalized = key.upper()
    return any(normalized.endswith(suffix) for suffix in _SECRET_ENV_KEY_SUFFIXES)


def _dynamic_config_values(surface: ConfigSurface) -> list[ConfigResolvedValue]:
    values: list[ConfigResolvedValue] = []
    resolved = surface.resolved_dynamic_config

    if resolved.context is not None:
        for context_name, context_value in sorted(resolved.context.items()):
            for field_name in sorted(context_value.model_fields_set):
                values.append(
                    ConfigResolvedValue(
                        key=f"context.{context_name}.{field_name}",
                        value=getattr(context_value, field_name),
                        source=_source_for_path(
                            surface,
                            ("context", context_name, field_name),
                        )[0],
                    )
                )

    if resolved.work is not None and resolved.work.artifacts is not None:
        artifacts = resolved.work.artifacts
        for field_name in sorted(artifacts.model_fields_set):
            values.append(
                ConfigResolvedValue(
                    key=f"work.artifacts.{field_name}",
                    value=getattr(artifacts, field_name),
                    source=_source_for_path(
                        surface,
                        ("work", "artifacts", field_name),
                    )[0],
                )
            )

    if resolved.hooks is not None:
        report = load_hooks_config_report(
            surface.project_root,
            authority=surface.authority,
            user_config=surface.user_config_path,
        )
        configured_names = tuple(
            dict.fromkeys(
                hook.name
                for hook in report.config.hooks
                if hook.source != DEFAULT_SOURCE
            )
        )
        count = len(configured_names)
        rendered_hook_value: object = f"{count} hook" + ("" if count == 1 else "s")
        if configured_names:
            rendered_hook_value += f" ({', '.join(configured_names)})"
        if report.errors:
            invalid_count = len(report.errors)
            rendered_hook_value += (
                f"; {invalid_count} invalid row" + ("" if invalid_count == 1 else "s")
            )
        values.append(
            ConfigResolvedValue(
                key="hooks",
                value=rendered_hook_value,
                source=_source_for_path(surface, ("hooks",))[0],
            )
        )

    env_config = surface.resolved_config.env
    if env_config:
        for env_key, env_value in sorted(env_config.items()):
            display_value: object = env_value
            if _looks_like_secret_env_key(env_key):
                display_value = "[redacted]"
            values.append(
                ConfigResolvedValue(
                    key=f"env.{env_key}",
                    value=display_value,
                    source=_source_for_path(surface, ("env", env_key))[0],
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
    if (
        repo_state.kb_dir is None
        or repo_state.work_dir is None
        or repo_state.work_archive_dir is None
    ):
        raise ValueError("Project context paths are unresolved after identity creation.")
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


def ensure_state_bootstrap_sync(project_root: Path) -> ConfigInitOutput:
    """Atomically establish committed config identity, then bootstrap its state."""

    project_root = project_root.expanduser().resolve()
    prior_identity = resolve_project_identity(project_root)
    prior_adhoc_root = (
        get_project_home(prior_identity.id)
        if isinstance(prior_identity, AdHoc) and prior_identity.materialized
        else None
    )
    state = _resolve_project_config_state(project_root)
    created = state.path is None
    mint_committed_identity(project_root, _scaffold_template())

    ensure_runtime_state_bootstrap_sync(project_root)
    note = (
        f"earlier ad-hoc state at {prior_adhoc_root.as_posix()} is not carried over"
        if prior_adhoc_root is not None
        else None
    )
    return ConfigInitOutput(path=state.write_path.as_posix(), created=created, note=note)


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
    surface = build_config_surface(authority)

    values: list[ConfigResolvedValue] = []
    for option in OPTION_CATALOG.visible_options:
        source, env_var = _source_for_path(surface, option.field_path)
        values.append(
            ConfigResolvedValue(
                key=option.canonical_key,
                value=normalize_runtime_scalar(
                    _get_field_value(surface.resolved_config, option.field_path)
                ),
                source=source,
                env_var=env_var,
            )
        )

    values.extend(_dynamic_config_values(surface))

    return ConfigShowOutput(
        path=surface.project_config.write_path.as_posix(),
        project_root=surface.project_root.as_posix(),
        project_root_source=surface.authority.project_root_source,
        project_identity=surface.authority.project_id,
        project_identity_source=surface.authority.project_identity_source,
        mars_root=surface.authority.mars_roots.primary.as_posix(),
        mars_scope=surface.authority.mars_roots.scope,
        mars_fallback=mars_fallback_label(surface.authority.mars_roots),
        runtime_root=(
            surface.authority.runtime_root.as_posix()
            if surface.authority.runtime_root is not None
            else None
        ),
        runtime_root_source=surface.authority.runtime_root_source,
        workspace=surface.workspace,
        values=tuple(values),
        workspace_findings=surface.workspace_findings,
        warning=surface.warning,
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

    def edit(text: str) -> tuple[str, None]:
        result = set_scalar_option(text, option=option, value=value)
        return result.text, None

    mutate_project_config(
        project_root,
        get_user_home(),
        edit,
    )

    return ConfigSetOutput(
        path=path.as_posix(),
        key=option.canonical_key,
        value=normalize_runtime_scalar(value),
    )


def config_get_sync(payload: ConfigGetInput) -> ConfigGetOutput:
    authority = _resolve_project_authority(payload.project_root)
    project_root = authority.project_root
    option = _resolve_option(payload.key)
    surface = build_config_surface(project_root)
    source, env_var = _source_for_path(surface, option.field_path)

    return ConfigGetOutput(
        key=option.canonical_key,
        value=normalize_runtime_scalar(
            _get_field_value(surface.resolved_config, option.field_path)
        ),
        source=source,
        env_var=env_var,
    )


def config_reset_sync(payload: ConfigResetInput) -> ConfigResetOutput:
    project_root = _resolve_project_root(payload.project_root)
    path = _require_project_config_path(_resolve_project_config_state(project_root))
    option = _resolve_option(payload.key)

    def edit(text: str):
        result = reset_scalar_option(text, option=option)
        return result.text, result

    edit_result = mutate_project_config(
        project_root,
        get_user_home(),
        edit,
    )

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
