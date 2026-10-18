"""TOML and environment loading for Meridian configuration."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.config.catalog import build_option_catalog
from meridian.lib.config.dynamic_sections import (
    DYNAMIC_SECTION_DESCRIPTORS,
    DynamicConfigSections,
    merge_dynamic_sections,
    merge_nested_dict_values,
    normalize_dynamic_sections,
)
from meridian.lib.config.errors import (
    config_validation_error,
    validation_error_path,
    warn_unknown_model_keys,
)
from meridian.lib.config.harness_models import HarnessProfileConfig, PiHarnessProfileConfig
from meridian.lib.config.layers import iter_config_layers, read_config_payload
from meridian.lib.config.model_base import ConfigModel
from meridian.lib.config.models import (
    MeridianConfig,
)
from meridian.lib.config.project_paths import (
    ProjectConfigPaths,
)
from meridian.lib.config.project_root import resolve_user_config_path
from meridian.lib.config.schema import parse_env_scalar, parse_toml_scalar
from meridian.lib.utils.json_shapes import is_json_object

logger = logging.getLogger(__name__)

_LEGACY_AGENTS_SECTION = "agents"
_FLATTENED_MODEL_SECTIONS = frozenset({"defaults", "spawn", "timeouts"})


class _ProjectAuthorityLike(Protocol):
    project_root: Path
    project_config_paths: ProjectConfigPaths


ConfigSourceName = Literal["builtin", "file", "user-config", "env var"]


class ConfigValueSource(BaseModel):
    """Provenance for one validated config field path."""

    model_config = ConfigDict(frozen=True)

    path: tuple[str, ...]
    source: ConfigSourceName
    env_var: str | None = None


class LoadedConfig(BaseModel):
    """Resolved config plus typed dynamic sections and field provenance."""

    model_config = ConfigDict(frozen=True)

    config: MeridianConfig
    dynamic: DynamicConfigSections
    sources: tuple[ConfigValueSource, ...] = ()

    def source_for(self, path: tuple[str, ...]) -> ConfigValueSource | None:
        return next((entry for entry in self.sources if entry.path == path), None)


@dataclass(frozen=True)
class _NormalizedConfigLayer:
    values: dict[str, object]
    dynamic: dict[str, object]


OPTION_CATALOG = build_option_catalog(MeridianConfig)


def _assign_nested_value(
    target: dict[str, object],
    path: tuple[str, ...],
    value: object,
) -> None:
    current = target
    for part in path[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            replacement: dict[str, object] = {}
            current[part] = replacement
            current = replacement
            continue
        current = cast("dict[str, object]", nested)
    current[path[-1]] = value


def _map_catalog_table(
    raw_value: dict[str, object],
    *,
    table_path: tuple[str, ...],
    target: dict[str, object],
) -> None:
    """Map file aliases to model fields without validating their values."""

    for key, value in raw_value.items():
        option = OPTION_CATALOG.find_file_alias(table_path=table_path, key=key)
        if option is not None:
            _assign_nested_value(target, option.field_path, value)
            continue

        if isinstance(value, dict):
            _map_catalog_table(
                cast("dict[str, object]", value),
                table_path=(*table_path, key),
                target=target,
            )
            continue

        logger.warning(
            "Ignoring unknown Meridian config key '%s'.",
            ".".join((*table_path, key)),
        )


def _nested_model_type(annotation: object) -> type[ConfigModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, ConfigModel):
        return annotation
    return None


def _section_model(section: str) -> type[ConfigModel] | None:
    field = MeridianConfig.model_fields.get(section)
    if field is None:
        return None
    return _nested_model_type(field.annotation)


def _legacy_agents_section_error(*, path: Path) -> ValueError:
    message = (
        f"Legacy section '[{_LEGACY_AGENTS_SECTION}]' is not supported in '{path}'. "
        "[agents] in meridian.toml is no longer supported. Agent overlays moved to mars.toml: "
        "define [agents.<name>] under mars.toml or mars.local.toml. See docs/configuration.md "
        "for the new schema."
    )
    return ValueError(message)


def _normalize_toml_payload(
    *,
    payload: dict[str, object],
    path: Path,
    validate_hooks: bool = True,
) -> _NormalizedConfigLayer:
    try:
        return _normalize_toml_payload_values(
            payload=payload,
            path=path,
            validate_hooks=validate_hooks,
        )
    except ValueError as error:
        message = str(error)
        if str(path) in message:
            raise
        raise ValueError(f"{message.removesuffix('.')} in '{path}'.") from None


def _normalize_toml_payload_values(
    *,
    payload: dict[str, object],
    path: Path,
    validate_hooks: bool,
) -> _NormalizedConfigLayer:
    normalized: dict[str, object] = {}
    dynamic: dict[str, object] = {}
    for key, raw_value in payload.items():
        if key == "project":
            # Identity is read by the precedence-exempt minimal loader.
            continue
        if key == "hooks" and not validate_hooks:
            continue
        if key == _LEGACY_AGENTS_SECTION:
            raise _legacy_agents_section_error(path=path)
        if key in DYNAMIC_SECTION_DESCRIPTORS:
            section = normalize_dynamic_sections(
                payload={key: raw_value},
            )
            normalized = merge_dynamic_sections(normalized, section)
            dynamic = merge_dynamic_sections(dynamic, section)
            continue

        section_model = _section_model(key)
        if section_model is not None:
            warn_unknown_model_keys(raw_value, model=section_model, source=key)
            normalized[key] = raw_value
            continue

        if key in _FLATTENED_MODEL_SECTIONS:
            if not is_json_object(raw_value):
                raise ValueError(f"Invalid value for '{key}' in '{path}': expected table.")
            _map_catalog_table(raw_value, table_path=(key,), target=normalized)
            continue

        option = OPTION_CATALOG.find_file_alias(table_path=(), key=key)
        if option is None:
            logger.warning("Ignoring unknown Meridian config key '%s'.", key)
            continue

        coerced = parse_toml_scalar(
            value_kind=option.value_kind,
            raw_value=raw_value,
            source=key,
        )
        _assign_nested_value(normalized, option.field_path, coerced)

    try:
        values = MeridianConfig.model_validate(normalized).model_dump(exclude_unset=True)
    except ValidationError as error:
        raise config_validation_error(
            error, path_formatter=_catalog_validation_error_path
        ) from None
    return _NormalizedConfigLayer(values=values, dynamic=dynamic)


def _env_alias_overrides() -> tuple[dict[str, object], tuple[ConfigValueSource, ...]]:
    values: dict[str, object] = {}
    sources: list[ConfigValueSource] = []
    for env_name, option in OPTION_CATALOG.env_options():
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue

        parsed = parse_env_scalar(
            value_kind=option.value_kind,
            raw_value=raw_value,
            env_name=env_name,
        )
        _assign_nested_value(values, option.field_path, parsed)
        sources.append(
            ConfigValueSource(
                path=option.field_path,
                source="env var",
                env_var=env_name,
            )
        )

    return values, tuple(sources)


def _catalog_validation_error_path(location: tuple[str | int, ...]) -> str:
    field_path = tuple(item for item in location if isinstance(item, str))
    option = OPTION_CATALOG.option_for_field_path(field_path)
    if (
        option is not None
        and len(field_path) == 1
        and option.canonical_key.partition(".")[0] in _FLATTENED_MODEL_SECTIONS
    ):
        return option.canonical_key

    return validation_error_path(location)


def _path_is_present(values: dict[str, object], path: tuple[str, ...]) -> bool:
    current: object = values
    for part in path:
        if not is_json_object(current) or part not in current:
            return False
        current = current[part]
    return True


def _iter_dynamic_source_paths(
    values: dict[str, object],
    prefix: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], ...]:
    paths: list[tuple[str, ...]] = []
    for key, value in values.items():
        path = (*prefix, key)
        if path == ("hooks",):
            paths.append(path)
        elif is_json_object(value):
            paths.extend(_iter_dynamic_source_paths(value, path))
        else:
            paths.append(path)
    return tuple(paths)


def _record_layer_sources(
    sources: dict[tuple[str, ...], ConfigValueSource],
    layer: _NormalizedConfigLayer,
    *,
    source: Literal["file", "user-config"],
) -> None:
    for option in OPTION_CATALOG.options:
        if _path_is_present(layer.values, option.field_path):
            sources[option.field_path] = ConfigValueSource(
                path=option.field_path,
                source=source,
            )
    for path in _iter_dynamic_source_paths(layer.dynamic):
        sources[path] = ConfigValueSource(path=path, source=source)


def _load_config_with_sources(
    project_root: Path,
    *,
    authority: _ProjectAuthorityLike | None = None,
    user_config: Path | None = None,
    validate_hooks: bool,
) -> LoadedConfig:
    """Load validated config and provenance across all precedence layers.

    RuntimeOverrides fields (model, harness, effort, etc.) are NOT loaded
    from ENV here — they are read separately via RuntimeOverrides.from_env().
    """

    resolved_project_root = (
        authority.project_root.expanduser().resolve()
        if authority is not None
        else project_root.expanduser().resolve()
    )
    values: dict[str, object] = {}
    dynamic_values: dict[str, object] = {}
    sources: dict[tuple[str, ...], ConfigValueSource] = {}
    for layer in iter_config_layers(
        resolved_project_root,
        authority=authority,
        user_config=user_config,
    ):
        normalized = _normalize_toml_payload(
            payload=layer.payload,
            path=layer.path,
            validate_hooks=validate_hooks,
        )
        values = merge_nested_dict_values(
            values,
            normalized.values,
        )
        dynamic_values = merge_dynamic_sections(dynamic_values, normalized.dynamic)
        _record_layer_sources(
            sources,
            normalized,
            source="user-config" if layer.name == "user" else "file",
        )
    env_values, env_sources = _env_alias_overrides()
    values = merge_nested_dict_values(values, env_values)
    for entry in env_sources:
        sources[entry.path] = entry

    try:
        config = MeridianConfig.model_validate(values)
        dynamic = DynamicConfigSections.model_validate(dynamic_values)
    except ValidationError as error:
        raise config_validation_error(
            error, path_formatter=_catalog_validation_error_path
        ) from None
    return LoadedConfig(
        config=config,
        dynamic=dynamic,
        sources=tuple(sources.values()),
    )


def load_config_with_sources(
    project_root: Path,
    *,
    authority: _ProjectAuthorityLike | None = None,
    user_config: Path | None = None,
) -> LoadedConfig:
    """Load validated config and provenance across all precedence layers."""

    return _load_config_with_sources(
        project_root,
        authority=authority,
        user_config=user_config,
        validate_hooks=True,
    )


def load_config(
    project_root: Path,
    *,
    authority: _ProjectAuthorityLike | None = None,
    user_config: Path | None = None,
) -> MeridianConfig:
    """Load config with precedence: defaults < user < project < local < environment."""

    return load_config_with_sources(
        project_root,
        authority=authority,
        user_config=user_config,
    ).config


def load_config_without_hooks(
    project_root: Path,
    *,
    authority: _ProjectAuthorityLike | None = None,
    user_config: Path | None = None,
) -> MeridianConfig:
    """Load operational config without revalidating independently loaded hook rows."""

    return _load_config_with_sources(
        project_root,
        authority=authority,
        user_config=user_config,
        validate_hooks=False,
    ).config


def load_user_config(*, user_config: Path | None = None) -> MeridianConfig:
    """Load defaults, user config, and environment without cwd project layers."""

    values: dict[str, object] = {}
    resolved_user_config = resolve_user_config_path(user_config)
    if resolved_user_config is not None:
        normalized = _normalize_toml_payload(
            payload=read_config_payload(resolved_user_config),
            path=resolved_user_config,
        )
        values = normalized.values

    env_values, _ = _env_alias_overrides()
    values = merge_nested_dict_values(values, env_values)
    try:
        return MeridianConfig.model_validate(values)
    except ValidationError as error:
        raise config_validation_error(
            error, path_formatter=_catalog_validation_error_path
        ) from None


def _truthy_env_value(raw_value: str) -> bool:
    return raw_value.strip().lower() not in {"", "0", "false", "no", "off"}


def resolve_pi_harness_profile(
    *,
    base_profile: PiHarnessProfileConfig | None = None,
    project_root: Path | None = None,
) -> PiHarnessProfileConfig:
    """Resolve ``[harness.pi]`` with optional launch snapshot and env overrides."""

    if base_profile is not None:
        profile = base_profile
    elif project_root is not None:
        profile = load_config(project_root).harness.pi
    else:
        from meridian.lib.config.project_root import resolve_project_root_resolution

        profile = load_config(resolve_project_root_resolution().project_root).harness.pi

    raw_load_all = os.getenv("MERIDIAN_PI_LOAD_ALL_EXTENSIONS")
    if raw_load_all is not None:
        return profile.model_copy(
            update={"load_all_pi_extensions": _truthy_env_value(raw_load_all)}
        )

    raw_disable = os.getenv("MERIDIAN_PI_DISABLE_MANAGED_BASH")
    if raw_disable is not None and _truthy_env_value(raw_disable):
        return profile.model_copy(update={"disable_managed_bash": True})

    raw_managed = os.getenv("MERIDIAN_PI_MANAGED_BASH")
    if raw_managed is not None and raw_managed.strip() == "0":
        return profile.model_copy(update={"disable_managed_bash": True})

    return profile


def resolve_pi_harness_profile_for_launch(
    *,
    config_snapshot: dict[str, object] | None,
    project_root: Path,
) -> PiHarnessProfileConfig:
    """Resolve ``[harness.pi]`` from a launch config snapshot (not ambient CWD)."""

    base_profile: PiHarnessProfileConfig | None = None
    if config_snapshot:
        try:
            base_profile = MeridianConfig.model_validate(config_snapshot).harness.pi
        except Exception:
            base_profile = None
    return resolve_pi_harness_profile(
        base_profile=base_profile,
        project_root=project_root if base_profile is None else None,
    )


def resolve_allow_builtin_agents_for_launch(
    *,
    harness_id: str,
    config_snapshot: dict[str, object] | None,
    project_root: Path,
) -> bool:
    """Resolve ``[harness.<id>].allow_builtin_agents`` from a launch config snapshot."""

    def _from(config: MeridianConfig) -> bool:
        profile = getattr(config.harness, harness_id, None)
        return isinstance(profile, HarnessProfileConfig) and profile.allow_builtin_agents

    if config_snapshot:
        try:
            return _from(MeridianConfig.model_validate(config_snapshot))
        except Exception:
            pass
    return _from(load_config(project_root))


def resolve_opencode_version_for_launch(
    *,
    config_snapshot: dict[str, object] | None,
    project_root: Path,
) -> str:
    """Resolve ``[harness.opencode].version`` from a launch config snapshot."""

    if config_snapshot:
        try:
            return str(MeridianConfig.model_validate(config_snapshot).harness.opencode.version)
        except Exception:
            pass
    return str(load_config(project_root).harness.opencode.version)


def resolve_pi_disable_managed_bash() -> bool:
    """Resolve whether Meridian should skip Pi's managed bash extension."""

    return resolve_pi_harness_profile().disable_managed_bash
