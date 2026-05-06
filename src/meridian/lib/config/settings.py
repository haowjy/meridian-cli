"""Repository-level operational config loader."""

import logging
import os
import tomllib
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from meridian.lib.config.project_config_state import resolve_project_config_state
from meridian.lib.core.overrides import (
    KNOWN_APPROVAL_VALUES,
    KNOWN_EFFORT_VALUES,
)

logger = logging.getLogger(__name__)

_OUTPUT_VERBOSITY_PRESETS = frozenset({"quiet", "normal", "verbose", "debug"})
_PRIMARY_AUTOCOMPACT_PCT_MIN = 1
_PRIMARY_AUTOCOMPACT_PCT_MAX = 100
_LOCAL_CONFIG_FILENAME = "meridian.local.toml"


class _SettingsLoadContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: Path
    user_config: Path | None
    resolve_models: bool = True


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


def _parse_env_int(raw_value: str, *, env_name: str) -> int:
    try:
        return int(raw_value.strip())
    except ValueError as error:
        raise ValueError(
            f"Invalid environment override '{env_name}': expected int, got {raw_value!r}."
        ) from error


def _parse_env_float(raw_value: str, *, env_name: str) -> float:
    try:
        return float(raw_value.strip())
    except ValueError as error:
        raise ValueError(
            f"Invalid environment override '{env_name}': expected float, got {raw_value!r}."
        ) from error


def _parse_env_string(raw_value: str, *, env_name: str) -> str:
    normalized = raw_value.strip()
    if not normalized:
        raise ValueError(f"Invalid environment override '{env_name}': expected non-empty string.")
    return normalized


def _parse_file_scalar(*, field_name: str, raw_value: object, source: str) -> object:
    int_fields = {"max_depth", "max_retries"}
    float_fields = {
        "retry_backoff_seconds",
        "kill_grace_minutes",
        "guardrail_timeout_minutes",
        "wait_timeout_minutes",
        "default_wait_yield_seconds",
        "min_wait_yield_seconds",
    }

    if field_name in int_fields:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ValueError(
                f"Invalid value for '{source}': expected int, got "
                f"{type(raw_value).__name__} ({raw_value!r})."
            )
        return raw_value

    if field_name in float_fields:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise ValueError(
                f"Invalid value for '{source}': expected float, got "
                f"{type(raw_value).__name__} ({raw_value!r})."
            )
        return float(raw_value)

    if not isinstance(raw_value, str):
        raise ValueError(
            f"Invalid value for '{source}': expected str, got "
            f"{type(raw_value).__name__} ({raw_value!r})."
        )
    if field_name == "default_model":
        return raw_value.strip()
    return _normalize_required_string(raw_value, source=source)


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


def _resolve_project_toml(project_root: Path) -> Path | None:
    return resolve_project_config_state(project_root).path


