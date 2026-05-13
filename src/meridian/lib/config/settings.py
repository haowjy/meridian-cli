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
    KNOWN_APPROVAL_VALUES,
    KNOWN_EFFORT_VALUES,
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

    allowed = frozenset({"claude", "codex", "opencode"})
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


def _normalize_agent_autocompact(raw_value: object, *, source: str) -> int:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise ValueError(
            f"Invalid value for '{source}': expected int, got "
            f"{type(raw_value).__name__} ({raw_value!r})."
        )
    if raw_value < _PRIMARY_AUTOCOMPACT_TOKEN_MIN:
        raise ValueError(
            f"Invalid value for '{source}': expected int >= "
            f"{_PRIMARY_AUTOCOMPACT_TOKEN_MIN} (token count), got {raw_value!r}."
        )
    return raw_value


def _normalize_agent_autocompact_pct(raw_value: object, *, source: str) -> int:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise ValueError(
            f"Invalid value for '{source}': expected int, got "
            f"{type(raw_value).__name__} ({raw_value!r})."
        )
    if not (_PRIMARY_AUTOCOMPACT_PCT_MIN <= raw_value <= _PRIMARY_AUTOCOMPACT_PCT_MAX):
        raise ValueError(
            f"Invalid value for '{source}': expected int between "
            f"{_PRIMARY_AUTOCOMPACT_PCT_MIN} and {_PRIMARY_AUTOCOMPACT_PCT_MAX}, "
            f"got {raw_value!r}."
        )
    return raw_value


def _normalize_agent_policy_overrides(
    raw_value: object,
    *,
    source: str,
) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    rejected_list_keys = frozenset({"skills", "tools", "mcp-tools"})
    allowed_scalar_keys = frozenset(
        {"harness", "effort", "approval", "sandbox", "autocompact", "autocompact_pct"}
    )
    overrides: dict[str, object] = {}
    for key, value in cast("dict[str, object]", raw_value).items():
        field_source = f"{source}.{key}"
        if key in rejected_list_keys:
            logger.warning(
                "Ignoring unsupported Meridian config key '%s'; list overrides are not "
                "supported in agent model-policies.",
                field_source,
            )
            continue
        if key not in allowed_scalar_keys:
            logger.warning("Ignoring unknown Meridian config key '%s'.", field_source)
            continue
        if key == "autocompact":
            overrides[key] = _normalize_agent_autocompact(value, source=field_source)
            continue
        if key == "autocompact_pct":
            overrides[key] = _normalize_agent_autocompact_pct(value, source=field_source)
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"Invalid value for '{field_source}': expected str, got "
                f"{type(value).__name__} ({value!r})."
            )
        normalized = _normalize_required_string(value, source=field_source)
        if key == "effort" and normalized not in KNOWN_EFFORT_VALUES:
            raise ValueError(
                f"Invalid value for '{field_source}': expected one of "
                f"{sorted(KNOWN_EFFORT_VALUES)}, got {value!r}."
            )
        if key == "approval" and normalized not in KNOWN_APPROVAL_VALUES:
            raise ValueError(
                f"Invalid value for '{field_source}': expected one of "
                f"{sorted(KNOWN_APPROVAL_VALUES)}, got {value!r}."
            )
        overrides[key] = normalized
    return overrides


