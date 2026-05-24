"""Repository-level operational config loader."""

import logging
import os
import tomllib
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from meridian.lib.config.catalog import build_option_catalog, file_alias
from meridian.lib.config.project_config_state import resolve_project_config_state
from meridian.lib.config.project_paths import (
    ProjectConfigPaths,
    resolve_project_config_paths,
)
from meridian.lib.config.schema import (
    DynamicSectionDescriptor,
    config_field,
    parse_env_scalar,
    parse_toml_scalar,
)
from meridian.lib.core.overrides import (
    ApprovalValue,
    AutocompactPctValue,
    AutocompactValue,
    EffortValue,
)

logger = logging.getLogger(__name__)

_OUTPUT_VERBOSITY_PRESETS = frozenset({"quiet", "normal", "verbose", "debug"})
_PRIMARY_AUTOCOMPACT_PCT_MIN = 1
_PRIMARY_AUTOCOMPACT_PCT_MAX = 100
_PRIMARY_AUTOCOMPACT_TOKEN_MIN = 1000
_LOCAL_CONFIG_FILENAME = "meridian.local.toml"
_LEGACY_AGENTS_SECTION = "agents"
_HARNESS_TABLE_KEYS = frozenset({"claude", "codex", "opencode", "pi"})


class _SettingsLoadContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: Path
    project_config_paths: ProjectConfigPaths
    user_config: Path | None
    resolve_models: bool = True


class _ProjectAuthorityLike(Protocol):
    project_root: Path
    project_config_paths: ProjectConfigPaths


_SETTINGS_CONTEXT: ContextVar[_SettingsLoadContext | None] = ContextVar(
    "_SETTINGS_CONTEXT",
    default=None,
)


def _current_project_root() -> Path | None:
    context = _SETTINGS_CONTEXT.get()
    if context is None:
        return None
    return context.project_root


def _normalize_required_string(raw: str, *, source: str) -> str:
    normalized = raw.strip()
    if not normalized:
        raise ValueError(f"Invalid value for '{source}': expected non-empty string.")
    return normalized


def _normalize_optional_string(raw: str | None, *, source: str) -> str | None:
    if raw is None:
        return None
    normalized = raw.strip()
    if not normalized:
        raise ValueError(f"Invalid value for '{source}': expected non-empty string.")
    return normalized


def _normalize_model_identifier(model: str, *, project_root: Path | None) -> str:
    _ = project_root
    normalized = model.strip()
    return normalized


