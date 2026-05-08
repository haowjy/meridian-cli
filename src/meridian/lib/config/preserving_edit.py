"""TOML-preserving scalar config edits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import tomlkit
from tomlkit.items import Table

from meridian.lib.config.schema import ConfigOptionDescriptor

if TYPE_CHECKING:
    from tomlkit.toml_document import TOMLDocument

type TomlEditableScalar = bool | int | float | str | list[str]


@dataclass(frozen=True)
class ScalarEditResult:
    text: str
    removed: bool


def set_scalar_option(
    text: str,
    *,
    option: ConfigOptionDescriptor,
    value: object,
) -> ScalarEditResult:
    document = _parse_document(text)
    _remove_non_primary_aliases(document, option)

    primary_alias = _primary_file_alias(option)
    target_table = _ensure_table(document, primary_alias.table_path)
    target_table[primary_alias.key] = tomlkit.item(_coerce_toml_scalar(value))

    return ScalarEditResult(text=_render_document(document, original_text=text), removed=False)


def reset_scalar_option(text: str, *, option: ConfigOptionDescriptor) -> ScalarEditResult:
    document = _parse_document(text)
    removed = False
    for alias in option.file_aliases:
        removed = _remove_alias(document, alias.table_path, alias.key) or removed
    return ScalarEditResult(text=_render_document(document, original_text=text), removed=removed)


def _remove_non_primary_aliases(document: TOMLDocument, option: ConfigOptionDescriptor) -> None:
    primary_alias = _primary_file_alias(option)
    for alias in option.file_aliases:
        if alias == primary_alias:
            continue
        _remove_alias(document, alias.table_path, alias.key)


def _primary_file_alias(option: ConfigOptionDescriptor):
    if not option.file_aliases:
        raise ValueError(f"Config option '{option.canonical_key}' is not file-editable.")
    return option.file_aliases[0]


def _ensure_table(
    document: TOMLDocument,
    table_path: tuple[str, ...],
) -> TOMLDocument | Table:
    container: TOMLDocument | Table = document
    for depth, segment in enumerate(table_path):
        child = cast("object | None", container.get(segment))
        if child is None:
            new_table = tomlkit.table(is_super_table=depth < len(table_path) - 1)
            container[segment] = new_table
            child = container[segment]
        if not isinstance(child, Table):
            dotted_path = ".".join((*table_path[: depth + 1],))
            raise ValueError(f"Cannot edit config key because '{dotted_path}' is not a TOML table.")
        container = child
    return container


def _remove_alias(
    document: TOMLDocument,
    table_path: tuple[str, ...],
    key: str,
) -> bool:
    container: TOMLDocument | Table = document
    ancestors: list[tuple[TOMLDocument | Table, str, Table]] = []

    for segment in table_path:
        child = cast("object | None", container.get(segment))
        if not isinstance(child, Table):
            return False
        ancestors.append((container, segment, child))
        container = child

    if key not in container:
        return False

    del container[key]
    _prune_empty_tables(ancestors)
    return True


def _prune_empty_tables(ancestors: list[tuple[TOMLDocument | Table, str, Table]]) -> None:
    for parent, segment, child in reversed(ancestors):
        if len(child) != 0:
            break
        del parent[segment]


def _render_document(document: TOMLDocument, *, original_text: str) -> str:
    rendered = cast("str", tomlkit.dumps(document))
    if rendered.startswith("\n") and not original_text.startswith("\n"):
        return rendered.removeprefix("\n")
    return rendered


def _parse_document(text: str) -> TOMLDocument:
    return cast("TOMLDocument", tomlkit.parse(text))


def _coerce_toml_scalar(value: object) -> TomlEditableScalar:
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, tuple):
        items = [str(item) for item in value]
        return items
    raise ValueError(f"Unsupported config value type: {type(value).__name__}")