def _normalize_agent_model_policies(
    raw_value: object,
    *,
    source: str,
) -> list[dict[str, object]]:
    if not isinstance(raw_value, list):
        raise ValueError(
            f"Invalid value for '{source}': expected array[table], "
            f"got {type(raw_value).__name__} ({raw_value!r})."
        )

    allowed_match_keys = frozenset({"model", "alias", "model-glob"})
    policies: list[dict[str, object]] = []
    for index, item in enumerate(cast("list[object]", raw_value), start=1):
        policy_source = f"{source}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(
                f"Invalid value for '{policy_source}': expected table, "
                f"got {type(item).__name__} ({item!r})."
            )
        policy = cast("dict[str, object]", item)
        raw_match = policy.get("match")
        if not isinstance(raw_match, dict):
            raise ValueError(f"Invalid value for '{policy_source}.match': expected table.")
        match = cast("dict[str, object]", raw_match)
        normalized_match = {str(key).strip(): value for key, value in match.items()}
        if len(normalized_match) != 1:
            raise ValueError(
                f"Invalid value for '{policy_source}.match': expected exactly one of "
                "model, alias, or model-glob."
            )
        match_type = next(iter(normalized_match))
        if match_type not in allowed_match_keys:
            raise ValueError(
                f"Invalid value for '{policy_source}.match': expected one of "
                f"{sorted(allowed_match_keys)}, got {match_type!r}."
            )
        raw_match_value = normalized_match[match_type]
        if not isinstance(raw_match_value, str):
            raise ValueError(
                f"Invalid value for '{policy_source}.match.{match_type}': expected str, got "
                f"{type(raw_match_value).__name__} ({raw_match_value!r})."
            )
        match_value = _normalize_required_string(
            raw_match_value,
            source=f"{policy_source}.match.{match_type}",
        )
        if "override" not in policy:
            raise ValueError(f"Invalid value for '{policy_source}.override': expected table.")
        overrides = _normalize_agent_policy_overrides(
            policy["override"],
            source=f"{policy_source}.override",
        )
        if not overrides:
            raise ValueError(
                f"Invalid value for '{policy_source}.override': expected at least one "
                "override field."
            )
        no_fallback = policy.get("no-fallback", False)
        if not isinstance(no_fallback, bool):
            raise ValueError(
                f"Invalid value for '{policy_source}.no-fallback': expected bool."
            )
        policies.append(
            {
                "match_type": match_type,
                "match_value": match_value,
                "overrides": overrides,
                "no_fallback": no_fallback,
            }
        )
    return policies


