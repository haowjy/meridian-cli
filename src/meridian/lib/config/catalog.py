"""Generated catalog for metadata-backed scalar config options."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel

from meridian.lib.config.schema import ConfigOptionDescriptor, ConfigOptionMeta, FileAlias


@dataclass(frozen=True)
class OptionCatalog:
    options: tuple[ConfigOptionDescriptor, ...]
    _by_canonical_key: dict[str, ConfigOptionDescriptor] = field(init=False, repr=False)
    _by_lookup_key: dict[str, ConfigOptionDescriptor] = field(init=False, repr=False)
    _by_file_alias: dict[tuple[str | None, str], ConfigOptionDescriptor] = field(
        init=False,
        repr=False,
    )
    _by_env_var: dict[str, ConfigOptionDescriptor] = field(init=False, repr=False)
    _by_field_path: dict[tuple[str, ...], ConfigOptionDescriptor] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        canonical: dict[str, ConfigOptionDescriptor] = {}
        lookup: dict[str, ConfigOptionDescriptor] = {}
        file_aliases: dict[tuple[str | None, str], ConfigOptionDescriptor] = {}
        env_vars: dict[str, ConfigOptionDescriptor] = {}
        field_paths: dict[tuple[str, ...], ConfigOptionDescriptor] = {}

        for option in self.options:
            existing = canonical.get(option.canonical_key)
            if existing is not None and existing != option:
                raise ValueError(f"Duplicate config canonical key '{option.canonical_key}'.")
            canonical[option.canonical_key] = option
            field_paths[option.field_path] = option

            for alias in _lookup_aliases(option):
                existing_lookup = lookup.get(alias)
                if existing_lookup is not None and existing_lookup != option:
                    raise ValueError(f"Conflicting config key alias '{alias}'.")
                lookup[alias] = option

            for file_alias in option.file_aliases:
                file_key = (file_alias.section, file_alias.key)
                existing_file = file_aliases.get(file_key)
                if existing_file is not None and existing_file != option:
                    raise ValueError(f"Conflicting config file alias '{file_key}'.")
                file_aliases[file_key] = option

            for env_var in option.env_vars:
                existing_env = env_vars.get(env_var)
                if existing_env is not None and existing_env != option:
                    raise ValueError(f"Conflicting config env var alias '{env_var}'.")
                env_vars[env_var] = option

        object.__setattr__(self, "_by_canonical_key", canonical)
        object.__setattr__(self, "_by_lookup_key", lookup)
        object.__setattr__(self, "_by_file_alias", file_aliases)
        object.__setattr__(self, "_by_env_var", env_vars)
        object.__setattr__(self, "_by_field_path", field_paths)

    @property
    def visible_options(self) -> tuple[ConfigOptionDescriptor, ...]:
        return tuple(option for option in self.options if option.command_visible)

    def resolve_key(self, key: str) -> ConfigOptionDescriptor:
        normalized = key.strip()
        if not normalized:
            raise ValueError("Config key must not be empty.")
        option = self._by_lookup_key.get(normalized)
        if option is None:
            valid = ", ".join(entry.canonical_key for entry in self.visible_options)
            raise ValueError(f"Unknown config key '{key}'. Supported keys: {valid}")
        return option

    def find_file_alias(
        self,
        *,
        section: str | None,
        key: str,
    ) -> ConfigOptionDescriptor | None:
        return self._by_file_alias.get((section, key))

    def option_for_field_path(self, field_path: tuple[str, ...]) -> ConfigOptionDescriptor | None:
        return self._by_field_path.get(field_path)

    def env_options(self) -> tuple[tuple[str, ConfigOptionDescriptor], ...]:
        return tuple(self._by_env_var.items())


@dataclass(frozen=True)
class _TraversalFrame:
    model: type[BaseModel]
    path: tuple[str, ...]


def build_option_catalog(root_model: type[BaseModel]) -> OptionCatalog:
    options: list[ConfigOptionDescriptor] = []
    queue: list[_TraversalFrame] = [_TraversalFrame(model=root_model, path=())]

    while queue:
        frame = queue.pop(0)
        for field_name, field_info in frame.model.model_fields.items():
            field_path = (*frame.path, field_name)
            metadata = _extract_option_meta(field_info.metadata)
            if metadata is not None:
                options.append(
                    ConfigOptionDescriptor(
                        canonical_key=metadata.canonical_key,
                        field_path=field_path,
                        value_kind=metadata.value_kind,
                        file_aliases=metadata.file_aliases,
                        env_vars=metadata.env_vars,
                        command_visible=metadata.command_visible,
                    )
                )

            nested_model = _nested_base_model_type(field_info.annotation)
            if nested_model is not None:
                queue.append(_TraversalFrame(model=nested_model, path=field_path))

    return OptionCatalog(tuple(options))


def _extract_option_meta(metadata: list[Any]) -> ConfigOptionMeta | None:
    for item in metadata:
        if isinstance(item, ConfigOptionMeta):
            return item
    return None


def _nested_base_model_type(annotation: Any) -> type[BaseModel] | None:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _nested_base_model_type(get_args(annotation)[0])
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _lookup_aliases(option: ConfigOptionDescriptor) -> tuple[str, ...]:
    aliases: list[str] = [option.canonical_key]
    for file_alias in option.file_aliases:
        if file_alias.section is None:
            aliases.append(file_alias.key)
            continue
        aliases.append(f"{file_alias.section}.{file_alias.key}")
    return tuple(dict.fromkeys(aliases))


def file_alias(section: str | None, key: str) -> FileAlias:
    return FileAlias(section=section, key=key)
