"""Config schema metadata and scalar codecs."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import Literal, cast

_OUTPUT_VERBOSITY_PRESETS = frozenset({"quiet", "normal", "verbose", "debug"})

ValueKind = Literal["int", "float", "str", "str_list", "verbosity"]


@dataclass(frozen=True)
class FileAlias:
    """One accepted TOML key spelling for a scalar config option."""

    section: str | None
    key: str


@dataclass(frozen=True)
class ConfigOptionMeta:
    """Metadata attached to one scalar MeridianConfig field."""

    canonical_key: str
    value_kind: ValueKind
    file_aliases: tuple[FileAlias, ...] = ()
    env_vars: tuple[str, ...] = ()
    command_visible: bool = True


@dataclass(frozen=True)
class ConfigOptionDescriptor:
    """Catalog entry for one metadata-backed scalar option."""

    canonical_key: str
    field_path: tuple[str, ...]
    value_kind: ValueKind
    file_aliases: tuple[FileAlias, ...]
    env_vars: tuple[str, ...]
    command_visible: bool

    @property
    def primary_env_var(self) -> str | None:
        return self.env_vars[0] if self.env_vars else None


def config_field(
    canonical_key: str,
    *,
    value_kind: ValueKind,
    file_aliases: tuple[FileAlias, ...] = (),
    env_vars: tuple[str, ...] = (),
    command_visible: bool = True,
) -> ConfigOptionMeta:
    """Attach Meridian-private config metadata to one Pydantic field."""

    return ConfigOptionMeta(
        canonical_key=canonical_key,
        value_kind=value_kind,
        file_aliases=file_aliases,
        env_vars=env_vars,
        command_visible=command_visible,
    )


def parse_toml_scalar(*, value_kind: ValueKind, raw_value: object, source: str) -> object:
    if value_kind == "int":
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ValueError(
                f"Invalid value for '{source}': expected int, got "
                f"{type(raw_value).__name__} ({raw_value!r})."
            )
        return raw_value

    if value_kind == "float":
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise ValueError(
                f"Invalid value for '{source}': expected float, got "
                f"{type(raw_value).__name__} ({raw_value!r})."
            )
        return float(raw_value)

    if value_kind == "str_list":
        if not isinstance(raw_value, list):
            raise ValueError(
                f"Invalid value for '{source}': expected array[str], got "
                f"{type(raw_value).__name__} ({raw_value!r})."
            )
        parsed: list[str] = []
        for item in cast("list[object]", raw_value):
            if not isinstance(item, str):
                raise ValueError(
                    f"Invalid value for '{source}': expected array[str], got "
                    f"{type(item).__name__} ({item!r})."
                )
            normalized = item.strip()
            if not normalized:
                raise ValueError(f"Invalid value for '{source}': expected non-empty items.")
            parsed.append(normalized)
        return tuple(parsed)

    if not isinstance(raw_value, str):
        raise ValueError(
            f"Invalid value for '{source}': expected str, got "
            f"{type(raw_value).__name__} ({raw_value!r})."
        )

    normalized = raw_value.strip()
    if not normalized:
        raise ValueError(f"Invalid value for '{source}': expected non-empty string.")

    if value_kind == "verbosity":
        lowered = normalized.lower()
        if lowered not in _OUTPUT_VERBOSITY_PRESETS:
            raise ValueError(
                f"Invalid value for '{source}': expected one of "
                f"{sorted(_OUTPUT_VERBOSITY_PRESETS)}, got {raw_value!r}."
            )
        return lowered

    return normalized


def parse_cli_scalar(*, canonical_key: str, value_kind: ValueKind, raw_value: str) -> object:
    normalized = raw_value.strip()

    if value_kind == "int":
        try:
            return int(normalized)
        except ValueError as error:
            raise ValueError(
                f"Invalid value for '{canonical_key}': expected int, got {raw_value!r}."
            ) from error

    if value_kind == "float":
        try:
            return float(normalized)
        except ValueError as error:
            raise ValueError(
                f"Invalid value for '{canonical_key}': expected float, got {raw_value!r}."
            ) from error

    if value_kind == "str_list":
        if not normalized:
            raise ValueError(
                f"Invalid value for '{canonical_key}': expected comma-separated values."
            )

        items: list[str]
        if normalized.startswith("["):
            try:
                parsed_obj = tomllib.loads(f"value = {normalized}")["value"]
            except tomllib.TOMLDecodeError as error:
                raise ValueError(
                    f"Invalid TOML array for '{canonical_key}': {raw_value!r}."
                ) from error
            if not isinstance(parsed_obj, list):
                raise ValueError(f"Invalid value for '{canonical_key}': expected array[str].")
            items = [str(item).strip() for item in cast("list[object]", parsed_obj)]
        else:
            items = [part.strip() for part in normalized.split(",")]

        filtered = [item for item in items if item]
        if not filtered:
            raise ValueError(
                f"Invalid value for '{canonical_key}': expected non-empty items."
            )
        return tuple(filtered)

    if not normalized:
        raise ValueError(f"Invalid value for '{canonical_key}': expected non-empty string.")

    if value_kind == "verbosity":
        lowered = normalized.lower()
        if lowered not in _OUTPUT_VERBOSITY_PRESETS:
            raise ValueError(
                f"Invalid value for '{canonical_key}': expected one of "
                f"{sorted(_OUTPUT_VERBOSITY_PRESETS)}, got {raw_value!r}."
            )
        return lowered

    return normalized


def parse_env_scalar(*, value_kind: ValueKind, raw_value: str, env_name: str) -> object:
    if value_kind == "int":
        try:
            return int(raw_value.strip())
        except ValueError as error:
            raise ValueError(
                f"Invalid environment override '{env_name}': expected int, got {raw_value!r}."
            ) from error

    if value_kind == "float":
        try:
            return float(raw_value.strip())
        except ValueError as error:
            raise ValueError(
                f"Invalid environment override '{env_name}': expected float, got {raw_value!r}."
            ) from error

    normalized = raw_value.strip()
    if not normalized:
        raise ValueError(
            f"Invalid environment override '{env_name}': expected non-empty string."
        )
    return normalized


def normalize_runtime_scalar(value: object) -> object:
    if isinstance(value, tuple):
        return tuple(str(item) for item in cast("tuple[object, ...]", value))
    return value