def _normalize_string_tuple(
    values: tuple[str, ...],
    *,
    source: str,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in values:
        compact = item.strip()
        if not compact:
            raise ValueError(f"Invalid value for '{source}': expected non-empty entries.")
        normalized.append(compact)
    return tuple(normalized)


def _parse_toml_list(*, raw_value: object, source: str) -> tuple[str, ...]:
    if not isinstance(raw_value, list):
        raise ValueError(
            f"Invalid value for '{source}': expected array[str], "
            f"got {type(raw_value).__name__} ({raw_value!r})."
        )

    parsed: list[str] = []
    for item in cast("list[object]", raw_value):
        if not isinstance(item, str):
            raise ValueError(
                f"Invalid value for '{source}': expected array[str], "
                f"got {type(item).__name__} ({item!r})."
            )
        normalized = item.strip()
        if not normalized:
            raise ValueError(f"Invalid value for '{source}': expected non-empty path entries.")
        parsed.append(normalized)
    return tuple(parsed)


def _merge_nested_dicts(base: dict[str, object], overrides: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_nested_dicts(
                cast("dict[str, object]", current),
                cast("dict[str, object]", value),
            )
            continue
        merged[key] = value
    return merged


def _assign_nested_value(target: dict[str, object], path: tuple[str, ...], value: object) -> None:
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


def _read_toml(path: Path) -> dict[str, object]:
    payload_obj = tomllib.loads(path.read_text(encoding="utf-8"))
    return cast("dict[str, object]", payload_obj)


def _resolve_project_toml(
    project_root: Path,
    project_config_paths: ProjectConfigPaths,
) -> Path | None:
    _ = project_root
    if project_config_paths.meridian_toml.is_file():
        return project_config_paths.meridian_toml
    return resolve_project_config_state(project_config_paths.project_root).path


def _resolve_local_toml(project_config_paths: ProjectConfigPaths) -> Path | None:
    local_config = project_config_paths.meridian_local_toml
    if not local_config.is_file():
        return None
    return local_config


def _normalize_output_table(raw_value: object, *, source: str) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    values: dict[str, object] = {}
    for key, value in cast("dict[str, object]", raw_value).items():
        if key == "show":
            values[key] = _parse_toml_list(raw_value=value, source=f"{source}.show")
            continue
        if key == "verbosity":
            if not isinstance(value, str):
                raise ValueError(
                    f"Invalid value for '{source}.verbosity': expected str, got "
                    f"{type(value).__name__} ({value!r})."
                )
            normalized = value.strip().lower()
            if not normalized:
                raise ValueError(
                    f"Invalid value for '{source}.verbosity': expected non-empty string."
                )
            if normalized not in _OUTPUT_VERBOSITY_PRESETS:
                raise ValueError(
                    f"Invalid value for '{source}.verbosity': expected one of "
                    f"{sorted(_OUTPUT_VERBOSITY_PRESETS)}, got {value!r}."
                )
            values[key] = normalized
            continue
        if key == "format":
            if not isinstance(value, str):
                raise ValueError(
                    f"Invalid value for '{source}.format': expected str, got "
                    f"{type(value).__name__} ({value!r})."
                )
            values[key] = _normalize_required_string(value, source=f"{source}.format")
            continue

        logger.warning("Ignoring unknown Meridian config key '%s.%s'.", source, key)

    return values


def _normalize_state_table(raw_value: object, *, source: str) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    values: dict[str, object] = {}
    for key, value in cast("dict[str, object]", raw_value).items():
        if key == "retention_days":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"Invalid value for '{source}.retention_days': expected int, got "
                    f"{type(value).__name__} ({value!r})."
                )
            if value < -1:
                raise ValueError(
                    f"Invalid value for '{source}.retention_days': expected int >= -1, got "
                    f"{value!r}."
                )
            values[key] = value
            continue

        logger.warning("Ignoring unknown Meridian config key '%s.%s'.", source, key)

    return values


def _normalize_primary_table(raw_value: object, *, source: str) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    values: dict[str, object] = {}
    for key, value in cast("dict[str, object]", raw_value).items():
        if key in {"model", "harness", "agent", "effort", "sandbox", "approval"}:
            if not isinstance(value, str):
                raise ValueError(
                    f"Invalid value for '{source}.{key}': expected str, got "
                    f"{type(value).__name__} ({value!r})."
                )
            values[key] = _normalize_required_string(value, source=f"{source}.{key}")
            continue

        if key == "autocompact":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"Invalid value for '{source}.autocompact': expected int, got "
                    f"{type(value).__name__} ({value!r})."
                )
            if value < _PRIMARY_AUTOCOMPACT_TOKEN_MIN:
                raise ValueError(
                    f"Invalid value for '{source}.autocompact': expected int >= "
                    f"{_PRIMARY_AUTOCOMPACT_TOKEN_MIN} (token count), got {value!r}."
                )
            values[key] = value
            continue

        if key == "autocompact_pct":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"Invalid value for '{source}.autocompact_pct': expected int, got "
                    f"{type(value).__name__} ({value!r})."
                )
            if not (_PRIMARY_AUTOCOMPACT_PCT_MIN <= value <= _PRIMARY_AUTOCOMPACT_PCT_MAX):
                raise ValueError(
                    f"Invalid value for '{source}.autocompact_pct': expected int between "
                    f"{_PRIMARY_AUTOCOMPACT_PCT_MIN} and "
                    f"{_PRIMARY_AUTOCOMPACT_PCT_MAX}, got {value!r}."
                )
            values[key] = value
            continue

        if key == "timeout":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(
                    f"Invalid value for '{source}.timeout': expected float, got "
                    f"{type(value).__name__} ({value!r})."
                )
            if float(value) <= 0:
                raise ValueError(
                    f"Invalid value for '{source}.timeout': expected float > 0, got {value!r}."
                )
            values[key] = float(value)
            continue

        logger.warning("Ignoring unknown Meridian config key '%s.%s'.", source, key)

    return values


def _normalize_harness_table(
    raw_value: object,
    *,
    source: str,
    project_root: Path,
) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    allowed = _HARNESS_TABLE_KEYS
    values: dict[str, object] = {}
    for key, value in cast("dict[str, object]", raw_value).items():
        if key not in allowed:
            logger.warning("Ignoring unknown Meridian config key '%s.%s'.", source, key)
            continue
        if isinstance(value, dict):
            harness_values: dict[str, object] = {}
            for harness_key, harness_value in cast("dict[str, object]", value).items():
                if harness_key == "model":
                    if not isinstance(harness_value, str):
                        raise ValueError(
                            f"Invalid value for '{source}.{key}.model': expected str, got "
                            f"{type(harness_value).__name__} ({harness_value!r})."
                        )
                    normalized_model = harness_value.strip()
                    if normalized_model:
                        normalized_model = _normalize_model_identifier(
                            normalized_model,
                            project_root=project_root,
                        )
                    harness_values["model"] = normalized_model
                    continue
                if harness_key == "wait_yield_seconds":
                    if isinstance(harness_value, bool) or not isinstance(
                        harness_value,
                        int | float,
                    ):
                        raise ValueError(
                            f"Invalid value for '{source}.{key}.wait_yield_seconds': "
                            f"expected float, got "
                            f"{type(harness_value).__name__} ({harness_value!r})."
                        )
                    harness_values["wait_yield_seconds"] = float(harness_value)
                    continue
                if key == "pi" and harness_key == "disable_managed_bash":
                    if not isinstance(harness_value, bool):
                        raise ValueError(
                            f"Invalid value for '{source}.{key}.disable_managed_bash': "
                            f"expected bool, got "
                            f"{type(harness_value).__name__} ({harness_value!r})."
                        )
                    harness_values["disable_managed_bash"] = harness_value
                    continue
                if key == "pi" and harness_key == "load_all_pi_extensions":
                    if not isinstance(harness_value, bool):
                        raise ValueError(
                            f"Invalid value for '{source}.{key}.load_all_pi_extensions': "
                            f"expected bool, got "
                            f"{type(harness_value).__name__} ({harness_value!r})."
                        )
                    harness_values["load_all_pi_extensions"] = harness_value
                    continue
                if key == "pi" and harness_key == "extra_extension_paths":
                    if not isinstance(harness_value, list):
                        raise ValueError(
                            f"Invalid value for '{source}.{key}.extra_extension_paths': "
                            f"expected array, got "
                            f"{type(harness_value).__name__} ({harness_value!r})."
                        )
                    harness_values["extra_extension_paths"] = [
                        str(item).strip()
                        for item in harness_value
                        if str(item).strip()
                    ]
                    continue
                if key == "pi" and harness_key in {"background_tasks", "spawn_watch"}:
                    if not isinstance(harness_value, dict):
                        raise ValueError(
                            f"Invalid value for '{source}.{key}.{harness_key}': "
                            f"expected table, got "
                            f"{type(harness_value).__name__} ({harness_value!r})."
                        )
                    nested: dict[str, object] = {}
                    enabled = harness_value.get("enabled")
                    if enabled is not None:
                        if not isinstance(enabled, bool):
                            raise ValueError(
                                f"Invalid value for '{source}.{key}.{harness_key}.enabled': "
                                f"expected bool, got "
                                f"{type(enabled).__name__} ({enabled!r})."
                            )
                        nested["enabled"] = enabled
                    harness_values[harness_key] = nested
                    continue
                logger.warning(
                    "Ignoring unknown Meridian config key '%s.%s.%s'.",
                    source,
                    key,
                    harness_key,
                )
            values[key] = harness_values
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"Invalid value for '{source}.{key}': expected str or table, got "
                f"{type(value).__name__} ({value!r})."
            )
        normalized = value.strip()
        if not normalized:
            values[key] = {"model": normalized}
            continue
        values[key] = {"model": _normalize_model_identifier(normalized, project_root=project_root)}

    return values


