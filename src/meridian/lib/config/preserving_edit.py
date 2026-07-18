"""Serialized, TOML-preserving project config edits."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import tomlkit
from tomlkit.items import Table

from meridian.lib.config.schema import ConfigOptionDescriptor
from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.platform.locking import lock_file

if TYPE_CHECKING:
    from tomlkit.toml_document import TOMLDocument

type TomlEditableScalar = bool | int | float | str | list[str]
type ProjectConfigEdit[T] = Callable[[str], tuple[str, T]]

_PROJECT_ID_COMMENT = "# managed by meridian — do not edit"


@dataclass(frozen=True)
class ScalarEditResult:
    text: str
    removed: bool


def project_config_lock_path(project_root: Path, user_home: Path) -> Path:
    """Return the stable user-home lock for one project's ``meridian.toml``.

    The resolved project root is hashed so checkouts with the same basename do
    not contend and the lock never creates repo-local or identity-dependent
    runtime state.
    """

    root_key = project_root.resolve().as_posix().encode("utf-8")
    digest = hashlib.sha256(root_key).hexdigest()
    return user_home / "locks" / "project-config" / f"{digest}.lock"


@contextmanager
def project_config_transaction(project_root: Path, user_home: Path) -> Generator[None, None, None]:
    """Serialize project config mutations; nested use is thread-local reentrant."""

    with lock_file(project_config_lock_path(project_root, user_home)):
        yield


def mutate_project_config[T](
    project_root: Path,
    user_home: Path,
    edit: ProjectConfigEdit[T],
) -> T:
    """Lock, read, preserve-edit, and atomically replace ``meridian.toml``."""

    with project_config_transaction(project_root, user_home):
        project_root.mkdir(parents=True, exist_ok=True)
        path = project_root / "meridian.toml"
        try:
            original = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            original = ""
        updated, result = edit(original)
        if updated != original:
            atomic_write_text(path, updated)
        return result


def set_project_id(text: str, project_id: str, *, config_path: Path) -> str:
    """Append a precedence-exempt project identity without reformatting TOML."""

    normalized = project_id.strip()
    if not normalized:
        raise ValueError("Project ID must not be empty.")

    try:
        payload = cast("dict[str, object]", tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in '{config_path.as_posix()}': {exc}") from exc
    project = payload.get("project")
    if project is not None:
        if not isinstance(project, dict):
            raise ValueError(f"Invalid [project] table in '{config_path.as_posix()}'.")
        existing = cast("dict[str, object]", project).get("id")
        if existing is None:
            raise ValueError("Existing [project] table has no valid id.")
        if not isinstance(existing, str) or not existing.strip():
            raise ValueError(f"Invalid [project] id in '{config_path.as_posix()}'.")
        if existing.strip() != normalized:
            raise ValueError("Project identity is immutable once assigned.")
        return text

    separator = "" if not text or text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
    encoded_id = json.dumps(normalized, ensure_ascii=False)
    return f"{text}{separator}{_PROJECT_ID_COMMENT}\n[project]\nid = {encoded_id}\n"


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
    target_table[primary_alias.key] = tomlkit.item(_coerce_toml_scalar(value))  # pyright: ignore[reportUnknownMemberType]

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
        child = cast("object | None", container.get(segment))  # pyright: ignore[reportUnknownMemberType]
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
        child = cast("object | None", container.get(segment))  # pyright: ignore[reportUnknownMemberType]
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
    rendered = tomlkit.dumps(document)  # pyright: ignore[reportUnknownMemberType]
    if rendered.startswith("\n") and not original_text.startswith("\n"):
        return rendered.removeprefix("\n")
    return rendered


def _parse_document(text: str) -> TOMLDocument:
    return tomlkit.parse(text)


def _coerce_toml_scalar(value: object) -> TomlEditableScalar:
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, tuple):
        items = [str(item) for item in cast("tuple[object, ...]", value)]
        return items
    raise ValueError(f"Unsupported config value type: {type(value).__name__}")
