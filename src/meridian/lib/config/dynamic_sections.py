"""Model-backed validation and merge policy for dynamic config sections."""

from __future__ import annotations

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    RootModel,
    StringConstraints,
    ValidationError,
)

from meridian.lib.config.errors import config_validation_error, warn_unknown_model_keys
from meridian.lib.config.models import WorkConfig
from meridian.lib.config.schema import DynamicSectionDescriptor
from meridian.lib.utils.json_shapes import is_json_object, is_object_sequence

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DynamicContextEntry(BaseModel):
    """Validated fields explicitly configured for one named context."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    source: NonEmptyString | None = None
    path: NonEmptyString | None = None
    archive: NonEmptyString | None = None
    remote: NonEmptyString | None = None


class DynamicContextConfig(RootModel[dict[str, DynamicContextEntry]]):
    """Validated named context entries."""

    model_config = ConfigDict(frozen=True, strict=True)


class DynamicConfigSections(BaseModel):
    """Resolved non-catalog config values after layer-specific merge rules."""

    model_config = ConfigDict(frozen=True, strict=True)

    # Hook rows are carried for config inspection, but lib/hooks/config.py is
    # their sole validation and runtime-loading boundary.
    hooks: object | None = None
    work: WorkConfig | None = None
    context: dict[str, DynamicContextEntry] | None = None


DYNAMIC_SECTION_DESCRIPTORS: dict[str, DynamicSectionDescriptor] = {
    "hooks": DynamicSectionDescriptor(
        section_key="hooks",
        merge_kind="replace",
        model=None,
        scaffold_lines=(
            "# -- Hook examples -----------------------------------------------------------",
            "# Hooks are dynamic arrays of tables. Keep examples commented until needed.",
            "",
            "# [[hooks]]",
            '# name = "notify-on-failure"',
            '# event = "spawn.started"',
            '# command = "echo spawn-hook"',
            "# timeout_secs = 30",
            "",
        ),
    ),
    "work": DynamicSectionDescriptor(
        section_key="work",
        merge_kind="nested_dict",
        model=WorkConfig,
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
        model=DynamicContextConfig,
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
        model=None,
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
        model=None,
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


def _dump_configured_value(value: object) -> object:
    """Dump explicitly configured model fields, including excluded inspection fields."""

    if isinstance(value, RootModel):
        dumped: object = value.model_dump(exclude_unset=True)
        return _dump_configured_value(dumped)
    if isinstance(value, BaseModel):
        configured: dict[str, object] = {}
        for field_name in value.model_fields_set:
            field_value: object = getattr(value, field_name)
            configured[field_name] = _dump_configured_value(field_value)
        return configured
    if is_object_sequence(value):
        return tuple(_dump_configured_value(item) for item in value)
    if is_json_object(value):
        return {key: _dump_configured_value(item) for key, item in value.items()}
    return value


def normalize_dynamic_sections(
    *,
    payload: dict[str, object],
) -> dict[str, object]:
    """Validate configured dynamic sections and return mergeable values."""

    normalized: dict[str, object] = {}
    for section_key, descriptor in DYNAMIC_SECTION_DESCRIPTORS.items():
        if section_key not in payload or descriptor.merge_kind == "external":
            continue
        raw_value = payload[section_key]
        if descriptor.model is None:
            normalized[section_key] = raw_value
            continue
        warn_unknown_model_keys(raw_value, model=descriptor.model, source=section_key)
        try:
            validated = descriptor.model.model_validate(raw_value)
        except ValidationError as error:
            raise config_validation_error(error, source=section_key) from None
        normalized[section_key] = _dump_configured_value(validated)
    return normalized


def merge_nested_dict_values(
    base: dict[str, object],
    overrides: dict[str, object],
) -> dict[str, object]:
    merged = dict(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if is_json_object(current) and is_json_object(value):
            merged[key] = merge_nested_dict_values(current, value)
            continue
        merged[key] = value
    return merged


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
            and is_json_object(current)
            and is_json_object(value)
        ):
            merged[section_key] = merge_nested_dict_values(current, value)
            continue
        merged[section_key] = value
    return merged
