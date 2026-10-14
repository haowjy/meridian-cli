"""Normalization and merge policy for dynamic config sections."""

import logging
from pathlib import Path
from typing import cast

from meridian.lib.config.models import normalize_required_string
from meridian.lib.config.schema import DynamicSectionDescriptor
from meridian.lib.hooks.schema import normalize_hooks_array

logger = logging.getLogger(__name__)


def merge_nested_dict_values(
    base: dict[str, object],
    overrides: dict[str, object],
) -> dict[str, object]:
    merged = dict(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_nested_dict_values(
                cast("dict[str, object]", current),
                cast("dict[str, object]", value),
            )
            continue
        merged[key] = value
    return merged


def _normalize_env_table(raw_value: object, *, source: str) -> dict[str, str]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")
    values: dict[str, str] = {}
    for raw_key, raw_val in cast("dict[str, object]", raw_value).items():
        key = str(raw_key).strip()
        if not key:
            raise ValueError(f"Invalid value for '{source}': expected non-empty key.")
        if not isinstance(raw_val, str):
            raise ValueError(
                f"Invalid value for '{source}.{key}': expected string, got "
                f"{type(raw_val).__name__} ({raw_val!r})."
            )
        values[key] = raw_val
    return values


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
                    artifacts["sync"] = normalize_required_string(
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
                context_fields[key] = normalize_required_string(value, source=field_source)
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
    "env": DynamicSectionDescriptor(
        section_key="env",
        merge_kind="nested_dict",
        scaffold_lines=(
            "# -- Environment variables passed to spawned harnesses -----------------------",
            "# Flat key-value string map. Applied to every spawn from this project.",
            "# Keys from meridian.local.toml override meridian.toml; project overrides user.",
            "",
            "# [env]",
            '# OPENCODE_ENABLE_EXA = "1"',
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
            value = normalize_hooks_array(raw_value, source=section_key)
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
        elif section_key == "env":
            value = _normalize_env_table(raw_value, source=section_key)
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
            merged[section_key] = merge_nested_dict_values(
                cast("dict[str, object]", current),
                cast("dict[str, object]", value),
            )
            continue
        merged[section_key] = value
    return merged