def _normalize_spawn_table(raw_value: object, *, source: str) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    values: dict[str, object] = {}
    for key, value in cast("dict[str, object]", raw_value).items():
        if key not in {"default_wait_yield_seconds", "min_wait_yield_seconds"}:
            logger.warning("Ignoring unknown Meridian config key '%s.%s'.", source, key)
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(
                f"Invalid value for '{source}.{key}': expected float, got "
                f"{type(value).__name__} ({value!r})."
            )
        values[key] = float(value)
    return values


def _legacy_agents_section_error(*, path: Path) -> ValueError:
    message = (
        f"Legacy section '[{_LEGACY_AGENTS_SECTION}]' is not supported in '{path}'. "
        "[agents] in meridian.toml is no longer supported. Agent overlays moved to mars.toml: "
        "define [agents.<name>] under mars.toml or mars.local.toml. See docs/configuration.md "
        "for the new schema."
    )
    return ValueError(message)


def _normalize_hooks_array(raw_value: object, *, source: str) -> tuple[dict[str, object], ...]:
    allowed_hook_keys = frozenset(
        {
            "name",
            "builtin",
            "command",
            "event",
            "events",
            "timeout_secs",
            "interval",
            "enabled",
            "priority",
            "failure_policy",
            "require_serial",
            "when",
            "exclude",
            "repo",
            "remote",
            "options",
        }
    )
    if not isinstance(raw_value, list):
        raise ValueError(
            f"Invalid value for '{source}': expected array[table], "
            f"got {type(raw_value).__name__} ({raw_value!r})."
        )

    rows: list[dict[str, object]] = []
    for index, item in enumerate(cast("list[object]", raw_value), start=1):
        row_source = f"{source}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(
                f"Invalid value for '{row_source}': expected table, "
                f"got {type(item).__name__} ({item!r})."
            )

        row: dict[str, object] = {}
        for key, value in cast("dict[str, object]", item).items():
            if key not in allowed_hook_keys:
                logger.warning("Ignoring unknown Meridian config key '%s.%s'.", row_source, key)
                continue

            field_source = f"{row_source}.{key}"
            if key in {
                "name",
                "event",
                "command",
                "builtin",
                "interval",
                "failure_policy",
                "repo",
                "remote",
            }:
                if not isinstance(value, str):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected str, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                row[key] = _normalize_required_string(value, source=field_source)
                continue

            if key in {"enabled", "require_serial"}:
                if not isinstance(value, bool):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected bool, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                row[key] = value
                continue

            if key in {"priority", "timeout_secs"}:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected int, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                row[key] = value
                continue

            if key == "exclude":
                row[key] = _parse_toml_list(raw_value=value, source=field_source)
                continue

            if key == "options":
                if not isinstance(value, dict):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected table, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                # Preserve plugin-specific options payload as-is; builtin registry validates it.
                row[key] = dict(cast("dict[str, object]", value))
                continue

            if key == "when":
                if not isinstance(value, dict):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected table, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                when: dict[str, object] = {}
                for when_key, when_value in cast("dict[str, object]", value).items():
                    when_source = f"{field_source}.{when_key}"
                    if when_key == "status":
                        when["status"] = _parse_toml_list(raw_value=when_value, source=when_source)
                        continue
                    if when_key == "agent":
                        if not isinstance(when_value, str):
                            raise ValueError(
                                f"Invalid value for '{when_source}': expected str, got "
                                f"{type(when_value).__name__} ({when_value!r})."
                            )
                        when["agent"] = _normalize_required_string(when_value, source=when_source)
                        continue
                    logger.warning(
                        "Ignoring unknown Meridian config key '%s.%s'.",
                        field_source,
                        when_key,
                    )
                row[key] = when
                continue

            logger.warning("Ignoring unknown Meridian config key '%s.%s'.", row_source, key)

        rows.append(row)

    return tuple(rows)