def _resolve_local_toml(project_root: Path) -> Path | None:
    local_config = project_root / _LOCAL_CONFIG_FILENAME
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
            if not (_PRIMARY_AUTOCOMPACT_PCT_MIN <= value <= _PRIMARY_AUTOCOMPACT_PCT_MAX):
                raise ValueError(
                    f"Invalid value for '{source}.autocompact': expected int between "
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

    # Copy autocompact_pct → autocompact if autocompact is not explicitly set.
    if values.get("autocompact_pct") is not None and values.get("autocompact") is None:
        values["autocompact"] = values["autocompact_pct"]

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
        values[key] = {
            "model": _normalize_model_identifier(normalized, project_root=project_root)
        }

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
        if key != "artifacts":
            logger.warning("Ignoring unknown Meridian config key '%s.%s'.", source, key)
            continue

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


def _normalize_toml_payload(
    *,
    payload: dict[str, object],
    path: Path,
    project_root: Path,
) -> dict[str, object]:
    section_aliases: dict[str, dict[str, str]] = {
        "defaults": {
            "max_depth": "max_depth",
            "max_retries": "max_retries",
            "retry_backoff_seconds": "retry_backoff_seconds",
            "model": "default_model",
            "harness": "default_harness",
        },
        "timeouts": {
            "kill_grace_minutes": "kill_grace_minutes",
            "guardrail_minutes": "guardrail_timeout_minutes",
            "guardrail_timeout_minutes": "guardrail_timeout_minutes",
            "wait_minutes": "wait_timeout_minutes",
            "wait_timeout_minutes": "wait_timeout_minutes",
            "wait_yield_after_seconds": "default_wait_yield_seconds",
            "default_wait_yield_seconds": "default_wait_yield_seconds",
            "min_wait_yield_seconds": "min_wait_yield_seconds",
        },
    }
    top_level_aliases: dict[str, str] = {
        "max_depth": "max_depth",
        "max_retries": "max_retries",
        "retry_backoff_seconds": "retry_backoff_seconds",
        "kill_grace_minutes": "kill_grace_minutes",
        "guardrail_timeout_minutes": "guardrail_timeout_minutes",
        "wait_timeout_minutes": "wait_timeout_minutes",
        "wait_yield_after_seconds": "default_wait_yield_seconds",
        "default_wait_yield_seconds": "default_wait_yield_seconds",
        "min_wait_yield_seconds": "min_wait_yield_seconds",
        "model": "default_model",
        "default_harness": "default_harness",
    }

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
        if key == "hooks":
            normalized["hooks"] = _normalize_hooks_array(raw_value, source="hooks")
            continue
        if key == "work":
            normalized["work"] = _merge_nested_dicts(
                cast("dict[str, object]", normalized.get("work", {})),
                _normalize_work_table(raw_value, source="work"),
            )
            continue
        if key == "context":
            normalized["context"] = _merge_nested_dicts(
                cast("dict[str, object]", normalized.get("context", {})),
                _normalize_context_table(raw_value, source="context"),
            )
            continue
        if key == "workspace":
            continue

        section_map = section_aliases.get(key)
        if section_map is not None:
            if not isinstance(raw_value, dict):
                raise ValueError(f"Invalid value for '{key}' in '{path}': expected table.")
            for section_key, section_value in cast("dict[str, object]", raw_value).items():
                field_name = section_map.get(section_key)
                if field_name is None:
                    logger.warning(
                        "Ignoring unknown Meridian config key '%s.%s'.",
                        key,
                        section_key,
                    )
                    continue
                coerced = _parse_file_scalar(
                    field_name=field_name,
                    raw_value=section_value,
                    source=f"{key}.{section_key}",
                )
                if field_name == "default_model":
                    coerced = _normalize_model_identifier(
                        cast("str", coerced),
                        project_root=project_root,
                    )
                normalized[field_name] = coerced
            continue

        field_name = top_level_aliases.get(key)
        if field_name is None:
            logger.warning("Ignoring unknown Meridian config key '%s'.", key)
            continue

        coerced = _parse_file_scalar(
            field_name=field_name,
            raw_value=raw_value,
            source=key,
        )
        if field_name == "default_model":
            coerced = _normalize_model_identifier(cast("str", coerced), project_root=project_root)
        normalized[field_name] = coerced

    return normalized


def _env_alias_overrides(project_root: Path) -> dict[str, object]:
    values: dict[str, object] = {}
    env_specs: tuple[tuple[str, tuple[str, ...], Literal["int", "float", "str"]], ...] = (
        ("MERIDIAN_MAX_DEPTH", ("max_depth",), "int"),
        ("MERIDIAN_MAX_RETRIES", ("max_retries",), "int"),
        ("MERIDIAN_RETRY_BACKOFF_SECONDS", ("retry_backoff_seconds",), "float"),
        ("MERIDIAN_KILL_GRACE_MINUTES", ("kill_grace_minutes",), "float"),
        (
            "MERIDIAN_GUARDRAIL_TIMEOUT_MINUTES",
            ("guardrail_timeout_minutes",),
            "float",
        ),
        ("MERIDIAN_WAIT_TIMEOUT_MINUTES", ("wait_timeout_minutes",), "float"),
        ("MERIDIAN_WAIT_YIELD_AFTER_SECONDS", ("default_wait_yield_seconds",), "float"),
        ("MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS", ("default_wait_yield_seconds",), "float"),
        ("MERIDIAN_MIN_WAIT_YIELD_SECONDS", ("min_wait_yield_seconds",), "float"),
        ("MERIDIAN_DEFAULT_MODEL", ("default_model",), "str"),
        ("MERIDIAN_DEFAULT_HARNESS", ("default_harness",), "str"),
        ("MERIDIAN_HARNESS_MODEL_CLAUDE", ("harness", "claude", "model"), "str"),
        ("MERIDIAN_HARNESS_MODEL_CODEX", ("harness", "codex", "model"), "str"),
        ("MERIDIAN_HARNESS_MODEL_OPENCODE", ("harness", "opencode", "model"), "str"),
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
        ("MERIDIAN_STATE_RETENTION_DAYS", ("state", "retention_days"), "int"),
        ("MERIDIAN_AGENT", ("primary", "agent"), "str"),
        ("MERIDIAN_FORMAT", ("output", "format"), "str"),
    )

    for env_name, field_path, value_kind in env_specs:
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue

        parsed: object
        if value_kind == "int":
            parsed = _parse_env_int(raw_value, env_name=env_name)
        elif value_kind == "float":
            parsed = _parse_env_float(raw_value, env_name=env_name)
        else:
            parsed = _parse_env_string(raw_value, env_name=env_name)

        if field_path in {
            ("default_model",),
            ("harness", "claude", "model"),
            ("harness", "codex", "model"),
            ("harness", "opencode", "model"),
            ("primary", "model"),
        }:
            parsed = _normalize_model_identifier(cast("str", parsed), project_root=project_root)

        _assign_nested_value(values, field_path, parsed)

    return values


class OutputConfig(BaseModel):
    """Terminal output filtering configuration for run streaming."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    show: tuple[str, ...] = ("lifecycle", "sub-run", "error", "system")
    verbosity: str | None = None
    format: str = "text"

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

    retention_days: int = 30

    @field_validator("retention_days")
    @classmethod
    def _validate_retention_days(cls, value: int) -> int:
        if isinstance(value, bool) or value < -1:
            raise ValueError(
                "Invalid value for 'state.retention_days': expected int >= -1, "
                f"got {value!r}."
            )
        return value


class PrimaryConfig(BaseModel):
    """Primary-specific harness settings."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    autocompact_pct: int | None = None
    model: str | None = None
    harness: str | None = None
    agent: str | None = None
    effort: str | None = None
    sandbox: str | None = None
    approval: str | None = None
    timeout: float | None = None
    autocompact: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _copy_autocompact_pct_to_autocompact(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        d: dict[str, Any] = cast("dict[str, Any]", values)
        if d.get("autocompact") is None and d.get("autocompact_pct") is not None:
            d = dict(d)
            d["autocompact"] = d["autocompact_pct"]
        return d

    @field_validator("autocompact_pct")
    @classmethod
    def _validate_autocompact_pct(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not (_PRIMARY_AUTOCOMPACT_PCT_MIN <= value <= _PRIMARY_AUTOCOMPACT_PCT_MAX):
            raise ValueError(
                "Invalid value for 'primary.autocompact_pct': expected int between "
                f"{_PRIMARY_AUTOCOMPACT_PCT_MIN} and "
                f"{_PRIMARY_AUTOCOMPACT_PCT_MAX}, got {value!r}."
            )
        return value

    @field_validator("autocompact")
    @classmethod
    def _validate_autocompact(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not (_PRIMARY_AUTOCOMPACT_PCT_MIN <= value <= _PRIMARY_AUTOCOMPACT_PCT_MAX):
            raise ValueError(
                "Invalid value for 'primary.autocompact': expected int between "
                f"{_PRIMARY_AUTOCOMPACT_PCT_MIN} and "
                f"{_PRIMARY_AUTOCOMPACT_PCT_MAX}, got {value!r}."
            )
        return value

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

    @field_validator("effort")
    @classmethod
    def _validate_effort(cls, value: str | None) -> str | None:
        normalized = _normalize_optional_string(value, source="primary.effort")
        if normalized is None:
            return None
        if normalized not in KNOWN_EFFORT_VALUES:
            raise ValueError(
                "Invalid value for 'primary.effort': expected one of "
                f"{sorted(KNOWN_EFFORT_VALUES)}, got {value!r}."
            )
        return normalized

    @field_validator("approval")
    @classmethod
    def _validate_approval(cls, value: str | None) -> str | None:
        normalized = _normalize_optional_string(value, source="primary.approval")
        if normalized is None:
            return None
        if normalized not in KNOWN_APPROVAL_VALUES:
            raise ValueError(
                "Invalid value for 'primary.approval': expected one of "
                f"{sorted(KNOWN_APPROVAL_VALUES)}, got {value!r}."
            )
        return normalized

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
                "Invalid value for 'harness.wait_yield_seconds': expected float, "
                f"got {value!r}."
            )
        return float(value)


class HarnessConfig(BaseModel):
    """Default model and wait-yield configuration for each harness adapter."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    claude: HarnessProfileConfig = Field(
        default_factory=lambda: HarnessProfileConfig(wait_yield_seconds=3000.0)
    )
    codex: HarnessProfileConfig = Field(
        default_factory=lambda: HarnessProfileConfig(wait_yield_seconds=3000.0)
    )
    opencode: HarnessProfileConfig = Field(
        default_factory=lambda: HarnessProfileConfig(model="opencode-go/kimi-k2.6")
    )


class MeridianConfig(BaseSettings):
    """Resolved operational configuration for meridian."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="ignore",
        env_prefix="MERIDIAN_",
        env_nested_delimiter="__",
    )

    max_depth: int = 3
    max_retries: int = 3
    retry_backoff_seconds: float = 0.25
    kill_grace_minutes: float = 2.0 / 60.0
    guardrail_timeout_minutes: float = 0.5
    wait_timeout_minutes: float = 30.0
    default_wait_yield_seconds: float = 3000.0
    min_wait_yield_seconds: float = 30.0
    default_model: str = ""
    default_harness: str = "codex"

    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    primary: PrimaryConfig = Field(default_factory=PrimaryConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    state: StateConfig = Field(default_factory=StateConfig)

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
            raise ValueError(
                "Invalid wait-yield setting: expected float > 0, "
                f"got {value!r}."
            )
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

    @property
    def wait_yield_after_seconds(self) -> float:
        """Compatibility alias for the unknown/default wait-yield interval."""

        return self.default_wait_yield_seconds

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
            project_config = _resolve_project_toml(context.project_root)
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
            local_config = _resolve_local_toml(context.project_root)
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


def load_config(
    project_root: Path,
    *,
    user_config: Path | None = None,
    resolve_models: bool = True,
) -> MeridianConfig:
    """Load config with precedence: defaults < user < project < local < environment.

    RuntimeOverrides fields (model, harness, effort, etc.) are NOT loaded
    from ENV here — they are read separately via RuntimeOverrides.from_env().
    """

    from meridian.lib.config.project_root import resolve_user_config_path

    resolved_project_root = project_root.expanduser().resolve()
    resolved_user_config = resolve_user_config_path(user_config)

    token = _SETTINGS_CONTEXT.set(
        _SettingsLoadContext(
            project_root=resolved_project_root,
            user_config=resolved_user_config,
            resolve_models=resolve_models,
        )
    )
    try:
        return MeridianConfig()
    finally:
        _SETTINGS_CONTEXT.reset(token)
