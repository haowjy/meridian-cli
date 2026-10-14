"""Compatibility config surface and temporary hook normalization.

The implementation is split by concern. This module remains as the stable internal
import surface because config types are imported throughout the codebase. Hook
normalization stays here until the hooks package takes ownership in state #3.
"""

import logging
from typing import cast

from meridian.lib.config.models import (
    normalize_required_string,
    normalize_string_sequence,
)
from meridian.lib.utils.json_shapes import json_list

logger = logging.getLogger(__name__)


def _parse_toml_list(*, raw_value: object, source: str) -> tuple[str, ...]:
    if json_list(raw_value) is None:
        raise ValueError(
            f"Invalid value for '{source}': expected array[str], "
            f"got {type(raw_value).__name__} ({raw_value!r})."
        )
    return normalize_string_sequence(raw_value, source=source)


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
                row[key] = normalize_required_string(value, source=field_source)
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
                        when["agent"] = normalize_required_string(when_value, source=when_source)
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

from meridian.lib.config.dynamic_sections import (  # noqa: E402
    DYNAMIC_SECTION_DESCRIPTORS,
    merge_dynamic_sections,
    normalize_context_table,
    normalize_dynamic_sections,
    normalize_work_table,
)
from meridian.lib.config.loader import (  # noqa: E402
    OPTION_CATALOG,
    load_config,
    resolve_allow_builtin_agents_for_launch,
    resolve_opencode_version_for_launch,
    resolve_pi_disable_managed_bash,
    resolve_pi_harness_profile,
    resolve_pi_harness_profile_for_launch,
)
from meridian.lib.config.models import (  # noqa: E402
    ClaudeHarnessProfileConfig,
    CodexHarnessProfileConfig,
    EnvironmentConfig,
    HarnessConfig,
    HarnessProfileConfig,
    HistoryArchiveConfig,
    HistoryConfig,
    IdleConfig,
    MeridianConfig,
    MeridianConfigModel,
    NotifyConfig,
    OpenCodeHarnessProfileConfig,
    OutputConfig,
    PiBundleToggleConfig,
    PiHarnessProfileConfig,
    PrimaryConfig,
    StateConfig,
    WorkConfig,
    harness_idle_model,
)

__all__ = [
    "DYNAMIC_SECTION_DESCRIPTORS",
    "OPTION_CATALOG",
    "ClaudeHarnessProfileConfig",
    "CodexHarnessProfileConfig",
    "EnvironmentConfig",
    "HarnessConfig",
    "HarnessProfileConfig",
    "HistoryArchiveConfig",
    "HistoryConfig",
    "IdleConfig",
    "MeridianConfig",
    "MeridianConfigModel",
    "NotifyConfig",
    "OpenCodeHarnessProfileConfig",
    "OutputConfig",
    "PiBundleToggleConfig",
    "PiHarnessProfileConfig",
    "PrimaryConfig",
    "StateConfig",
    "WorkConfig",
    "harness_idle_model",
    "load_config",
    "merge_dynamic_sections",
    "normalize_context_table",
    "normalize_dynamic_sections",
    "normalize_hooks_array",
    "normalize_work_table",
    "resolve_allow_builtin_agents_for_launch",
    "resolve_opencode_version_for_launch",
    "resolve_pi_disable_managed_bash",
    "resolve_pi_harness_profile",
    "resolve_pi_harness_profile_for_launch",
]