def normalize_hooks_array(raw_value: object, *, source: str) -> tuple[dict[str, object], ...]:
    """Normalize one hooks array with settings-style type checks."""

    return _normalize_hooks_array(raw_value, source=source)


def _normalize_work_table(raw_value: object, *, source: str) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    values: dict[str, object] = {}
    for key, value in cast("dict[str, object]", raw_value).items():
        if key == "artifacts":
            if not isinstance(value, dict):
                raise ValueError(
                    f"Invalid value for '{source}.artifacts': expected table, "
                    f"got {type(value).__name__} ({value!r})."
                )

            artifacts: dict[str, object] = {}
            for artifacts_key, artifacts_value in cast("dict[str, object]", value).items():
                artifacts_source = f"{source}.artifacts.{artifacts_key}"
                if artifacts_key == "sync":
                    if not isinstance(artifacts_value, str):
                        raise ValueError(
                            f"Invalid value for '{artifacts_source}': expected str, got "
                            f"{type(artifacts_value).__name__} ({artifacts_value!r})."
                        )
                    artifacts["sync"] = _normalize_required_string(
                        artifacts_value,
                        source=artifacts_source,
                    )
                    continue
                logger.warning(
                    "Ignoring unknown Meridian config key '%s.%s'.",
                    f"{source}.artifacts",
                    artifacts_key,
                )

            if artifacts:
                values["artifacts"] = artifacts
            continue

        if key == "default_worktree":
            if not isinstance(value, bool):
                raise ValueError(
                    f"Invalid value for '{source}.default_worktree': expected bool, "
                    f"got {type(value).__name__} ({value!r})."
                )
            values["default_worktree"] = value
            continue

        if key == "worktree_base":
            if not isinstance(value, str):
                raise ValueError(
                    f"Invalid value for '{source}.worktree_base': expected str, "
                    f"got {type(value).__name__} ({value!r})."
                )
            normalized = value.strip()
            if not normalized:
                raise ValueError(
                    f"Invalid value for '{source}.worktree_base': expected non-empty string."
                )
            values["worktree_base"] = normalized
            continue

        logger.warning("Ignoring unknown Meridian config key '%s.%s'.", source, key)

    return values


def normalize_work_table(raw_value: object, *, source: str) -> dict[str, object]:
    """Normalize one [work] table with settings-style type checks."""

    return _normalize_work_table(raw_value, source=source)


def _normalize_context_table(raw_value: object, *, source: str) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    values: dict[str, object] = {}
    for context_name, context_value in cast("dict[str, object]", raw_value).items():
        context_source = f"{source}.{context_name}"
        if not isinstance(context_value, dict):
            raise ValueError(f"Invalid value for '{context_source}': expected table.")

        context_fields: dict[str, object] = {}
        for key, value in cast("dict[str, object]", context_value).items():
            field_source = f"{context_source}.{key}"
            if key in {"source", "path", "archive", "remote"}:
                if not isinstance(value, str):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected str, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                context_fields[key] = _normalize_required_string(value, source=field_source)
                continue

            logger.warning("Ignoring unknown Meridian config key '%s.%s'.", context_source, key)

        values[context_name] = context_fields

    return values


def normalize_context_table(raw_value: object, *, source: str) -> dict[str, object]:
    """Normalize one [context] table with settings-style type checks."""

    return _normalize_context_table(raw_value, source=source)


def _normalize_workspace_section(
    raw_value: object,
    *,
    source: str,
    project_root: Path,
) -> None:
    _ = raw_value
    _ = source
    _ = project_root
    return None


DYNAMIC_SECTION_DESCRIPTORS: dict[str, DynamicSectionDescriptor] = {
    "hooks": DynamicSectionDescriptor(
        section_key="hooks",
        merge_kind="replace",
        scaffold_lines=(
            "# -- Hook examples -----------------------------------------------------------",
            "# Hooks are dynamic arrays of tables. Keep examples commented until needed.",
            "",
            "# [[hooks]]",
            '# name = "notify-on-failure"',
            '# event = "spawn"',
            '# command = "echo spawn-hook"',
            "# timeout_secs = 30",
            "",
        ),
    ),
    "work": DynamicSectionDescriptor(
        section_key="work",
        merge_kind="nested_dict",
        scaffold_lines=(
            "# -- Work behavior -----------------------------------------------------------",
            "# [work]",
            "# default_worktree = false",
            '# worktree_base = "../my-worktrees"',
            "",
            "# [work.artifacts]",
            '# sync = "project"',
            "",
        ),
    ),
    "context": DynamicSectionDescriptor(
        section_key="context",
        merge_kind="nested_dict",
        scaffold_lines=(
            "# -- Context source examples -------------------------------------------------",
            "# [context.work]",
            '# source = "git"',
            '# remote = "https://example.com/work.git"',
            '# path = ".meridian/work"',
            '# archive = ".meridian/archive/work"',
            "",
        ),
    ),
    "workspace": DynamicSectionDescriptor(
        section_key="workspace",
        merge_kind="external",
        scaffold_lines=(
            "# -- Workspace root examples -------------------------------------------------",
            "# Named workspace entries live in meridian.toml / meridian.local.toml.",
            "",
            "# [workspace.docs]",
            '# path = "./docs"',
            "",
        ),
    ),
}