def _normalize_agents_table(raw_value: object, *, source: str) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    values: dict[str, object] = {}
    scalar_keys = frozenset({"model", "harness", "effort", "approval", "sandbox"})
    for agent_name, agent_value in cast("dict[str, object]", raw_value).items():
        normalized_name = str(agent_name).strip()
        agent_source = f"{source}.{normalized_name or agent_name}"
        if not normalized_name:
            raise ValueError(f"Invalid value for '{source}': expected non-empty agent name.")
        if not isinstance(agent_value, dict):
            raise ValueError(f"Invalid value for '{agent_source}': expected table.")
        logger.debug("Loading Meridian overlay config for agent '%s'.", normalized_name)
        overlay: dict[str, object] = {}
        for key, value in cast("dict[str, object]", agent_value).items():
            field_source = f"{agent_source}.{key}"
            if key in scalar_keys:
                if not isinstance(value, str):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected str, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                normalized = _normalize_required_string(value, source=field_source)
                if key == "effort" and normalized not in KNOWN_EFFORT_VALUES:
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected one of "
                        f"{sorted(KNOWN_EFFORT_VALUES)}, got {value!r}."
                    )
                if key == "approval" and normalized not in KNOWN_APPROVAL_VALUES:
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected one of "
                        f"{sorted(KNOWN_APPROVAL_VALUES)}, got {value!r}."
                    )
                overlay[key] = normalized
                continue
            if key == "autocompact":
                overlay[key] = _normalize_agent_autocompact(value, source=field_source)
                continue
            if key == "autocompact_pct":
                overlay[key] = _normalize_agent_autocompact_pct(value, source=field_source)
                continue
            if key == "model-policies":
                overlay["model_policies"] = _normalize_agent_model_policies(
                    value,
                    source=field_source,
                )
                continue
            if key == "timeout":
                logger.warning(
                    "Ignoring unsupported Meridian config key '%s'; agent overlays do not "
                    "support timeout.",
                    field_source,
                )
                continue
            logger.warning("Ignoring unknown Meridian config key '%s'.", field_source)
        values[normalized_name] = overlay
    return values


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
    "agents": DynamicSectionDescriptor(
        section_key="agents",
        merge_kind="nested_dict",
        scaffold_lines=(
            "# -- Agent runtime overrides ------------------------------------------------",
            "# Override default model/policy for specific agent profiles without editing",
            "# generated .mars/agents/ sources.",
            "# See docs/configuration.md for full semantics.",
            "",
            "# [agents.tech-lead]",
            '# model = "gpt55"',
            '# effort = "medium"',
            '# approval = "auto"',
            "",
            "# [[agents.tech-lead.model-policies]]",
            '# match = { model-glob = "gpt*" }',
            '# override = { effort = "medium", autocompact = 200000 }',
            "# # Or use percentage-based threshold (1-100):",
            '# # override = { effort = "medium", autocompact_pct = 80 }',
            "",
        ),
    ),
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
        if section_key == "agents":
            value = _normalize_agents_table(raw_value, source=section_key)
        elif section_key == "hooks":
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
            normalized = merge_dynamic_sections(
                normalized,
                normalize_dynamic_sections(
                    payload={key: raw_value},
                    project_root=project_root,
                ),
            )
            continue
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
                if option.field_path in {("default_model",)}:
                    coerced = _normalize_model_identifier(
                        cast("str", coerced),
                        project_root=project_root,
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
        if option.field_path in {("default_model",)}:
            coerced = _normalize_model_identifier(cast("str", coerced), project_root=project_root)
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
            ("default_model",),
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
    model: Annotated[
        str | None,
        config_field(
            "primary.model",
            value_kind="str",
            file_aliases=(file_alias("primary", "model"),),
            env_vars=("MERIDIAN_MODEL",),
        ),
    ] = None
    harness: Annotated[
        str | None,
        config_field(
            "primary.harness",
            value_kind="str",
            file_aliases=(file_alias("primary", "harness"),),
            env_vars=("MERIDIAN_HARNESS",),
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

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        normalized = _normalize_optional_string(value, source="primary.model")
        if normalized is None:
            return None
        return _normalize_model_identifier(normalized, project_root=_current_project_root())

    @field_validator("harness", "agent")
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


class AgentOverlayModelPolicy(BaseModel):
    """One conditional model-policy rule in an agent overlay."""

    model_config = ConfigDict(frozen=True)

    match_type: str
    match_value: str
    overrides: dict[str, object]
    no_fallback: bool = False

    @field_validator("match_type")
    @classmethod
    def _validate_match_type(cls, value: str) -> str:
        normalized = _normalize_required_string(value, source="agents.model-policies.match")
        if normalized not in {"model", "alias", "model-glob"}:
            raise ValueError(
                "Invalid value for 'agents.model-policies.match': expected one of "
                "['alias', 'model', 'model-glob'], "
                f"got {value!r}."
            )
        return normalized

    @field_validator("match_value")
    @classmethod
    def _validate_match_value(cls, value: str) -> str:
        return _normalize_required_string(value, source="agents.model-policies.match")

    @field_validator("overrides")
    @classmethod
    def _validate_overrides(cls, value: dict[str, object]) -> dict[str, object]:
        allowed = frozenset(
            {"harness", "effort", "approval", "sandbox", "autocompact", "autocompact_pct"}
        )
        normalized: dict[str, object] = {}
        for key, raw_value in value.items():
            if key not in allowed:
                raise ValueError(
                    "Invalid value for 'agents.model-policies.override': expected one of "
                    f"{sorted(allowed)}, got {key!r}."
                )
            source = f"agents.model-policies.override.{key}"
            if key == "autocompact":
                normalized[key] = _normalize_agent_autocompact(raw_value, source=source)
                continue
            if key == "autocompact_pct":
                normalized[key] = _normalize_agent_autocompact_pct(raw_value, source=source)
                continue
            if not isinstance(raw_value, str):
                raise ValueError(
                    f"Invalid value for '{source}': expected str, got "
                    f"{type(raw_value).__name__} ({raw_value!r})."
                )
            text = _normalize_required_string(raw_value, source=source)
            if key == "effort" and text not in KNOWN_EFFORT_VALUES:
                raise ValueError(
                    f"Invalid value for '{source}': expected one of "
                    f"{sorted(KNOWN_EFFORT_VALUES)}, got {raw_value!r}."
                )
            if key == "approval" and text not in KNOWN_APPROVAL_VALUES:
                raise ValueError(
                    f"Invalid value for '{source}': expected one of "
                    f"{sorted(KNOWN_APPROVAL_VALUES)}, got {raw_value!r}."
                )
            normalized[key] = text
        return normalized


class AgentOverlayConfig(BaseModel):
    """Per-agent runtime policy overlay from project config."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model: str | None = None
    harness: str | None = None
    effort: EffortValue = None
    approval: ApprovalValue = None
    sandbox: str | None = None
    autocompact: AutocompactValue = None
    autocompact_pct: AutocompactPctValue = None
    # Three-state: None = inherit, () = prepend no-op, non-empty = prepend before profile rules
    model_policies: tuple[AgentOverlayModelPolicy, ...] | None = None

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        normalized = _normalize_optional_string(value, source="agents.model")
        if normalized is None:
            return None
        return _normalize_model_identifier(normalized, project_root=_current_project_root())

    @field_validator("harness", "sandbox")
    @classmethod
    def _validate_optional_string_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_string(value, source="agents")


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
    default_model: Annotated[
        str,
        config_field(
            "defaults.model",
            value_kind="str",
            file_aliases=(
                file_alias("defaults", "model"),
                file_alias("defaults", "default_model"),
                file_alias(None, "model"),
                file_alias(None, "default_model"),
            ),
            env_vars=("MERIDIAN_DEFAULT_MODEL",),
        ),
    ] = ""
    default_harness: Annotated[
        str,
        config_field(
            "defaults.harness",
            value_kind="str",
            file_aliases=(
                file_alias("defaults", "harness"),
                file_alias(None, "default_harness"),
            ),
            env_vars=("MERIDIAN_DEFAULT_HARNESS",),
        ),
    ] = "codex"

    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    primary: PrimaryConfig = Field(default_factory=PrimaryConfig)
    agents: dict[str, AgentOverlayConfig] = Field(default_factory=dict)
    output: OutputConfig = Field(default_factory=OutputConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    work: WorkConfig = Field(default_factory=WorkConfig)

    @field_validator("default_model")
    @classmethod
    def _validate_default_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return normalized
        return _normalize_model_identifier(normalized, project_root=_current_project_root())

    @field_validator("default_harness")
    @classmethod
    def _validate_default_harness(cls, value: str) -> str:
        return _normalize_required_string(value, source="defaults")

    @field_validator("default_wait_yield_seconds", "min_wait_yield_seconds")
    @classmethod
    def _validate_wait_yield_settings(cls, value: float) -> float:
        if isinstance(value, bool) or value <= 0:
            raise ValueError(f"Invalid wait-yield setting: expected float > 0, got {value!r}.")
        return float(value)

    def default_model_for_harness(self, harness_id: str) -> str | None:
        """Return configured default model for one harness ID."""

        normalized = harness_id.strip().lower()
        mapping: dict[str, HarnessProfileConfig] = {
            "claude": self.harness.claude,
            "codex": self.harness.codex,
            "opencode": self.harness.opencode,
        }
        profile = mapping.get(normalized)
        return None if profile is None else profile.model

    def wait_yield_seconds_for_harness(self, harness_id: str | None) -> float:
        """Return clamped wait-yield seconds for a harness or the unknown default."""

        normalized = (harness_id or "").strip().lower()
        mapping: dict[str, HarnessProfileConfig] = {
            "claude": self.harness.claude,
            "codex": self.harness.codex,
            "opencode": self.harness.opencode,
        }
        configured = mapping.get(normalized)
        raw_value = (
            configured.wait_yield_seconds
            if configured is not None and configured.wait_yield_seconds is not None
            else self.default_wait_yield_seconds
        )
        return max(float(raw_value), float(self.min_wait_yield_seconds))

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