def normalize_dynamic_sections(
    *,
    payload: dict[str, object],
    project_root: Path,
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for section_key, descriptor in DYNAMIC_SECTION_DESCRIPTORS.items():
        if section_key not in payload:
            continue
        raw_value = payload[section_key]
        if section_key == "hooks":
            value = _normalize_hooks_array(raw_value, source=section_key)
        elif section_key == "work":
            value = _normalize_work_table(raw_value, source=section_key)
        elif section_key == "context":
            value = _normalize_context_table(raw_value, source=section_key)
        elif section_key == "workspace":
            value = _normalize_workspace_section(
                raw_value,
                source=section_key,
                project_root=project_root,
            )
        else:
            continue
        if descriptor.merge_kind != "external" and value is not None:
            normalized[section_key] = value
    return normalized


def merge_dynamic_sections(
    base: dict[str, object],
    overrides: dict[str, object],
) -> dict[str, object]:
    merged = dict(base)
    for section_key, value in overrides.items():
        descriptor = DYNAMIC_SECTION_DESCRIPTORS.get(section_key)
        if descriptor is None or descriptor.merge_kind == "external":
            continue
        current = merged.get(section_key)
        if (
            descriptor.merge_kind == "nested_dict"
            and isinstance(current, dict)
            and isinstance(value, dict)
        ):
            merged[section_key] = _merge_nested_dicts(
                cast("dict[str, object]", current),
                cast("dict[str, object]", value),
            )
            continue
        merged[section_key] = value
    return merged


def _normalize_toml_payload(
    *,
    payload: dict[str, object],
    path: Path,
    project_root: Path,
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, raw_value in payload.items():
        if key == "output":
            normalized["output"] = _merge_nested_dicts(
                cast("dict[str, object]", normalized.get("output", {})),
                _normalize_output_table(raw_value, source="output"),
            )
            continue
        if key == "state":
            normalized["state"] = _merge_nested_dicts(
                cast("dict[str, object]", normalized.get("state", {})),
                _normalize_state_table(raw_value, source="state"),
            )
            continue
        if key == "primary":
            normalized["primary"] = _merge_nested_dicts(
                cast("dict[str, object]", normalized.get("primary", {})),
                _normalize_primary_table(raw_value, source="primary"),
            )
            continue
        if key == "agents":
            raise _legacy_agents_section_error(path=path)
        if key == "harness":
            normalized["harness"] = _merge_nested_dicts(
                cast("dict[str, object]", normalized.get("harness", {})),
                _normalize_harness_table(raw_value, source="harness", project_root=project_root),
            )
            continue
        if key == "spawn":
            normalized = _merge_nested_dicts(
                normalized,
                _normalize_spawn_table(raw_value, source="spawn"),
            )
            continue
        if key in DYNAMIC_SECTION_DESCRIPTORS:
            normalized = merge_dynamic_sections(
                normalized,
                normalize_dynamic_sections(
                    payload={key: raw_value},
                    project_root=project_root,
                ),
            )
            continue

        if key in {"defaults", "timeouts"}:
            if not isinstance(raw_value, dict):
                raise ValueError(f"Invalid value for '{key}' in '{path}': expected table.")
            for section_key, section_value in cast("dict[str, object]", raw_value).items():
                option = OPTION_CATALOG.find_file_alias(table_path=(key,), key=section_key)
                if option is None:
                    logger.warning(
                        "Ignoring unknown Meridian config key '%s.%s'.",
                        key,
                        section_key,
                    )
                    continue
                coerced = parse_toml_scalar(
                    value_kind=option.value_kind,
                    raw_value=section_value,
                    source=f"{key}.{section_key}",
                )
                _assign_nested_value(normalized, option.field_path, coerced)
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

    return normalized


def _env_alias_overrides(project_root: Path) -> dict[str, object]:
    values: dict[str, object] = {}
    for env_name, option in OPTION_CATALOG.env_options():
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue

        parsed = parse_env_scalar(
            value_kind=option.value_kind,
            raw_value=raw_value,
            env_name=env_name,
        )

        if option.field_path in {
            ("harness", "claude", "model"),
            ("harness", "codex", "model"),
            ("harness", "opencode", "model"),
            ("primary", "model"),
        }:
            parsed = _normalize_model_identifier(cast("str", parsed), project_root=project_root)

        _assign_nested_value(values, option.field_path, parsed)

    hidden_env_specs: tuple[tuple[str, tuple[str, ...], Literal["float", "str"]], ...] = (
        (
            "MERIDIAN_HARNESS_WAIT_YIELD_SECONDS_CLAUDE",
            ("harness", "claude", "wait_yield_seconds"),
            "float",
        ),
        (
            "MERIDIAN_HARNESS_WAIT_YIELD_SECONDS_CODEX",
            ("harness", "codex", "wait_yield_seconds"),
            "float",
        ),
        (
            "MERIDIAN_HARNESS_WAIT_YIELD_SECONDS_OPENCODE",
            ("harness", "opencode", "wait_yield_seconds"),
            "float",
        ),
    )
    for env_name, field_path, value_kind in hidden_env_specs:
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue
        parsed = parse_env_scalar(
            value_kind=value_kind,
            raw_value=raw_value,
            env_name=env_name,
        )
        _assign_nested_value(values, field_path, parsed)

    return values


class OutputConfig(BaseModel):
    """Terminal output filtering configuration for run streaming."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    show: Annotated[
        tuple[str, ...],
        config_field(
            "output.show",
            value_kind="str_list",
            file_aliases=(file_alias("output", "show"),),
        ),
    ] = ("lifecycle", "sub-run", "error", "system")
    verbosity: Annotated[
        str | None,
        config_field(
            "output.verbosity",
            value_kind="verbosity",
            file_aliases=(file_alias("output", "verbosity"),),
        ),
    ] = None
    format: Annotated[
        str,
        config_field(
            "output.format",
            value_kind="str",
            file_aliases=(file_alias("output", "format"),),
            env_vars=("MERIDIAN_FORMAT",),
            command_visible=False,
        ),
    ] = "text"

    @field_validator("show")
    @classmethod
    def _validate_show(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_string_tuple(value, source="output.show")

    @field_validator("verbosity")
    @classmethod
    def _validate_verbosity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _normalize_required_string(value, source="output.verbosity").lower()
        if normalized not in _OUTPUT_VERBOSITY_PRESETS:
            raise ValueError(
                "Invalid value for 'output.verbosity': expected one of "
                f"{sorted(_OUTPUT_VERBOSITY_PRESETS)}, got {value!r}."
            )
        return normalized

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str) -> str:
        return _normalize_required_string(value, source="output.format")


class StateConfig(BaseModel):
    """State retention settings for project and spawn artifacts."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    retention_days: Annotated[
        int,
        config_field(
            "state.retention_days",
            value_kind="int",
            file_aliases=(
                file_alias("state", "retention_days"),
                file_alias(None, "retention_days"),
            ),
            env_vars=("MERIDIAN_STATE_RETENTION_DAYS",),
        ),
    ] = 30

    @field_validator("retention_days")
    @classmethod
    def _validate_retention_days(cls, value: int) -> int:
        if isinstance(value, bool) or value < -1:
            raise ValueError(
                f"Invalid value for 'state.retention_days': expected int >= -1, got {value!r}."
            )
        return value


class WorkConfig(BaseModel):
    """Work-item behavior settings."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    default_worktree: bool = False
    worktree_base: str | None = None


class PrimaryConfig(BaseModel):
    """Primary-specific harness settings."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    autocompact: Annotated[
        AutocompactValue,
        config_field(
            "primary.autocompact",
            value_kind="int",
            file_aliases=(file_alias("primary", "autocompact"),),
        ),
    ] = None
    autocompact_pct: Annotated[
        AutocompactPctValue,
        config_field(
            "primary.autocompact_pct",
            value_kind="int",
            file_aliases=(file_alias("primary", "autocompact_pct"),),
        ),
    ] = None
    agent: Annotated[
        str | None,
        config_field(
            "primary.agent",
            value_kind="str",
            file_aliases=(file_alias("primary", "agent"),),
            env_vars=("MERIDIAN_AGENT",),
        ),
    ] = None
    effort: Annotated[
        EffortValue,
        config_field(
            "primary.effort",
            value_kind="str",
            file_aliases=(file_alias("primary", "effort"),),
            command_visible=False,
        ),
    ] = None
    sandbox: Annotated[
        str | None,
        config_field(
            "primary.sandbox",
            value_kind="str",
            file_aliases=(file_alias("primary", "sandbox"),),
            command_visible=False,
        ),
    ] = None
    approval: Annotated[
        ApprovalValue,
        config_field(
            "primary.approval",
            value_kind="str",
            file_aliases=(file_alias("primary", "approval"),),
            command_visible=False,
        ),
    ] = None
    timeout: float | None = None

    @field_validator("agent")
    @classmethod
    def _validate_optional_string_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_string(value, source="primary")

    @field_validator("timeout")
    @classmethod
    def _validate_timeout(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or value <= 0:
            raise ValueError(
                f"Invalid value for 'primary.timeout': expected float > 0, got {value!r}."
            )
        return value


class HarnessProfileConfig(BaseModel):
    """Per-harness model and wait-yield settings."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model: str = ""
    wait_yield_seconds: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _string_as_model(cls, values: Any) -> Any:
        if isinstance(values, str):
            return {"model": values}
        return values

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return normalized
        return _normalize_model_identifier(normalized, project_root=_current_project_root())

    @field_validator("wait_yield_seconds")
    @classmethod
    def _validate_wait_yield_seconds(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(
                f"Invalid value for 'harness.wait_yield_seconds': expected float, got {value!r}."
            )
        return float(value)


class ClaudeHarnessProfileConfig(HarnessProfileConfig):
    model: Annotated[
        str,
        config_field(
            "harness.claude",
            value_kind="str",
            file_aliases=(
                file_alias("harness", "claude"),
                file_alias(("harness", "claude"), "model"),
            ),
            env_vars=("MERIDIAN_HARNESS_MODEL_CLAUDE",),
        ),
    ] = ""


class CodexHarnessProfileConfig(HarnessProfileConfig):
    model: Annotated[
        str,
        config_field(
            "harness.codex",
            value_kind="str",
            file_aliases=(
                file_alias("harness", "codex"),
                file_alias(("harness", "codex"), "model"),
            ),
            env_vars=("MERIDIAN_HARNESS_MODEL_CODEX",),
        ),
    ] = ""


class OpenCodeHarnessProfileConfig(HarnessProfileConfig):
    model: Annotated[
        str,
        config_field(
            "harness.opencode",
            value_kind="str",
            file_aliases=(
                file_alias("harness", "opencode"),
                file_alias(("harness", "opencode"), "model"),
            ),
            env_vars=("MERIDIAN_HARNESS_MODEL_OPENCODE",),
        ),
    ] = "opencode-go/kimi-k2.6"


class PiBundleToggleConfig(BaseModel):
    """Per-bundle enable switch under ``[harness.pi]``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    enabled: bool = True


class PiHarnessProfileConfig(HarnessProfileConfig):
    load_all_pi_extensions: bool = False
    extra_extension_paths: tuple[str, ...] = ()
    background_tasks: PiBundleToggleConfig = Field(default_factory=PiBundleToggleConfig)
    spawn_watch: PiBundleToggleConfig = Field(default_factory=PiBundleToggleConfig)
    disable_managed_bash: bool = False

    def background_tasks_enabled(self) -> bool:
        if self.disable_managed_bash:
            return False
        return self.background_tasks.enabled


class HarnessConfig(BaseModel):
    """Default model and wait-yield configuration for each harness adapter."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    claude: ClaudeHarnessProfileConfig = Field(
        default_factory=lambda: ClaudeHarnessProfileConfig(wait_yield_seconds=3000.0)
    )
    codex: CodexHarnessProfileConfig = Field(
        default_factory=lambda: CodexHarnessProfileConfig(wait_yield_seconds=3000.0)
    )
    opencode: OpenCodeHarnessProfileConfig = Field(default_factory=OpenCodeHarnessProfileConfig)
    pi: PiHarnessProfileConfig = Field(default_factory=PiHarnessProfileConfig)


class MeridianConfig(BaseSettings):
    """Resolved operational configuration for meridian."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="ignore",
        env_prefix="MERIDIAN_",
        env_nested_delimiter="__",
    )

    max_depth: Annotated[
        int,
        config_field(
            "defaults.max_depth",
            value_kind="int",
            file_aliases=(file_alias("defaults", "max_depth"), file_alias(None, "max_depth")),
            env_vars=("MERIDIAN_MAX_DEPTH",),
        ),
    ] = 3
    max_retries: Annotated[
        int,
        config_field(
            "defaults.max_retries",
            value_kind="int",
            file_aliases=(file_alias("defaults", "max_retries"), file_alias(None, "max_retries")),
            env_vars=("MERIDIAN_MAX_RETRIES",),
        ),
    ] = 3
    retry_backoff_seconds: Annotated[
        float,
        config_field(
            "defaults.retry_backoff_seconds",
            value_kind="float",
            file_aliases=(
                file_alias("defaults", "retry_backoff_seconds"),
                file_alias(None, "retry_backoff_seconds"),
            ),
            env_vars=("MERIDIAN_RETRY_BACKOFF_SECONDS",),
        ),
    ] = 0.25
    kill_grace_minutes: Annotated[
        float,
        config_field(
            "timeouts.kill_grace_minutes",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "kill_grace_minutes"),
                file_alias(None, "kill_grace_minutes"),
            ),
            env_vars=("MERIDIAN_KILL_GRACE_MINUTES",),
        ),
    ] = 2.0 / 60.0
    guardrail_timeout_minutes: Annotated[
        float,
        config_field(
            "timeouts.guardrail_minutes",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "guardrail_minutes"),
                file_alias("timeouts", "guardrail_timeout_minutes"),
                file_alias(None, "guardrail_timeout_minutes"),
            ),
            env_vars=("MERIDIAN_GUARDRAIL_TIMEOUT_MINUTES",),
        ),
    ] = 0.5
    wait_timeout_minutes: Annotated[
        float,
        config_field(
            "timeouts.wait_minutes",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "wait_minutes"),
                file_alias("timeouts", "wait_timeout_minutes"),
                file_alias(None, "wait_timeout_minutes"),
            ),
            env_vars=("MERIDIAN_WAIT_TIMEOUT_MINUTES",),
        ),
    ] = 30.0
    pi_child_wave_timeout_seconds: Annotated[
        float | None,
        config_field(
            "timeouts.pi_child_wave_timeout_seconds",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "pi_child_wave_timeout_seconds"),
                file_alias(None, "pi_child_wave_timeout_seconds"),
            ),
            env_vars=("MERIDIAN_PI_CHILD_WAVE_TIMEOUT_SECONDS",),
        ),
    ] = None
    pi_task_ping_interval_seconds: Annotated[
        float | None,
        config_field(
            "timeouts.pi_task_ping_interval_seconds",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "pi_task_ping_interval_seconds"),
                file_alias(None, "pi_task_ping_interval_seconds"),
            ),
            env_vars=("MERIDIAN_PI_TASK_PING_INTERVAL_SECONDS",),
        ),
    ] = None
    default_wait_yield_seconds: Annotated[
        float,
        config_field(
            "spawn.default_wait_yield_seconds",
            value_kind="float",
            file_aliases=(file_alias("spawn", "default_wait_yield_seconds"),),
            env_vars=("MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS",),
        ),
    ] = 3000.0
    min_wait_yield_seconds: Annotated[
        float,
        config_field(
            "spawn.min_wait_yield_seconds",
            value_kind="float",
            file_aliases=(file_alias("spawn", "min_wait_yield_seconds"),),
            env_vars=("MERIDIAN_MIN_WAIT_YIELD_SECONDS",),
        ),
    ] = 30.0
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    primary: PrimaryConfig = Field(default_factory=PrimaryConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    work: WorkConfig = Field(default_factory=WorkConfig)

    @field_validator("default_wait_yield_seconds", "min_wait_yield_seconds")
    @classmethod
    def _validate_wait_yield_settings(cls, value: float) -> float:
        if isinstance(value, bool) or value <= 0:
            raise ValueError(f"Invalid wait-yield setting: expected float > 0, got {value!r}.")
        return float(value)

    def default_model_for_harness(self, harness_id: str) -> str | None:
        """Return configured default model for one harness ID."""

        profile = self._harness_profile_for_id(harness_id)
        return None if profile is None else profile.model

    def wait_yield_seconds_for_harness(self, harness_id: str | None) -> float:
        """Return clamped wait-yield seconds for a harness or the unknown default."""

        configured = self._harness_profile_for_id(harness_id)
        raw_value = (
            configured.wait_yield_seconds
            if configured is not None and configured.wait_yield_seconds is not None
            else self.default_wait_yield_seconds
        )
        return max(float(raw_value), float(self.min_wait_yield_seconds))

    def _harness_profile_for_id(self, harness_id: str | None) -> HarnessProfileConfig | None:
        normalized = (harness_id or "").strip().lower()
        if normalized not in _HARNESS_TABLE_KEYS:
            return None
        profile = getattr(self.harness, normalized, None)
        if not isinstance(profile, HarnessProfileConfig):
            return None
        return profile

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        _ = settings_cls
        _ = dotenv_settings
        _ = file_secret_settings

        def project_toml_source() -> dict[str, object]:
            context = _SETTINGS_CONTEXT.get()
            if context is None:
                return {}
            project_config = _resolve_project_toml(
                context.project_root,
                context.project_config_paths,
            )
            if project_config is None:
                return {}
            payload = _read_toml(project_config)
            return _normalize_toml_payload(
                payload=payload,
                path=project_config,
                project_root=context.project_root,
            )

        def local_toml_source() -> dict[str, object]:
            context = _SETTINGS_CONTEXT.get()
            if context is None:
                return {}
            local_config = _resolve_local_toml(context.project_config_paths)
            if local_config is None:
                return {}
            payload = _read_toml(local_config)
            return _normalize_toml_payload(
                payload=payload,
                path=local_config,
                project_root=context.project_root,
            )

        def user_toml_source() -> dict[str, object]:
            context = _SETTINGS_CONTEXT.get()
            if context is None or context.user_config is None:
                return {}
            payload = _read_toml(context.user_config)
            return _normalize_toml_payload(
                payload=payload,
                path=context.user_config,
                project_root=context.project_root,
            )

        def layered_env_source() -> dict[str, object]:
            context = _SETTINGS_CONTEXT.get()
            if context is None:
                return {}
            _ = env_settings
            return _env_alias_overrides(context.project_root)

        return (
            init_settings,
            cast("PydanticBaseSettingsSource", layered_env_source),
            cast("PydanticBaseSettingsSource", local_toml_source),
            cast("PydanticBaseSettingsSource", project_toml_source),
            cast("PydanticBaseSettingsSource", user_toml_source),
        )


OPTION_CATALOG = build_option_catalog(MeridianConfig)


def load_config(
    project_root: Path,
    *,
    authority: _ProjectAuthorityLike | None = None,
    user_config: Path | None = None,
    resolve_models: bool = True,
) -> MeridianConfig:
    """Load config with precedence: defaults < user < project < local < environment.

    RuntimeOverrides fields (model, harness, effort, etc.) are NOT loaded
    from ENV here — they are read separately via RuntimeOverrides.from_env().
    """

    from meridian.lib.config.project_root import resolve_user_config_path

    if authority is not None:
        resolved_project_root = authority.project_root.expanduser().resolve()
        project_config_paths = authority.project_config_paths
    else:
        resolved_project_root = project_root.expanduser().resolve()
        project_config_paths = resolve_project_config_paths(resolved_project_root)
    resolved_user_config = resolve_user_config_path(user_config)

    token = _SETTINGS_CONTEXT.set(
        _SettingsLoadContext(
            project_root=resolved_project_root,
            project_config_paths=project_config_paths,
            user_config=resolved_user_config,
            resolve_models=resolve_models,
        )
    )
    try:
        return MeridianConfig()
    finally:
        _SETTINGS_CONTEXT.reset(token)


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
        return profile.model_copy(update={"load_all_pi_extensions": _truthy_env_value(raw_load_all)})

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


def resolve_pi_disable_managed_bash() -> bool:
    """Resolve whether Meridian should skip Pi's managed bash extension."""

    return resolve_pi_harness_profile().disable_managed_bash
